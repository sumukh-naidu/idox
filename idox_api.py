#!/usr/bin/env python3
"""
idox_api.py -- a FastAPI service over the idox converters, for other programs to call.

The web app (idox_app.py) is for a person with a browser. This is the same thing for
code: upload a file to a conversion endpoint, get a job id back at once, poll the job,
download the result. It adds no conversion logic of its own.

WHY SUBPROCESSES. The converters are scripts that run their whole job at import time
(argparse at top level), so they cannot be imported and called. Each job runs the SAME
script the CLI and the web app run, as a subprocess, from its own folder -- the
scripts write relative paths such as render/, and a folder per job keeps two jobs
from touching each other (handover_chat_integration.md, section 7.4).

WHY JOBS, NOT A WAITING REQUEST. A model conversion takes about a minute per page on
the local 2B. A request held open that long times out in most clients and proxies,
so every conversion endpoint answers 202 with a job, and the caller polls it.

ENDPOINTS
    GET    /health                      model server and tool status
    POST   /convert/pdf-to-word         upload a PDF -> job (202)
    GET    /jobs/{id}                   state, page progress, summary, files
    GET    /jobs/{id}/files/{name}      download one output file
    GET    /jobs/{id}/log               the converter's full output, as text
    DELETE /jobs/{id}                   cancel a queued/running job, or delete a finished one

JOB STATES
    queued -> running -> done      finished, no problems reported
                      -> review    a file was written, but the converter reported a
                                   problem (a page failed, a check failed): read
                                   "problems" before trusting it
                      -> failed    no file was written; "error" says why
                      -> cancelled

CONFIGURATION (environment variables, read at start)
    IDOX_BASE_URL       the model server (default http://127.0.0.1:8090, the local 2B;
                        blocks.DEFAULT_BASE_URL points at a remote 8B that is down)
    IDOX_API_KEY        sent to the model server if set (the tuhin-ai API needs one)
    IDOX_THINKING, IDOX_MAX_IMAGE_SIDE, IDOX_TIMEOUT
                        passed through to the converter if set (see blocks.py)
    IDOX_JOBS_DIR       where jobs are kept (default ~/.local/share/idox/api_jobs)
    IDOX_MODEL_JOBS     model jobs that may run at once (default 1: the local server
                        reads one page at a time; two at once only share the CPU)
    IDOX_OTHER_JOBS     no-model jobs that may run at once (default 3)
    IDOX_MAX_UPLOAD_MB  largest accepted upload (default 100)
    IDOX_JOB_TIMEOUT_S  a job running longer is stopped (default 7200)
    IDOX_KEEP_HOURS     finished jobs older than this are deleted at start (default 48)

Usage:
    .venv/bin/uvicorn idox_api:app --host 127.0.0.1 --port 8000
    then open http://127.0.0.1:8000/docs for the interactive API page.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

REPO = Path(__file__).resolve().parent
PY = sys.executable

BASE_URL = os.environ.get("IDOX_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
JOBS_DIR = Path(os.environ.get("IDOX_JOBS_DIR",
                               Path.home() / ".local/share/idox/api_jobs")).expanduser()
LIMITS = {"model": int(os.environ.get("IDOX_MODEL_JOBS", "1")),
          "other": int(os.environ.get("IDOX_OTHER_JOBS", "3"))}
MAX_UPLOAD = int(os.environ.get("IDOX_MAX_UPLOAD_MB", "100")) * 1024 * 1024
JOB_TIMEOUT_S = int(os.environ.get("IDOX_JOB_TIMEOUT_S", str(2 * 60 * 60)))
KEEP_HOURS = float(os.environ.get("IDOX_KEEP_HOURS", "48"))

# Passed to the converter only when set here. Everything else starting IDOX_ is
# kept out of the child, so the service's own settings never leak into a script.
PASS_THROUGH = ("IDOX_API_KEY", "IDOX_THINKING", "IDOX_MAX_IMAGE_SIDE", "IDOX_TIMEOUT")

JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
PAGES_RE = re.compile(r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$")

# One entry per conversion. "magic" is the start of a valid file of that type, so a
# renamed file is refused at upload rather than failing inside the script.
ROUTES = {
    "pdf_docx": dict(script="test_pdf.py", model=True, exts=(".pdf",), magic=(b"%PDF-",),
                     out_ext="docx"),
}

JOBS = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def build_command(job: dict) -> list:
    """The argument list for one job, run from the job's folder. Never a shell string."""
    o, stem = job["options"], job["stem"]
    inp = f"in/{stem}{job['ext']}"
    if job["route"] == "pdf_docx":
        cmd = [PY, "-u", str(REPO / "test_pdf.py"), inp, "--base-url", BASE_URL,
               "--docx", f"out/{stem}.docx", "--out", f"out/{stem}.md",
               "--scan-mode", o.get("scan_mode", "text")]
        if o.get("pages"):
            cmd += ["--pages", o["pages"]]
        return cmd
    raise ValueError(f"no command for route {job['route']}")


def job_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("IDOX_")}
    env.update({k: os.environ[k] for k in PASS_THROUGH if os.environ.get(k)})
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(REPO)
    return env


# ---------------------------------------------------------------------------
# reading a finished job -- the same rules as idox_app.py, which were tested there
# ---------------------------------------------------------------------------
PROBLEMS = [
    (re.compile(r"VERDICT: FAIL"), "The script's own verification failed."),
    (re.compile(r"Tier 1 FAILED"), "The model failed on at least one page, which was skipped."),
    (re.compile(r"MODEL FAILED"), "The model failed on at least one page."),
    (re.compile(r"could not be read"), "At least one page could not be read."),
    (re.compile(r"COULD NOT OPEN|SKIPPED:"), "The file could not be processed."),
    (re.compile(r"Traceback \(most recent call last\)"), "The script crashed."),
    (re.compile(r"EXTRACTION FAILED"), "The model could not read the file."),
]
SUMMARY_LINES = re.compile(
    r"(VERDICT|verdict|pages verified|pages written|pages converted|pages in PDF|"
    r"content check|image check|word coverage|paragraphs kept|table cells kept|"
    r"UNVERIFIED|OCR agreement|self-consistency|OCR grounding|OCR coverage|"
    r"note:|FAILED|could not be read|dropped|restored|corrected|merged)")
FAILURE_REASON = re.compile(
    r"(?:EXTRACTION FAILED|MODEL FAILED|Tier 1 FAILED to produce valid output|"
    r"COULD NOT OPEN|SKIPPED|Could not start the converter)[:( ]+(.+)")


def job_dir(jid: str) -> Path:
    return JOBS_DIR / jid


def read_log(jid: str) -> str:
    try:
        return (job_dir(jid) / "log.txt").read_text("utf-8", "replace")
    except OSError:
        return ""


def list_outputs(jid: str) -> list:
    out_dir, files = job_dir(jid) / "out", []
    if out_dir.is_dir():
        for p in sorted(out_dir.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                files.append({"name": p.relative_to(out_dir).as_posix(),
                              "size": p.stat().st_size})
    return files


def progress_of(job: dict, text: str):
    """{done, total} pages, read from the converter's own progress lines, or None."""
    if ROUTES[job["route"]]["script"] == "test_pdf.py":
        m = re.search(r"testing (\d+):", text)
        done = len(re.findall(r"^Tier 1 (?:--|FAILED)", text, re.M))
    else:
        return None
    total = int(m.group(1)) if m else None
    return {"done": min(done, total), "total": total} if total else None


def summarize(text: str) -> list:
    seen, out = set(), []
    for line in text.splitlines():
        s = line.strip().lstrip("!~- ").strip()
        s = re.sub(r"^\d+(?:/\d+)?\.\s+", "", s)
        if s and SUMMARY_LINES.search(line) and s not in seen:
            seen.add(s)
            out.append(s[:200])
    return out[-14:]


PAGE_HEAD = re.compile(r"^PAGE (\d+)$", re.M)
CHECK_LINE = re.compile(r"^  (\d)\. ([a-z -]+?)(?: \(advisory\))?:\s+(PASS|FAIL|N/A|differs|matches)\b[ \t]*(.*)$",
                        re.M)
CHECK_KEYS = {"self-consistency": "self_consistency", "text-layer grounding": "grounding",
              "geometry": "geometry", "content coverage": "coverage"}


def page_checks(log: str) -> list:
    """test_pdf.py's four checks, per page, from its "--- checks ---" lines.

    Check 3 (geometry) is advisory in test_pdf.py itself and never makes a job need
    review. On a scanned page, checks 2 and 4 compare against an OCR reading of the
    page, not a real text layer, so a FAIL there can be OCR misreading the scan as
    much as the model missing text -- it still needs a person to look.
    """
    heads = list(PAGE_HEAD.finditer(log))
    pages = []
    for i, h in enumerate(heads):
        chunk = log[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(log)]
        scanned = bool(re.search(r"text layer: 0 chars", chunk))
        checks = {}
        for m in CHECK_LINE.finditer(chunk):
            key = CHECK_KEYS.get(m.group(2).strip(), m.group(2).strip())
            checks[key] = {"result": m.group(3), "detail": m.group(4).strip(" ()") or None}
        pages.append({"page": int(h.group(1)), "scanned": scanned, "checks": checks})
    return pages


def check_problems(pages: list) -> list:
    out = []
    for p in pages:
        for key, c in p["checks"].items():
            if key != "geometry" and c["result"] == "FAIL":
                against = "an OCR reading of the scan" if p["scanned"] else "the PDF's text layer"
                out.append(f"Page {p['page']}: {key.replace('_', ' ')} check failed "
                           f"({c['detail'] or 'no detail'}), compared against {against}.")
    return out


def save_job(job: dict) -> None:
    keep = {k: v for k, v in job.items() if k not in ("cancel", "pid")}
    tmp = job_dir(job["id"]) / "job.json.tmp"
    tmp.write_text(json.dumps(keep, indent=1))
    tmp.replace(job_dir(job["id"]) / "job.json")


def finish_job(job: dict, rc, timed_out: bool) -> None:
    log = read_log(job["id"])
    outputs = list_outputs(job["id"])
    problems = [msg for rx, msg in PROBLEMS if rx.search(log)]
    pages = page_checks(log) if ROUTES[job["route"]]["script"] == "test_pdf.py" else []
    problems += check_problems(pages)
    job.update(outputs=outputs, summary=summarize(log), problems=problems, pages=pages,
               finished=time.time(), exit_code=rc)
    if job.get("cancel"):
        job["state"], job["error"] = "cancelled", "Cancelled."
    elif timed_out:
        job["state"], job["error"] = "failed", f"Stopped after {JOB_TIMEOUT_S} s (the time limit)."
    elif not outputs:
        # test_pdf.py can exit 0 without writing a file, so success is decided by
        # the file existing, not by the exit code.
        job["state"] = "failed"
        why = FAILURE_REASON.search(log)
        if why:
            job["error"] = why.group(1).strip().rstrip(")")[:240]
        else:
            job["error"] = problems[0] if problems else "The converter produced no file."
    elif rc != 0 or problems:
        job["state"] = "review"
    else:
        job["state"] = "done"
    save_job(job)


# ---------------------------------------------------------------------------
# running jobs
# ---------------------------------------------------------------------------
def kill_group(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_job(job: dict) -> None:
    jd = job_dir(job["id"])
    rc, timed_out = None, False
    try:
        with open(jd / "log.txt", "wb") as log:
            # start_new_session: the converter and anything it starts form one
            # process group, so cancelling stops all of them.
            proc = subprocess.Popen(build_command(job), cwd=jd, env=job_env(),
                                    stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            job["pid"] = proc.pid
            deadline = time.time() + JOB_TIMEOUT_S
            while True:
                try:
                    rc = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if job.get("cancel"):
                        kill_group(proc)
                        break
                    if time.time() > deadline:
                        timed_out = True
                        kill_group(proc)
                        break
    except Exception as exc:
        with open(jd / "log.txt", "a") as fh:
            fh.write(f"\nCould not start the converter: {exc}\n")
    with JOBS_LOCK:
        finish_job(job, rc, timed_out)


def dispatcher() -> None:
    """Start queued jobs oldest first, within the model / no-model limits."""
    while True:
        with JOBS_LOCK:
            running = [j for j in JOBS.values() if j["state"] == "running"]
            n_model = sum(1 for j in running if j["model"])
            n_other = len(running) - n_model
            for j in sorted((j for j in JOBS.values() if j["state"] == "queued"),
                            key=lambda j: j["created"]):
                kind = "model" if j["model"] else "other"
                if (n_model if j["model"] else n_other) >= LIMITS[kind]:
                    continue
                if j["model"]:
                    n_model += 1
                else:
                    n_other += 1
                j["state"], j["started"] = "running", time.time()
                save_job(j)
                threading.Thread(target=run_job, args=(j,), daemon=True).start()
        time.sleep(0.3)


def load_old_jobs() -> None:
    """Reload jobs from disk after a restart; delete finished ones past KEEP_HOURS."""
    cutoff = time.time() - KEEP_HOURS * 3600
    for jf in JOBS_DIR.glob("*/job.json"):
        try:
            job = json.loads(jf.read_text())
        except (OSError, ValueError):
            continue
        if job.get("state") in ("queued", "running"):
            # The process that ran it is gone with the old service.
            job.update(state="failed", error="The service restarted while this job was waiting or running.",
                       finished=time.time())
            save_job(job)
        if (job.get("finished") or job.get("created", 0)) < cutoff:
            shutil.rmtree(jf.parent, ignore_errors=True)
            continue
        JOBS[job["id"]] = job


# ---------------------------------------------------------------------------
# the API
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    load_old_jobs()
    threading.Thread(target=dispatcher, daemon=True).start()
    yield


app = FastAPI(title="idox conversion API", version="1.0",
              description="Upload a file, get a job, poll it, download the result. "
                          "Model conversions take about a minute per page on the local 2B.",
              lifespan=lifespan)


def public_job(job: dict) -> dict:
    out = {k: v for k, v in job.items() if k not in ("cancel", "pid")}
    out["progress"] = (progress_of(job, read_log(job["id"]))
                       if job["state"] == "running" else None)
    out["files"] = [dict(f, url=f"/jobs/{job['id']}/files/{f['name']}")
                    for f in job.get("outputs", [])]
    out.pop("outputs", None)
    return out


def get_job(jid: str) -> dict:
    if not JOB_ID_RE.match(jid) or jid not in JOBS:
        raise HTTPException(404, "No such job.")
    return JOBS[jid]


def safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "").stem).strip("._")
    return stem[:80] or "document"


async def create_job(route_id: str, upload: UploadFile, options: dict) -> dict:
    route = ROUTES[route_id]
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in route["exts"]:
        raise HTTPException(415, f"Expected a {' or '.join(route['exts'])} file.")

    jid = uuid.uuid4().hex[:12]
    jd = job_dir(jid)
    (jd / "in").mkdir(parents=True)
    (jd / "out").mkdir()
    stem = safe_stem(upload.filename)
    dest = jd / "in" / f"{stem}{ext}"
    size, head = 0, b""
    try:
        with open(dest, "wb") as fh:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, f"The file is larger than {MAX_UPLOAD // (1024 * 1024)} MB.")
                if len(head) < 16:
                    head += chunk[:16]
                fh.write(chunk)
        if size == 0:
            raise HTTPException(400, "The file is empty.")
        if not head.startswith(route["magic"]):
            raise HTTPException(415, f"The file is not a real {ext} file (its contents do not match).")
    except HTTPException:
        shutil.rmtree(jd, ignore_errors=True)
        raise

    job = {"id": jid, "route": route_id, "filename": (upload.filename or "")[:120],
           "stem": stem, "ext": ext, "size": size, "options": options,
           "model": route["model"], "state": "queued", "created": time.time(),
           "started": None, "finished": None, "error": None,
           "summary": [], "problems": [], "outputs": []}
    with JOBS_LOCK:
        JOBS[jid] = job
        save_job(job)
    return public_job(job)


@app.get("/", include_in_schema=False)
def root():
    """The bare address has nothing of its own; send people to the API page."""
    return RedirectResponse("/docs")


@app.get("/health")
def health():
    """Is the model server answering, and are the external tools installed?"""
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=3,
                         headers={"Authorization": f"Bearer {os.environ['IDOX_API_KEY']}"}
                         if os.environ.get("IDOX_API_KEY") else {})
        model = {"url": BASE_URL, "ok": r.ok, "status": r.status_code}
    except requests.RequestException as exc:
        model = {"url": BASE_URL, "ok": False, "error": type(exc).__name__}
    with JOBS_LOCK:
        counts = {}
        for j in JOBS.values():
            counts[j["state"]] = counts.get(j["state"], 0) + 1
    return {"model_server": model,
            "tools": {"soffice": bool(shutil.which("soffice") or shutil.which("libreoffice")),
                      "tesseract": bool(shutil.which("tesseract"))},
            "jobs": counts}


@app.post("/convert/pdf-to-word", status_code=202)
async def pdf_to_word(
    file: UploadFile = File(..., description="The PDF to convert."),
    # Defaults to "all" rather than empty: the /docs page fills an empty text box
    # with the placeholder "string" and sends it, which is refused as a page range.
    pages: str = Form("all", description="all, or e.g. 1, 1-3 or 2,4."),
    scan_mode: Literal["text", "image", "both"] = Form(
        "text", description="Pages with no text layer (scans): 'text' reads them with the "
                            "model into editable text; 'image' embeds the page picture, "
                            "no model, not editable; 'both' writes the text file plus a "
                            "second <name>_scan.docx with the pictures."),
):
    """PDF -> Word (.docx). Every page is read by the vision model, digital and scanned.

    Returns a job at once (202). Poll GET /jobs/{id} until state is done, review or
    failed, then download the .docx from the job's files. A Markdown copy of what the
    model read (.md) is also produced.
    """
    options = {"scan_mode": scan_mode}
    pages = (pages or "").replace(" ", "").lower()
    if pages and pages != "all":
        if not PAGES_RE.match(pages):
            raise HTTPException(422, f"pages should be all, or look like 1, 1-3 or 2,4 "
                                     f"(got {pages[:20]!r})")
        options["pages"] = pages
    return await create_job("pdf_docx", file, options)


@app.get("/jobs/{jid}")
def job_status(jid: str):
    with JOBS_LOCK:
        return public_job(get_job(jid))


@app.get("/jobs/{jid}/files/{name:path}")
def job_file(jid: str, name: str):
    with JOBS_LOCK:
        job = get_job(jid)
        names = {f["name"] for f in job.get("outputs", [])}
    # Only files the job itself listed can be served, so a crafted name cannot
    # reach anything outside the job's out/ folder.
    if name not in names:
        raise HTTPException(404, "No such file in this job.")
    return FileResponse(job_dir(jid) / "out" / name, filename=Path(name).name)


@app.get("/jobs/{jid}/log", response_class=PlainTextResponse)
def job_log(jid: str):
    with JOBS_LOCK:
        get_job(jid)
    return read_log(jid)


@app.delete("/jobs/{jid}")
def delete_job(jid: str):
    """Cancel a queued or running job; delete a finished one and its files."""
    with JOBS_LOCK:
        job = get_job(jid)
        if job["state"] == "queued":
            job.update(state="cancelled", error="Cancelled.", finished=time.time())
            save_job(job)
            return {"cancelled": jid}
        if job["state"] == "running":
            job["cancel"] = True             # run_job stops the process group
            return {"cancelling": jid}
        JOBS.pop(jid, None)
    shutil.rmtree(job_dir(jid), ignore_errors=True)
    return {"deleted": jid}
