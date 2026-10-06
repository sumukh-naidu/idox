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

    Every POST /convert/... uploads one file and answers 202 with a job.
    POST   /convert/pdf-to-word         PDF   -> .docx                    (model)
    POST   /convert/pdf-to-excel        PDF   -> .xlsx                    (model)
    POST   /convert/pdf-to-powerpoint   PDF   -> .pptx                    (model)
    POST   /convert/pdf-to-text         PDF   -> .txt                     (model for scanned pages only)
    POST   /convert/pdf-to-jpg          PDF   -> one .jpg per page        (no model)
    POST   /convert/pdf-to-jpeg         PDF   -> one .jpeg per page       (no model)
    POST   /convert/pdf-to-png          PDF   -> one .png per page        (no model)
    POST   /convert/pdf-to-tiff         PDF   -> one multi-page .tiff     (no model)
    POST   /convert/image-to-word       PNG/JPG -> editable .docx         (model)
    POST   /convert/image-to-word-picture  PNG/JPG -> .docx, picture only (no model)
    POST   /convert/image-to-excel      PNG/JPG -> .xlsx from a table     (model)
    POST   /convert/jpg-to-pdf          JPG   -> .pdf with editable text  (model + LibreOffice)
    POST   /convert/jpg-to-png          JPG   -> .png                     (no model)
    POST   /convert/jpg-to-tiff         JPG   -> .tiff                    (no model)
    POST   /convert/word-to-pdf         .docx -> .pdf                     (LibreOffice)
    POST   /convert/tiff-to-pdf         TIFF  -> searchable .pdf          (Tesseract)
    POST   /convert/tiff-to-pdf-picture TIFF  -> .pdf, picture only       (no model)

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

VALIDATION. Each conversion's form is a pydantic model (the "requests" section
below), so everything is checked before a job exists, and a bad request gets 422
naming the field: an unknown field, an option out of range, a malformed page range,
and the file itself -- its type, its size, and that its contents really are that type.

CONFIGURATION (environment variables, read at start)
    IDOX_BASE_URL       the model server (default http://10.0.3.66:8080, the Qwen3.6-35B on
                        the Mac mini; http://127.0.0.1:8090 is the local 2B)
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
    .venv/bin/uvicorn api.idox_api:app --host 127.0.0.1 --port 8000   (from the repo root)
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
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, ClassVar, Dict, List, Literal, Optional

import requests
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

REPO = Path(__file__).resolve().parent.parent      # the scripts live one folder up
PY = sys.executable

BASE_URL = os.environ.get("IDOX_BASE_URL", "http://10.0.3.66:8080").rstrip("/")
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

# One entry per conversion -- the 17 the web app (idox_app.py) runs, plus the two
# JPG scripts it does not offer, plus JPG -> PDF (searchable picture). "flag" marks test_pdf.py, which writes one Office
# format per flag; "extra" is fixed arguments; "needs" is programs it cannot run without;
# "result" is the file that must exist for the job to count, when there are by-products.
ROUTES = {
    "pdf_docx": dict(script="test_pdf.py", model=True, flag="--docx", out_ext="docx"),
    "pdf_xlsx": dict(script="test_pdf.py", model=True, flag="--xlsx", out_ext="xlsx"),
    "pdf_pptx": dict(script="test_pdf.py", model=True, flag="--pptx", out_ext="pptx"),
    # The model reads scanned pages only, but a job is queued as a model job either way.
    "pdf_txt": dict(script="pdf_to_txt.py", model=True),
    "pdf_jpg": dict(script="pdf_to_jpg.py", model=False, extra=["--ext", "jpg"]),
    "pdf_jpeg": dict(script="pdf_to_jpg.py", model=False, extra=["--ext", "jpeg"]),
    "pdf_png": dict(script="pdf_to_png.py", model=False),
    "pdf_tiff": dict(script="pdf_to_tiff.py", model=False),
    "img_docx": dict(script="image_to_word.py", model=True, result="{stem}.docx"),
    "img_docx_pic": dict(script="image_to_word.py", model=False, extra=["--mode", "image"]),
    "img_xlsx": dict(script="image_to_excel.py", model=True),
    "jpg_pdf": dict(script="image_to_pdf.py", model=True, needs=("soffice",)),
    "jpg_png": dict(script="jpg_to_png.py", model=False),
    "jpg_tiff": dict(script="jpg_to_tiff.py", model=False),
    "docx_pdf": dict(script="word_to_pdf.py", model=False, needs=("soffice",)),
    "tiff_pdf": dict(script="tiff_to_pdf.py", model=False, needs=("tesseract",)),
    "tiff_pdf_pic": dict(script="tiff_to_pdf.py", model=False, extra=["--no-text"]),
    # the same script for one JPG: the picture, fitted to A4, with a hidden text layer (no model)
    "jpg_pdf_search": dict(script="tiff_to_pdf.py", model=False, needs=("tesseract",), extra=["--fit-a4"]),
}

# The start of a valid file of each type, so a renamed file is refused at upload
# rather than failing inside the script.
MAGIC = {".pdf": (b"%PDF-",),
         ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
         ".png": (b"\x89PNG\r\n\x1a\n",),
         ".tif": (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"),
         ".tiff": (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"),
         ".docx": (b"PK\x03\x04",)}

# External programs a route cannot run without, and how to check for each.
TOOLS = {"soffice": lambda: bool(shutil.which("soffice") or shutil.which("libreoffice")),
         "tesseract": lambda: bool(shutil.which("tesseract"))}
TOOL_NAMES = {"soffice": "LibreOffice", "tesseract": "Tesseract"}

# Validated options and the converter flag each one becomes. scan_mode is test_pdf.py's
# and is added with its output flags.
OPTION_FLAGS = {"pages": "--pages", "dpi": "--dpi", "quality": "--quality",
                "compression": "--compression", "mode": "--mode"}

JOBS = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def build_command(job: dict) -> list:
    """The argument list for one job, run from the job's folder. Never a shell string."""
    r, o, stem = ROUTES[job["route"]], job["options"], job["stem"]
    cmd = [PY, "-u", str(REPO / r["script"]), f"in/{stem}{job['ext']}"]
    if r["model"]:
        cmd += ["--base-url", BASE_URL]
    if "flag" in r:
        # test_pdf.py: the Office file it is asked for, plus a Markdown copy of what
        # the model read.
        cmd += [r["flag"], f"out/{stem}.{r['out_ext']}", "--out", f"out/{stem}.md",
                "--scan-mode", o.get("scan_mode", "text")]
    else:
        # Every other script names its own files inside --outdir: out/<stem>.<ext>, or
        # out/<stem>/page_001.<ext> for one image per page.
        cmd += ["--outdir", "out"]
    cmd += r.get("extra", [])
    for key, flag in OPTION_FLAGS.items():
        if key in o:
            cmd += [flag, str(o[key])]
    return cmd


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
    (re.compile(r"NO TABLE DETECTED"), "No table was found in the image."),
    (re.compile(r"CONVERSION FAILED|COULD NOT READ|COULD NOT WRITE"), "The file could not be converted."),
    # word_to_pdf.py prints its verdict as "  verdict            FAIL"
    (re.compile(r"^\s+verdict\s+FAIL", re.M), "The script's own verification failed."),
]
SUMMARY_LINES = re.compile(
    r"(VERDICT|verdict|pages verified|pages written|pages converted|pages in PDF|frames in file|"
    r"content check|image check|word coverage|paragraphs kept|table cells kept|"
    r"UNVERIFIED|OCR agreement|self-consistency|OCR grounding|OCR coverage|"
    r"note:|NO TABLE|FAILED|could not be read|dropped|restored|corrected|merged)")
FAILURE_REASON = re.compile(
    r"(?:EXTRACTION FAILED|MODEL FAILED|Tier 1 FAILED to produce valid output|"
    r"COULD NOT OPEN|COULD NOT READ|COULD NOT WRITE|CONVERSION FAILED|NO TABLE DETECTED|"
    r"SKIPPED|Could not start the converter)[:( -]+(.+)")


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
    script = ROUTES[job["route"]]["script"]
    if script == "test_pdf.py":
        m = re.search(r"testing (\d+):", text)
        done = len(re.findall(r"^Tier 1 (?:--|FAILED)", text, re.M))
    elif script in ("pdf_to_jpg.py", "pdf_to_png.py"):
        m = re.search(r"converting (\d+) at", text)
        done = len(re.findall(r"^  page \d+: (?:\d+x\d+px|FAILED)", text, re.M))
    elif script == "pdf_to_txt.py":
        m = re.search(r"converting (\d+):", text)
        done = len(re.findall(r"^  page \d+: (?:digital|scanned|blank)", text, re.M))
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
CHECK_LINE = re.compile(r"^  (\d)\. ([A-Za-z -]+?)(?: \(advisory\))?:\s+(PASS|FAIL|N/A|differs|matches)\b[ \t]*(.*)$",
                        re.M)
CHECK_KEYS = {"self-consistency": "self_consistency", "text-layer grounding": "grounding",
              "geometry": "geometry", "content coverage": "coverage",
              # image_to_*.py: the same checks, against OCR because an image has no text layer
              "OCR grounding": "grounding", "OCR coverage": "coverage"}
CHECKED_SCRIPTS = ("test_pdf.py", "image_to_pdf.py", "image_to_word.py", "image_to_excel.py")
# image_to_*.py, when OCR read nothing or is missing: "  2/4. OCR grounding/coverage: N/A (why)"
OCR_NA_LINE = re.compile(r"^  2/4\. OCR grounding/coverage:\s+N/A\s*\((.+)\)\s*$", re.M)


def page_checks(log: str) -> list:
    """The model checks, per page, from the converter's "--- checks ---" lines.

    Check 3 (geometry) is advisory in test_pdf.py itself and never makes a job need
    review. On a scanned page or an image, checks 2 and 4 compare against an OCR
    reading, not a real text layer, so a FAIL there can be OCR misreading the picture
    as much as the model missing text -- it still needs a person to look.
    """
    heads = list(PAGE_HEAD.finditer(log))
    if not heads:
        # image_to_*.py: one image, no PAGE headings, always checked against OCR.
        checks = {CHECK_KEYS.get(m.group(2).strip(), m.group(2).strip()):
                  {"result": m.group(3), "detail": m.group(4).strip(" ()") or None}
                  for m in CHECK_LINE.finditer(log)}
        na = OCR_NA_LINE.search(log)
        if na:
            # Not a failure, but the text was NOT compared with anything -- say so.
            for key in ("grounding", "coverage"):
                checks[key] = {"result": "N/A", "detail": na.group(1)}
        return [{"page": 1, "scanned": True, "checks": checks}] if checks else []
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
                against = "an OCR reading of the picture" if p["scanned"] else "the PDF's text layer"
                out.append(f"Page {p['page']}: {key.replace('_', ' ')} check failed "
                           f"({c['detail'] or 'no detail'}), compared against {against}.")
    return out


def save_job(job: dict) -> None:
    keep = {k: v for k, v in job.items() if k not in ("cancel", "pid")}
    tmp = job_dir(job["id"]) / "job.json.tmp"
    tmp.write_text(json.dumps(keep, indent=1))
    tmp.replace(job_dir(job["id"]) / "job.json")


def has_result(job: dict, outputs: list) -> bool:
    """Is the file the caller asked for there, not just a by-product? test_pdf.py always
    writes its Markdown copy and image_to_word.py --mode both its _scan.docx, even when
    the model failed and the real result was never written."""
    r = ROUTES[job["route"]]
    result = f"{job['stem']}.{r['out_ext']}" if "flag" in r else r.get("result", "").format(stem=job["stem"])
    return not result or result in {f["name"] for f in outputs}


def finish_job(job: dict, rc, timed_out: bool) -> None:
    log = read_log(job["id"])
    outputs = list_outputs(job["id"])
    problems = [msg for rx, msg in PROBLEMS if rx.search(log)]
    pages = page_checks(log) if ROUTES[job["route"]]["script"] in CHECKED_SCRIPTS else []
    problems += check_problems(pages)
    job.update(outputs=outputs, summary=summarize(log), problems=problems, pages=pages,
               finished=time.time(), exit_code=rc)
    if job.get("cancel"):
        job["state"], job["error"] = "cancelled", "Cancelled."
    elif timed_out:
        job["state"], job["error"] = "failed", f"Stopped after {JOB_TIMEOUT_S} s (the time limit)."
    elif not outputs or not has_result(job, outputs):
        # test_pdf.py can exit 0 without writing a file, so success is decided by
        # the file existing, not by the exit code.
        job["state"] = "failed"
        why = FAILURE_REASON.search(log)
        if why:
            job["error"] = why.group(1).strip().rstrip(")").lstrip("- ")[:240]
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


# ---------------------------------------------------------------------------
# requests -- one pydantic model per conversion form. extra="forbid" refuses a field
# the endpoint does not list (a typo such as "dpii" fails loudly instead of being
# ignored), and the file is checked in the model too, so a request either passes
# every check or gets 422 naming the field that failed.
# ---------------------------------------------------------------------------
def check_upload(f: UploadFile, exts: tuple) -> UploadFile:
    """The upload has an accepted name, is not empty or too large, and really is that
    type: its first bytes match, and a .docx is a zip with a Word document inside."""
    ext = Path(f.filename or "").suffix.lower()
    if ext not in exts:
        raise ValueError(f"expected a {' or '.join(exts)} file, got {(f.filename or '')[:60]!r}")
    f.file.seek(0, os.SEEK_END)
    size = f.file.tell()
    f.file.seek(0)
    if size == 0:
        raise ValueError("the file is empty")
    if size > MAX_UPLOAD:
        raise ValueError(f"the file is larger than {MAX_UPLOAD // (1024 * 1024)} MB")
    head = f.file.read(16)
    f.file.seek(0)
    if not head.startswith(MAGIC[ext]):
        raise ValueError(f"the file is not a real {ext} file (its contents do not match)")
    if ext == ".docx":
        try:
            ok = "word/document.xml" in zipfile.ZipFile(f.file).namelist()
        except zipfile.BadZipFile:
            ok = False
        f.file.seek(0)
        if not ok:
            raise ValueError("the file is not a real .docx file (no Word document inside)")
    return f


def page_range(pages: str) -> str:
    """'all', or a range such as 1, 1-3 or 2,4 with pages from 1 and ranges running forwards."""
    pages = (pages or "").replace(" ", "").lower()
    if not pages or pages == "all":
        return "all"
    if not PAGES_RE.match(pages):
        raise ValueError(f"pages should be all, or look like 1, 1-3 or 2,4 (got {pages[:20]!r})")
    for part in pages.split(","):
        first, _, last = part.partition("-")
        if int(first) < 1 or (last and int(last) < int(first)):
            raise ValueError(f"page range {part!r} is not valid: pages start at 1 and a range runs forwards")
    return pages


# Defaults to "all" rather than empty: the /docs page fills an empty text box with the
# placeholder "string" and sends it, which is refused as a page range.
PageRange = Annotated[str, AfterValidator(page_range),
                      Field(description="all, or e.g. 1, 1-3 or 2,4.")]
Dpi = Annotated[int, Field(ge=30, le=600, description="Resolution, 30-600. 150 is screen "
                                                      "quality; 300 is print quality (about 4x the size).")]
Compression = Annotated[Literal["lzw", "deflate"], Field(
    description="Lossless compression: 'lzw' opens everywhere; 'deflate' is smaller, but a "
                "few old viewers cannot open it.")]


class Conversion(BaseModel):
    """One uploaded file; subclasses add the options a conversion takes."""
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    EXTS: ClassVar[tuple] = ()
    file: UploadFile

    @field_validator("file")
    @classmethod
    def _real_file(cls, f: UploadFile) -> UploadFile:
        return check_upload(f, cls.EXTS)

    def options(self) -> dict:
        """The options as a job keeps them. All pages is the converters' own default."""
        o = self.model_dump(exclude={"file"})
        if o.get("pages") == "all":
            del o["pages"]
        return o


class PdfFile(Conversion):
    EXTS = (".pdf",)
    file: UploadFile = Field(description="The PDF to convert.")


class ImageFile(Conversion):
    # PNG and JPG only: blocks.py labels any other image as PNG for the model.
    EXTS = (".png", ".jpg", ".jpeg")
    file: UploadFile = Field(description="The PNG or JPG image to convert.")


class JpgFile(Conversion):
    EXTS = (".jpg", ".jpeg")
    file: UploadFile = Field(description="The JPG / JPEG image to convert.")


class TiffFile(Conversion):
    EXTS = (".tif", ".tiff")
    file: UploadFile = Field(description="The TIFF to convert (one or more frames).")


class WordFile(Conversion):
    EXTS = (".docx",)
    file: UploadFile = Field(description="The Word document (.docx) to convert.")


class PdfToWord(PdfFile):
    pages: PageRange = "all"
    scan_mode: Literal["text", "image", "both"] = Field(
        "text", description="Pages with no text layer (scans): 'text' reads them with the "
                            "model into editable text; 'image' embeds the page picture, "
                            "no model, not editable; 'both' writes the text file plus a "
                            "second <name>_scan.docx with the pictures.")


class PdfToOffice(PdfFile):
    """Excel and PowerPoint. No 'both': test_pdf.py writes the separate _scan file only
    for Word, so here 'both' would silently behave like 'text'."""
    pages: PageRange = "all"
    scan_mode: Literal["text", "image"] = Field(
        "text", description="Pages with no text layer (scans): 'text' reads them with the "
                            "model into editable text; 'image' puts the page picture in "
                            "instead, no model, not editable.")


class PdfPages(PdfFile):
    pages: PageRange = "all"


class PdfToJpg(PdfFile):
    pages: PageRange = "all"
    dpi: Dpi = 150
    quality: int = Field(90, ge=1, le=100, description="JPG quality, 1-100. Higher is sharper and larger.")


class PdfToPng(PdfFile):
    pages: PageRange = "all"
    dpi: Dpi = 150


class PdfToTiff(PdfFile):
    pages: PageRange = "all"
    dpi: Dpi = 300
    compression: Compression = "lzw"


class ImageToWord(ImageFile):
    mode: Literal["text", "both"] = Field(
        "text", description="'text' writes editable text and tables; 'both' also writes "
                            "<name>_scan.docx with the picture, as a separate file.")


class JpgToTiff(JpgFile):
    compression: Compression = "lzw"


# ---------------------------------------------------------------------------
# responses
# ---------------------------------------------------------------------------
class JobFile(BaseModel):
    name: str
    size: int
    url: str


class Progress(BaseModel):
    done: int
    total: int


class Job(BaseModel):
    id: str
    route: str
    filename: str
    ext: str
    size: int
    options: dict
    model: bool = Field(description="Whether the job uses the model server.")
    state: Literal["queued", "running", "done", "review", "failed", "cancelled"]
    created: float
    started: Optional[float] = None
    finished: Optional[float] = None
    error: Optional[str] = None
    progress: Optional[Progress] = Field(None, description="Pages done, while running.")
    summary: List[str] = []
    problems: List[str] = Field([], description="Read these before trusting a 'review' job.")
    pages: List[dict] = Field([], description="Per-page model checks, for model conversions.")
    exit_code: Optional[int] = None
    files: List[JobFile] = []


# Shapes for the pages that were documented only as "200 OK": they describe what is returned in Swagger and
# validate nothing. The endpoints return exactly what they returned before.
class ModelServer(BaseModel):
    url: str = Field(description="Where the converters send their page images.")
    ok: bool = Field(description="True when the model server answered /health.")
    status: Optional[int] = Field(None, description="The model server's HTTP status, when it answered.")
    error: Optional[str] = Field(None, description="Why it could not be reached, when it did not answer.")


class Health(BaseModel):
    model_server: ModelServer
    tools: Dict[str, bool] = Field(description="Whether each external tool is installed: soffice, tesseract.")
    jobs: Dict[str, int] = Field(description="How many jobs are in each state, e.g. {\"done\": 3}.")

    model_config = {"json_schema_extra": {"examples": [{
        "model_server": {"url": "http://127.0.0.1:8080", "ok": True, "status": 200},
        "tools": {"soffice": True, "tesseract": True}, "jobs": {"done": 3, "review": 1}}]}}


class DeleteResult(BaseModel):
    """Exactly one field is present."""
    cancelled: Optional[str] = Field(None, description="Job id of a queued job that was cancelled.")
    cancelling: Optional[str] = Field(None, description="Job id of a running job that is being stopped.")
    deleted: Optional[str] = Field(None, description="Job id of a finished job whose files were deleted.")

    model_config = {"json_schema_extra": {"examples": [{"deleted": "a1b2c3d4e5f6"}]}}


class ErrorDetail(BaseModel):
    detail: str = Field(description="What went wrong, in words.")

    model_config = {"json_schema_extra": {"examples": [{"detail": "No such job."}]}}


NOT_FOUND = {404: {"model": ErrorDetail, "description": "No job with that id."}}


def safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "").stem).strip("._")
    return stem[:80] or "document"


async def create_job(route_id: str, body: Conversion) -> dict:
    """A queued job for a request its model has already validated."""
    route = ROUTES[route_id]
    missing = [TOOL_NAMES[t] for t in route.get("needs", ()) if not TOOLS[t]()]
    if missing:
        raise HTTPException(503, f"This conversion needs {', '.join(missing)}, which is not "
                                 f"installed on the server.")
    upload = body.file
    ext = Path(upload.filename).suffix.lower()

    jid = uuid.uuid4().hex[:12]
    jd = job_dir(jid)
    (jd / "in").mkdir(parents=True)
    (jd / "out").mkdir()
    stem = safe_stem(upload.filename)
    size = 0
    with open(jd / "in" / f"{stem}{ext}", "wb") as fh:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            fh.write(chunk)

    job = {"id": jid, "route": route_id, "filename": (upload.filename or "")[:120],
           "stem": stem, "ext": ext, "size": size, "options": body.options(),
           "model": route["model"], "state": "queued", "created": time.time(),
           "started": None, "finished": None, "error": None,
           "summary": [], "problems": [], "outputs": []}
    with JOBS_LOCK:
        JOBS[jid] = job
        save_job(job)
    return public_job(job)


UI_PAGE = Path(__file__).resolve().parent / "idox_api_ui.html"


@app.get("/", include_in_schema=False)
def root():
    """The bare address opens the test page; /docs is the Swagger page."""
    return RedirectResponse("/ui")


@app.get("/ui", include_in_schema=False)
def ui():
    """A page with one button per conversion, built from /openapi.json, for testing by hand."""
    # no-cache: the browser asks the server each time (cheap, the file has an ETag), so an updated page is never stale.
    return FileResponse(UI_PAGE, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/health", responses={200: {"model": Health, "description": "Model server, tools and job counts."}})
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
            "tools": {name: ok() for name, ok in TOOLS.items()},
            "jobs": counts}


# Every conversion: the form is validated by its request model, then a job is queued.
# Each returns 202 with the job at once; poll GET /jobs/{id} until state is done,
# review or failed, then download the result from the job's files.
JOB_HELP = ("Returns a job at once (202). Poll GET /jobs/{id} until state is done, review "
            "or failed, then download the result from the job's files.")


def conversion(group: str, path: str, route_id: str, model: type, title: str, about: str):
    """Register POST /convert/<path> for one route, its form validated by `model`. The
    x- fields tell the test page (/ui) what to accept and what the route needs."""
    async def endpoint(body: Annotated[model, Form()]):
        return await create_job(route_id, body)
    endpoint.__name__ = route_id
    route = ROUTES[route_id]
    app.post(f"/convert/{path}", status_code=202, response_model=Job, tags=[group],
             summary=title, description=f"{about}\n\n{JOB_HELP}",
             openapi_extra={"x-accept": list(model.EXTS), "x-uses-model": route["model"],
                            "x-needs": [TOOL_NAMES[t] for t in route.get("needs", ())]})(endpoint)


PDF, IMAGE, OTHER = "From PDF", "From an image", "From Word or TIFF"
conversion(PDF, "pdf-to-word", "pdf_docx", PdfToWord, "PDF → Word",
           "PDF -> Word (.docx). Every page is read by the vision model, digital and scanned. "
           "A Markdown copy of what the model read (.md) is also produced.")
conversion(PDF, "pdf-to-excel", "pdf_xlsx", PdfToOffice, "PDF → Excel",
           "PDF -> Excel (.xlsx). Every page is read by the vision model and goes into one "
           "sheet: tables as grids, text in column A. Clear numbers in tables are real numbers shown exactly as "
           "printed; IDs, dates, phone numbers and anything doubtful stay text. A Markdown "
           "copy of what the model read (.md) is also produced.")
conversion(PDF, "pdf-to-powerpoint", "pdf_pptx", PdfToOffice, "PDF → PowerPoint",
           "PDF -> PowerPoint (.pptx). One slide per page; the first heading on a page becomes "
           "its slide title. Every page is read by the vision model, digital and scanned. A "
           "Markdown copy of what the model read (.md) is also produced.")
conversion(PDF, "pdf-to-text", "pdf_txt", PdfPages, "PDF → Text",
           "PDF -> plain text (.txt), page by page. Pages with real text are taken from the "
           "PDF and checked against it; scanned pages are read by the model and marked "
           "UNVERIFIED, because there is no text layer to check them against.")
conversion(PDF, "pdf-to-jpg", "pdf_jpg", PdfToJpg, "PDF → JPG",
           "PDF -> JPG images, one per page. No model: each page is drawn exactly as a PDF "
           "viewer shows it, about a second per page. The files are <name>/page_001.jpg, ... "
           "Each JPG is read back and checked against the PDF (size, not blank, every text "
           "line on ink, an OCR word check), so a job is 'review' if a page fails.")
conversion(PDF, "pdf-to-jpeg", "pdf_jpeg", PdfToJpg, "PDF → JPEG",
           "PDF -> JPEG images, one per page. The same as pdf-to-jpg, with the .jpeg file "
           "ending: <name>/page_001.jpeg, ... No model.")
conversion(PDF, "pdf-to-png", "pdf_png", PdfToPng, "PDF → PNG",
           "PDF -> PNG images, one per page, lossless. No model. The files are "
           "<name>/page_001.png, ... Each is checked pixel by pixel against the PDF.")
conversion(PDF, "pdf-to-tiff", "pdf_tiff", PdfToTiff, "PDF → TIFF",
           "PDF -> one multi-page TIFF (<name>.tiff), one frame per page, lossless, 300 dpi by "
           "default. No model. Every frame is checked pixel by pixel against the PDF.")

conversion(IMAGE, "image-to-word", "img_docx", ImageToWord, "Image → Word",
           "Image (PNG/JPG) -> Word (.docx) with editable text and tables, read by the vision "
           "model. An image has no text layer, so the reading is checked against an OCR "
           "reading instead; a failed check makes the job 'review'.")
conversion(IMAGE, "image-to-word-picture", "img_docx_pic", ImageFile, "Image → Word (picture)",
           "Image (PNG/JPG) -> Word (.docx) with the picture placed in unchanged. No model, "
           "not editable.")
conversion(IMAGE, "image-to-excel", "img_xlsx", ImageFile, "Image → Excel",
           "Image (PNG/JPG) of a table -> Excel (.xlsx), read by the vision model and checked "
           "against an OCR reading. If the image has no table, nothing is written and the "
           "job fails with 'No table was found'.")
conversion(IMAGE, "jpg-to-pdf", "jpg_pdf", JpgFile, "JPG → PDF",
           "JPG -> PDF, with editable text. The vision model reads the image (text, headings, "
           "tables) and the page is rebuilt from that reading as a text PDF, via Word and "
           "LibreOffice. This is NOT the picture placed on a page: the PDF contains the "
           "model's reading, so it can change letters or miss content. The reading is checked "
           "against an OCR reading of the image; a failed check makes the job 'review'.")
conversion(IMAGE, "jpg-to-pdf-searchable", "jpg_pdf_search", JpgFile, "JPG → PDF (searchable picture)",
           "JPG -> PDF that SHOWS THE ORIGINAL PICTURE, fitted to an A4 page, with a hidden text layer read by "
           "Tesseract OCR so it can be searched and copied. No model. Unlike 'JPG -> PDF' this does not "
           "re-type the page: the picture, its fonts and layout are exactly the JPG; only the hidden text is "
           "an OCR reading and it can miss text such as white-on-dark lettering.")
conversion(IMAGE, "jpg-to-png", "jpg_png", JpgFile, "JPG → PNG",
           "JPG -> PNG (<name>.png). No model. The PNG is read back and checked against the JPG.")
conversion(IMAGE, "jpg-to-tiff", "jpg_tiff", JpgToTiff, "JPG → TIFF",
           "JPG -> TIFF (<name>.tiff), lossless. No model. The TIFF is read back and checked "
           "against the JPG.")

conversion(OTHER, "word-to-pdf", "docx_pdf", WordFile, "Word → PDF",
           "Word (.docx) -> PDF, rendered by LibreOffice. No model. The PDF is then checked "
           "against the Word file: word coverage, paragraphs and table cells kept.")
conversion(OTHER, "tiff-to-pdf", "tiff_pdf", TiffFile, "TIFF → PDF (searchable)",
           "TIFF -> searchable PDF. Each frame becomes a page, unchanged, with a hidden text "
           "layer read by Tesseract OCR so it can be searched and copied. No model.")
conversion(OTHER, "tiff-to-pdf-picture", "tiff_pdf_pic", TiffFile, "TIFF → PDF (picture)",
           "TIFF -> PDF, picture only. Each frame becomes a page, unchanged, with no text "
           "layer. No model.")


_default_openapi = app.openapi


def openapi_with_uploads() -> dict:
    """FastAPI does not notice an UploadFile inside a Form model, so it describes these
    forms as url-encoded and Swagger's "Try it out" shows the file as a text box. Each
    form carries a file, so describe it as the multipart upload it really is."""
    if app.openapi_schema is None:
        spec = _default_openapi()
        for path, ops in spec["paths"].items():
            content = ops.get("post", {}).get("requestBody", {}).get("content", {})
            if path.startswith("/convert/") and "application/x-www-form-urlencoded" in content:
                content["multipart/form-data"] = content.pop("application/x-www-form-urlencoded")
    return app.openapi_schema


app.openapi = openapi_with_uploads


@app.get("/jobs/{jid}", response_model=Job, responses=NOT_FOUND)
def job_status(jid: str):
    """The job: its state, progress, problems and the files it produced."""
    with JOBS_LOCK:
        return public_job(get_job(jid))


@app.get("/jobs/{jid}/files/{name:path}",
         responses={200: {"description": "The file itself, sent as a download.",
                          "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
                    404: {"model": ErrorDetail, "description": "No such job, or the job has no file with that name."}})
def job_file(jid: str, name: str):
    """Download one of the files listed in the job's `files`."""
    with JOBS_LOCK:
        job = get_job(jid)
        names = {f["name"] for f in job.get("outputs", [])}
    # Only files the job itself listed can be served, so a crafted name cannot
    # reach anything outside the job's out/ folder.
    if name not in names:
        raise HTTPException(404, "No such file in this job.")
    return FileResponse(job_dir(jid) / "out" / name, filename=Path(name).name)


@app.get("/jobs/{jid}/log", response_class=PlainTextResponse,
         responses={200: {"description": "The converter's full log as plain text.", "content": {"text/plain": {"example": "Tier 1 -- 8 blocks in 15.1s"}}},
                    404: {"description": "No job with that id.",
                          "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorDetail"}}}}})
def job_log(jid: str):
    """The converter's own output for this job, as plain text."""
    with JOBS_LOCK:
        get_job(jid)
    return read_log(jid)


@app.delete("/jobs/{jid}", responses={200: {"model": DeleteResult, "description": "What was done to the job."}, **NOT_FOUND})
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
