#!/usr/bin/env python3
"""
idox_app.py -- a small local web app: upload a file, pick a conversion, download
the result.

    .venv/bin/python idox_app.py            then open  http://127.0.0.1:8765

WHY A SERVER AT ALL. Every converter here is a Python script, and a web page cannot
run Python. This file is the missing piece: it serves idox_app.html, receives the
upload, runs the SAME scripts as subprocesses, and hands back the files. It adds no
conversion logic of its own. Standard library only, so there is nothing to install.

WHAT IT DOES FOR EACH JOB
  - copies the upload into its own folder (outside the repo, by default
    ~/.local/share/idox/jobs/<id>/), so two jobs never share a working directory;
  - runs the right script from that folder, as an argument LIST (no shell);
  - collects what the script wrote, and reads its log for the outcome, because
    several scripts exit 0 even when a page failed;
  - offers each output file for download, or all of them as one .zip.

THE MODEL. Conversions that read pages with the vision model call whichever engine is
chosen in Settings: the tuhin-ai API (a remote, authenticated, OpenAI-style server),
or the local 2B model. The API key is held in this process's memory only. It reaches
the scripts through their environment (IDOX_API_KEY), never on a command line, never
in job.json or the log, and never back to the browser. Restarting the app clears it
(or set TUHIN_AI_KEY before starting).

SAFETY. Binds to 127.0.0.1 only. Requests whose Host is not localhost, and writes
whose Origin is another site, are refused, so a web page you happen to have open
cannot drive the app or spend the key. File names are reduced to a safe stem; only
the routes listed in ROUTES can run; downloads are confined to the job's own folder.
"""

import argparse
import io
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

REPO = Path(__file__).resolve().parent
PY = sys.executable
PAGE = REPO / "idox_app.html"

LOCAL_BASE = "http://127.0.0.1:8090"
MAX_UPLOAD = 300 * 1024 * 1024
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
PAGES_RE = re.compile(r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$")

SOURCE_TYPES = {
    "pdf": {"pdf"},
    "image": {"png", "jpg", "jpeg", "bmp", "webp"},
    "docx": {"docx"},
    "tiff": {"tif", "tiff"},
}
TYPE_NAMES = {"pdf": "PDF", "image": "Image", "docx": "Word", "tiff": "TIFF"}

# Every conversion the app can run. "opts" lists the settings a job may carry.
ROUTES = {
    "pdf_docx": dict(src="pdf", label="Word (.docx)", model=True, script="test_pdf.py",
                     flag="--docx", ext="docx", opts=["pages", "scan_mode"],
                     note="Reads each page with the model and rebuilds it as an editable Word file."),
    "pdf_xlsx": dict(src="pdf", label="Excel (.xlsx)", model=True, script="test_pdf.py",
                     flag="--xlsx", ext="xlsx", opts=["pages", "scan_mode"],
                     note="Tables become sheet grids; text goes in column A. Every value is kept as text."),
    "pdf_pptx": dict(src="pdf", label="PowerPoint (.pptx)", model=True, script="test_pdf.py",
                     flag="--pptx", ext="pptx", opts=["pages", "scan_mode"],
                     note="One slide per page. The first heading becomes the slide title."),
    "pdf_txt": dict(src="pdf", label="Text (.txt)", model=True, script="pdf_to_txt.py",
                    opts=["pages"],
                    note="Pages with real text are checked against the PDF's own text. Scanned pages are read by the model and marked unverified."),
    "pdf_jpg": dict(src="pdf", label="JPG images", model=False, script="pdf_to_jpg.py",
                    extra=["--ext", "jpg"], opts=["pages", "dpi"],
                    note="Draws each page exactly. One image per page. No model."),
    "pdf_jpeg": dict(src="pdf", label="JPEG images (.jpeg)", model=False, script="pdf_to_jpg.py",
                     extra=["--ext", "jpeg"], opts=["pages", "dpi"],
                     note="The same pictures as JPG, with the .jpeg file ending."),
    "pdf_png": dict(src="pdf", label="PNG images", model=False, script="pdf_to_png.py",
                    opts=["pages", "dpi"],
                    note="Draws each page exactly, lossless. One image per page. Checked pixel by pixel against the PDF. No model."),
    "pdf_tiff": dict(src="pdf", label="TIFF (one multi-page file)", model=False,
                     script="pdf_to_tiff.py", opts=["pages", "dpi"],
                     note="Lossless, 300 dpi by default. Checked pixel by pixel against the PDF."),
    "img_docx": dict(src="image", label="Word (.docx)", model=True, script="image_to_word.py",
                     note="Reads the image with the model and writes editable text and tables."),
    "img_docx_pic": dict(src="image", label="Word, picture only", model=False,
                         script="image_to_word.py", extra=["--mode", "image"],
                         note="Puts the image into a Word file unchanged. No model, not editable."),
    "img_pdf": dict(src="image", label="PDF", model=True, script="image_to_pdf.py",
                    needs=["soffice"],
                    note="Reads the image with the model and builds a text PDF (via LibreOffice)."),
    "img_xlsx": dict(src="image", label="Excel (.xlsx)", model=True, script="image_to_excel.py",
                     note="Needs a table in the image. Writes nothing if none is found."),
    "docx_pdf": dict(src="docx", label="PDF", model=False, script="word_to_pdf.py",
                     needs=["soffice"],
                     note="LibreOffice renders it, then the result is checked against the Word file."),
    "tiff_pdf": dict(src="tiff", label="Searchable PDF", model=False, script="tiff_to_pdf.py",
                     needs=["tesseract"],
                     note="Each frame becomes a page, unchanged, with a hidden text layer so it can be searched."),
    "tiff_pdf_pic": dict(src="tiff", label="PDF, picture only", model=False,
                         script="tiff_to_pdf.py", extra=["--no-text"],
                         note="Each frame becomes a page, unchanged. No text layer."),
}

# ---------------------------------------------------------------------------
# settings (in memory only)
# ---------------------------------------------------------------------------

CONFIG = {
    "backend": "tuhin",                       # "tuhin" (remote API) or "local"
    "endpoint": "http://10.0.3.2:8080",       # tuhin-ai.local does not resolve on every machine
    "key": os.environ.get("TUHIN_AI_KEY", "").strip(),
    "thinking": False,
    "max_image_side": 1600,
    "timeout_s": 900,
}
CONFIG_LOCK = threading.Lock()
FALLBACK = "http://10.0.3.2:8080"
NAMED = "http://tuhin-ai.local:8080"

JOBS = {}
JOBS_LOCK = threading.Lock()
JOBS_DIR = None
LIMITS = {"tuhin": 2, "local": 1, "other": 3}     # the API doc: 2 requests run at once
JOB_TIMEOUT_S = 2 * 60 * 60


def clean_endpoint(text: str) -> str:
    """http(s)://host:port, without a trailing slash or /v1 (idox adds the path)."""
    t = (text or "").strip().rstrip("/")
    if t.lower().endswith("/v1"):
        t = t[:-3].rstrip("/")
    if not re.match(r"^https?://[^\s/]+(:\d+)?$", t):
        raise ValueError("The address should look like http://10.0.3.2:8080")
    return t


def effective_base() -> str:
    return LOCAL_BASE if CONFIG["backend"] == "local" else CONFIG["endpoint"]


def public_config() -> dict:
    with CONFIG_LOCK:
        c = dict(CONFIG)
    c["key_set"] = bool(c.pop("key"))          # the key itself never leaves the process
    c["named"] = NAMED
    c["fallback"] = FALLBACK
    c["local_base"] = LOCAL_BASE
    c["tools"] = {"soffice": bool(shutil.which("soffice") or shutil.which("libreoffice")),
                  "tesseract": bool(shutil.which("tesseract"))}
    return c


def job_env(model: bool) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("IDOX_") and k != "TUHIN_AI_KEY"}
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(REPO)
    if model:
        with CONFIG_LOCK:
            if CONFIG["backend"] == "tuhin":
                if CONFIG["key"]:
                    env["IDOX_API_KEY"] = CONFIG["key"]
                env["IDOX_THINKING"] = "on" if CONFIG["thinking"] else "off"
                env["IDOX_MAX_IMAGE_SIDE"] = str(CONFIG["max_image_side"])
                env["IDOX_TIMEOUT"] = str(CONFIG["timeout_s"])
    return env


def tool_ok(name: str) -> bool:
    if name == "soffice":
        return bool(shutil.which("soffice") or shutil.which("libreoffice"))
    return bool(shutil.which(name))


def routes_payload() -> list:
    out = []
    for rid, r in ROUTES.items():
        missing = [t for t in r.get("needs", []) if not tool_ok(t)]
        out.append({"id": rid, "src": r["src"], "label": r["label"], "model": r["model"],
                    "opts": r.get("opts", []), "note": r["note"],
                    "missing": missing})
    return out


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

def safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._-") or "file"
    return stem[:60]


def source_type(ext: str):
    for kind, exts in SOURCE_TYPES.items():
        if ext in exts:
            return kind
    return None


def clean_options(route: dict, opts: dict) -> dict:
    allowed, out = route.get("opts", []), {}
    pages = str(opts.get("pages", "") or "").replace(" ", "")
    if pages and "pages" in allowed:
        if not PAGES_RE.match(pages):
            raise ValueError("Pages should look like 1, 1-3 or 2,4")
        out["pages"] = pages
    dpi = str(opts.get("dpi", "") or "").strip()
    if dpi and "dpi" in allowed:
        if not dpi.isdigit() or not 30 <= int(dpi) <= 600:
            raise ValueError("Resolution should be a number from 30 to 600 dpi")
        out["dpi"] = int(dpi)
    scan = str(opts.get("scan_mode", "") or "").strip()
    if scan and "scan_mode" in allowed:
        if scan not in ("text", "image", "both"):
            raise ValueError("Scanned pages: choose text, image or both")
        out["scan_mode"] = scan
    return out


def build_command(job: dict) -> list:
    r, o = ROUTES[job["route"]], job["options"]
    stem, inp = job["stem"], f"in/{job['stem']}.{job['ext']}"
    script = str(REPO / r["script"])
    if r["script"] == "test_pdf.py":
        cmd = [PY, "-u", script, inp, "--base-url", job["base_url"],
               r["flag"], f"out/{stem}.{r['ext']}", "--out", f"out/{stem}.md",
               "--scan-mode", o.get("scan_mode", "text")]
        if o.get("pages"):
            cmd += ["--pages", o["pages"]]
        return cmd
    cmd = [PY, "-u", script, inp]
    if r["model"]:
        cmd += ["--base-url", job["base_url"]]
    cmd += ["--outdir", "out"] + r.get("extra", [])
    if o.get("pages"):
        cmd += ["--pages", o["pages"]]
    if o.get("dpi"):
        cmd += ["--dpi", str(o["dpi"])]
    return cmd


def job_dir(jid: str) -> Path:
    return JOBS_DIR / jid


def save_job(job: dict) -> None:
    keep = {k: v for k, v in job.items() if k not in ("cancel", "pid", "base_url")}
    tmp = job_dir(job["id"]) / "job.json.tmp"
    tmp.write_text(json.dumps(keep, indent=1))
    tmp.replace(job_dir(job["id"]) / "job.json")


def list_outputs(jid: str) -> list:
    out_dir, files = job_dir(jid) / "out", []
    if out_dir.is_dir():
        for p in sorted(out_dir.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                files.append({"name": p.relative_to(out_dir).as_posix(), "size": p.stat().st_size})
    return files


PROBLEMS = [
    (re.compile(r"VERDICT: FAIL"), "The script's own verification failed."),
    (re.compile(r"Tier 1 FAILED"), "The model failed on at least one page, which was skipped."),
    (re.compile(r"MODEL FAILED"), "The model failed on at least one page."),
    (re.compile(r"could not be read"), "At least one page could not be read."),
    (re.compile(r"NO TABLE DETECTED"), "No table was found in the image."),
    (re.compile(r"COULD NOT OPEN|SKIPPED:"), "The file could not be processed."),
    (re.compile(r"Traceback \(most recent call last\)"), "The script crashed."),
    (re.compile(r"EXTRACTION FAILED"), "The model could not read the file."),
]
SUMMARY_LINES = re.compile(
    r"(VERDICT|verdict|pages verified|pages written|pages converted|pages in PDF|frames in file|"
    r"content check|image check|word coverage|paragraphs kept|table cells kept|"
    r"UNVERIFIED|OCR agreement|self-consistency|OCR grounding|OCR coverage|"
    r"note:|NO TABLE|FAILED|could not be read|dropped|restored|corrected|merged)")


def read_tail(path: Path, nbytes: int = 65536) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def progress_of(job: dict, text: str):
    """(done, total) pages, read from the script's own progress lines, or None."""
    script = ROUTES[job["route"]]["script"]
    total = None
    if script == "test_pdf.py":
        m = re.search(r"testing (\d+):", text)
        done = len(re.findall(r"^Tier 1 (?:--|FAILED)", text, re.M))
    elif script == "pdf_to_txt.py":
        m = re.search(r"converting (\d+):", text)
        done = len(re.findall(r"^  page \d+: (?:digital|scanned|blank)", text, re.M))
    elif script in ("pdf_to_jpg.py", "pdf_to_png.py"):
        m = re.search(r"converting (\d+) at", text)
        done = len(re.findall(r"^  page \d+: \d+x\d+px", text, re.M))
    else:
        return None
    if m:
        total = int(m.group(1))
    return {"done": min(done, total), "total": total} if total else None


def summarize(text: str) -> list:
    seen, out = set(), []
    for line in text.splitlines():
        s = line.strip().lstrip("!~- ").strip()
        s = re.sub(r"^\d+(?:/\d+)?\.\s+", "", s)          # "1. self-consistency" -> "self-consistency"
        if s and SUMMARY_LINES.search(line) and s not in seen:
            seen.add(s)
            out.append(s[:200])
    return out[-14:]


def finish_job(job: dict, rc, timed_out: bool) -> None:
    log = (job_dir(job["id"]) / "log.txt").read_text("utf-8", "replace") if (job_dir(job["id"]) / "log.txt").exists() else ""
    outputs = list_outputs(job["id"])
    problems = [msg for rx, msg in PROBLEMS if rx.search(log)]
    job["outputs"], job["summary"], job["problems"] = outputs, summarize(log), problems
    job["finished"] = time.time()
    if job.get("cancel"):
        job["state"], job["error"] = "cancelled", "Cancelled."
    elif timed_out:
        job["state"], job["error"] = "failed", "Stopped: it ran longer than the time limit."
    elif not outputs:
        job["state"] = "failed"
        # The scripts print the real reason after a marker; show that, not the log.
        why = re.search(r"(?:EXTRACTION FAILED|MODEL FAILED|Tier 1 FAILED to produce valid output|"
                        r"COULD NOT OPEN|SKIPPED|CONVERSION FAILED|Could not start the converter)"
                        r"[:( ]+(.+)", log)
        if why:
            job["error"] = why.group(1).strip().rstrip(")")[:240]
        else:
            job["error"] = problems[0] if problems else "The converter produced no file."
            tail = [l.strip() for l in log.strip().splitlines()[-3:] if l.strip("= \n")]
            job["detail"] = " | ".join(tail)[:300]
    elif rc != 0 or problems:
        job["state"] = "review"
    else:
        job["state"] = "done"
    save_job(job)


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
            proc = subprocess.Popen(build_command(job), cwd=jd, env=job_env(job["model"]),
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
    except Exception as exc:                         # could not even start
        with open(jd / "log.txt", "a") as fh:
            fh.write(f"\nCould not start the converter: {exc}\n")
    with JOBS_LOCK:
        finish_job(job, rc, timed_out)


def dispatcher() -> None:
    """Start queued jobs, oldest first, keeping model jobs within the engine's limit."""
    while True:
        with JOBS_LOCK:
            running = [j for j in JOBS.values() if j["state"] == "running"]
            n_model = sum(1 for j in running if j["model"])
            n_other = len(running) - n_model
            model_limit = LIMITS["local"] if CONFIG["backend"] == "local" else LIMITS["tuhin"]
            for j in sorted((j for j in JOBS.values() if j["state"] == "queued"),
                            key=lambda j: j["created"]):
                if j["model"]:
                    if n_model >= model_limit:
                        continue
                    n_model += 1
                else:
                    if n_other >= LIMITS["other"]:
                        continue
                    n_other += 1
                j["state"], j["started"] = "running", time.time()
                j["base_url"] = effective_base()
                j["backend"] = CONFIG["backend"]
                save_job(j)
                threading.Thread(target=run_job, args=(j,), daemon=True).start()
        time.sleep(0.3)


def public_job(job: dict, with_log: bool = True) -> dict:
    out = {k: v for k, v in job.items() if k not in ("cancel", "pid", "base_url")}
    out["label"] = ROUTES[job["route"]]["label"]
    out["src_name"] = TYPE_NAMES[ROUTES[job["route"]]["src"]]
    if job["state"] in ("running", "queued") or with_log:
        text = read_tail(job_dir(job["id"]) / "log.txt")
        out["progress"] = progress_of(job, text) if job["state"] == "running" else None
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        out["last_line"] = lines[-1][:160] if lines else ""
    return out


def load_old_jobs(keep_hours: float) -> int:
    removed = 0
    for d in sorted(JOBS_DIR.iterdir()):
        if not (d.is_dir() and JOB_ID_RE.match(d.name)):
            continue
        meta = d / "job.json"
        try:
            job = json.loads(meta.read_text())
        except (OSError, ValueError):
            job = None
        age_h = (time.time() - d.stat().st_mtime) / 3600
        if job is None or age_h > keep_hours:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
            continue
        if job["state"] in ("queued", "running"):
            job["state"], job["error"] = "failed", "The app was restarted while this was running."
        job["cancel"] = False
        JOBS[job["id"]] = job
    return removed


# ---------------------------------------------------------------------------
# connection tests
# ---------------------------------------------------------------------------

def http_probe(url: str, key: str = "", timeout: int = 8):
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(300).decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {getattr(e, 'reason', e)}"


PROBE_CODE = r'''
import json, sys, time
from PIL import Image, ImageDraw, ImageFont
import blocks
im = Image.new("RGB", (900, 260), "white"); d = ImageDraw.Draw(im)
try:
    f = ImageFont.load_default(size=44)
except TypeError:
    f = ImageFont.load_default()
d.text((30, 40), "Invoice 1047", fill="black", font=f)
d.text((30, 130), "Total: 2,350.00 USD", fill="black", font=f)
im.save("probe.png")
t = time.time()
page, timing = blocks.extract_page("probe.png", base_url=sys.argv[1], return_timing=True, include_look=False)
text = " ".join(getattr(b, "text", None) or " ".join(list(b.header) + [c for r in b.rows for c in r]) for b in page.blocks)
print(json.dumps({"seconds": round(time.time() - t, 1), "blocks": len(page.blocks), "text": text, "timing": timing}))
'''


def run_test(kind: str) -> dict:
    base, checks = effective_base(), []
    remote = CONFIG["backend"] == "tuhin"
    code, body = http_probe(base + "/health", timeout=6)
    checks.append({"name": f"Server answers at {base}", "ok": code == 200,
                   "detail": "up" if code == 200 else (body if code is None else f"answered {code}")})
    if code != 200:
        if remote and base != FALLBACK:
            c2, _ = http_probe(FALLBACK + "/health", timeout=6)
            checks.append({"name": f"Fallback address {FALLBACK}", "ok": c2 == 200,
                           "detail": "up. Use this address instead." if c2 == 200 else "no answer"})
        return {"checks": checks, "ok": False}
    if remote:
        if not CONFIG["key"]:
            checks.append({"name": "API key", "ok": False, "detail": "no key saved yet"})
            return {"checks": checks, "ok": False}
        code, body = http_probe(base + "/v1/models", CONFIG["key"])
        checks.append({"name": "API key accepted", "ok": code == 200,
                       "detail": "accepted" if code == 200 else
                       ("the server rejected it (401): the key is wrong" if code == 401 else (body or f"answered {code}")[:160])})
        if code != 200:
            return {"checks": checks, "ok": False}
    if kind == "full":
        pd = JOBS_DIR / "_probe"
        pd.mkdir(exist_ok=True)
        try:
            proc = subprocess.run([PY, "-c", PROBE_CODE, base], cwd=pd, env=job_env(True),
                                  capture_output=True, text=True, timeout=CONFIG["timeout_s"] + 30)
            line = next((l for l in reversed(proc.stdout.splitlines()) if l.startswith("{")), None)
            if proc.returncode != 0 or not line:
                err = (proc.stderr.strip().splitlines() or ["no output"])[-1][:240]
                checks.append({"name": "Read a small test image through the real path", "ok": False, "detail": err})
            else:
                res = json.loads(line)
                good = "1047" in res["text"] and "2,350" in res["text"]
                checks.append({"name": "Read a small test image through the real path (image + JSON schema)",
                               "ok": good,
                               "detail": f"{res['seconds']} s. It read: {res['text'][:90]!r}" +
                                         ("" if good else " (expected 'Invoice 1047' and '2,350.00')")})
        except subprocess.TimeoutExpired:
            checks.append({"name": "Read a small test image through the real path", "ok": False,
                           "detail": "no answer within the time limit"})
        finally:
            shutil.rmtree(pd, ignore_errors=True)
    return {"checks": checks, "ok": all(c["ok"] for c in checks)}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "idox"

    def log_message(self, fmt, *args):        # quiet: never print URLs or headers
        pass

    # -- helpers ---------------------------------------------------------
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, message):
        self.send_json({"error": message}, code)

    def allowed(self, write: bool) -> bool:
        host = self.headers.get("Host", "")
        if host.rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
            self.fail(403, "Refused: this app only answers on localhost.")
            return False
        origin = self.headers.get("Origin")
        if write and origin and urlparse(origin).netloc != host:
            self.fail(403, "Refused: the request came from another site.")
            return False
        return True

    def body_json(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > 100_000:
            raise ValueError("too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def job_from_path(self, jid):
        if not JOB_ID_RE.match(jid or ""):
            return None
        return JOBS.get(jid)

    # -- GET -------------------------------------------------------------
    def do_GET(self):
        if not self.allowed(False):
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                body = PAGE.read_bytes()
            except OSError:
                return self.fail(500, "idox_app.html is missing next to idox_app.py.")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/config":
            return self.send_json(public_config())
        if path == "/api/routes":
            return self.send_json(routes_payload())
        if path == "/api/jobs":
            with JOBS_LOCK:
                jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
                return self.send_json([public_job(j, with_log=False) for j in jobs])
        m = re.match(r"^/api/jobs/([0-9a-f]{12})(?:/(log|zip|files/.+))?$", path)
        if m:
            job = self.job_from_path(m.group(1))
            if not job:
                return self.fail(404, "No such job.")
            what = m.group(2)
            if what is None:
                return self.send_json(public_job(job))
            if what == "log":
                text = read_tail(job_dir(job["id"]) / "log.txt", 200_000)
                return self.send_text(text)
            if what == "zip":
                return self.send_zip(job)
            return self.send_file(job, unquote(what[len("files/"):]))
        self.fail(404, "Not found.")

    def send_text(self, text):
        body = text.encode("utf-8", "replace")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, job, rel):
        out_dir = (job_dir(job["id"]) / "out").resolve()
        target = (out_dir / rel).resolve()
        if not target.is_file() or out_dir not in target.parents:
            return self.fail(404, "No such file.")
        size = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(target.name)}")
        self.end_headers()
        with open(target, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1024 * 1024)

    def send_zip(self, job):
        out_dir = (job_dir(job["id"]) / "out").resolve()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in list_outputs(job["id"]):
                p = out_dir / f["name"]
                comp = zipfile.ZIP_STORED if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".tif", ".tiff") \
                    else zipfile.ZIP_DEFLATED
                z.write(p, f["name"], compress_type=comp)
        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(job['stem'])}-idox.zip")
        self.end_headers()
        self.wfile.write(body)

    # -- POST ------------------------------------------------------------
    def do_POST(self):
        if not self.allowed(True):
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/jobs":
                return self.post_job(parsed)
            if path == "/api/config":
                return self.post_config()
            if path == "/api/test":
                kind = self.body_json().get("kind", "connect")
                return self.send_json(run_test("full" if kind == "full" else "connect"))
            m = re.match(r"^/api/jobs/([0-9a-f]{12})/cancel$", path)
            if m:
                return self.cancel(m.group(1))
        except ValueError as exc:
            return self.fail(400, str(exc))
        self.fail(404, "Not found.")

    def post_config(self):
        data = self.body_json()
        with CONFIG_LOCK:
            if data.get("backend") in ("tuhin", "local"):
                CONFIG["backend"] = data["backend"]
            if "endpoint" in data:
                CONFIG["endpoint"] = clean_endpoint(str(data["endpoint"]))
            if data.get("clear_key"):
                CONFIG["key"] = ""
            elif str(data.get("key", "")).strip():
                CONFIG["key"] = str(data["key"]).strip()
            if "thinking" in data:
                CONFIG["thinking"] = bool(data["thinking"])
            if "max_image_side" in data:
                v = int(data["max_image_side"])
                if not 256 <= v <= 4000:
                    raise ValueError("Largest image side should be between 256 and 4000 px")
                CONFIG["max_image_side"] = v
            if "timeout_s" in data:
                v = int(data["timeout_s"])
                if not 60 <= v <= 7200:
                    raise ValueError("Wait limit should be between 1 and 120 minutes")
                CONFIG["timeout_s"] = v
        self.send_json(public_config())

    def post_job(self, parsed):
        q = parse_qs(parsed.query)
        route_id = (q.get("route") or [""])[0]
        route = ROUTES.get(route_id)
        if not route:
            raise ValueError("Unknown conversion.")
        name = unquote(self.headers.get("X-Filename", "file"))
        ext = Path(name).suffix.lower().lstrip(".")
        if source_type(ext) != route["src"]:
            raise ValueError(f"A .{ext or '?'} file cannot be converted this way.")
        missing = [t for t in route.get("needs", []) if not tool_ok(t)]
        if missing:
            raise ValueError(f"This conversion needs {', '.join(missing)}, which is not installed here.")
        if route["model"] and CONFIG["backend"] == "tuhin" and not CONFIG["key"]:
            raise ValueError("Add your API key in Settings first, or switch to the local model.")
        try:
            opts = clean_options(route, json.loads(unquote((q.get("opts") or ["{}"])[0])))
        except json.JSONDecodeError:
            raise ValueError("Unreadable options.")
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            raise ValueError("The file is empty.")
        if length > MAX_UPLOAD:
            self.rfile.read(0)
            return self.fail(413, f"The file is larger than {MAX_UPLOAD // (1024 * 1024)} MB.")
        jid = secrets.token_hex(6)
        jd = job_dir(jid)
        (jd / "in").mkdir(parents=True)
        (jd / "out").mkdir()
        stem = safe_stem(name)
        target = jd / "in" / f"{stem}.{ext}"
        remaining, head = length, b""
        with open(target, "wb") as fh:
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                if len(head) < 8:
                    head += chunk[:8]
                fh.write(chunk)
                remaining -= len(chunk)
        bad = None
        if remaining:
            bad = "The upload was cut off."
        elif ext == "pdf" and not head.startswith(b"%PDF"):
            bad = "That does not look like a PDF."
        elif ext == "docx" and not head.startswith(b"PK"):
            bad = "That does not look like a .docx file."
        elif ext in ("tif", "tiff") and head[:2] not in (b"II", b"MM"):
            bad = "That does not look like a TIFF."
        if bad:
            shutil.rmtree(jd, ignore_errors=True)
            raise ValueError(bad)
        job = {"id": jid, "filename": name[:120], "stem": stem, "ext": ext, "route": route_id,
               "model": route["model"], "options": opts, "state": "queued",
               "created": time.time(), "started": None, "finished": None,
               "outputs": [], "summary": [], "problems": [], "error": None, "cancel": False}
        with JOBS_LOCK:
            JOBS[jid] = job
            save_job(job)
        self.send_json(public_job(job, with_log=False), 201)

    def cancel(self, jid):
        job = self.job_from_path(jid)
        if not job:
            return self.fail(404, "No such job.")
        with JOBS_LOCK:
            if job["state"] == "queued":
                job.update(state="cancelled", error="Cancelled.", finished=time.time())
                save_job(job)
            elif job["state"] == "running":
                job["cancel"] = True
        self.send_json(public_job(job, with_log=False))

    # -- DELETE ----------------------------------------------------------
    def do_DELETE(self):
        if not self.allowed(True):
            return
        m = re.match(r"^/api/jobs/([0-9a-f]{12})$", urlparse(self.path).path)
        job = self.job_from_path(m.group(1)) if m else None
        if not job:
            return self.fail(404, "No such job.")
        with JOBS_LOCK:
            if job["state"] in ("queued", "running"):
                return self.fail(409, "Cancel it first.")
            JOBS.pop(job["id"], None)
        shutil.rmtree(job_dir(job["id"]), ignore_errors=True)
        self.send_json({"deleted": job["id"]})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    global JOBS_DIR
    ap = argparse.ArgumentParser(description="Local web app for the idox converters.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--jobs-dir", default=str(Path.home() / ".local/share/idox/jobs"),
                    help="where uploads and results are kept (default ~/.local/share/idox/jobs)")
    ap.add_argument("--keep-hours", type=float, default=48,
                    help="finished jobs older than this are deleted at start (default 48)")
    ap.add_argument("--model-jobs", type=int, default=None,
                    help="how many model jobs may run at once on the API (default 2)")
    args = ap.parse_args()
    if args.model_jobs:
        LIMITS["tuhin"] = args.model_jobs

    JOBS_DIR = Path(args.jobs_dir).expanduser().resolve()
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    removed = load_old_jobs(args.keep_hours)
    threading.Thread(target=dispatcher, daemon=True).start()

    server = Server(("127.0.0.1", args.port), Handler)
    print(f"idox is running:  http://127.0.0.1:{args.port}", flush=True)
    print(f"engine: {'local model' if CONFIG['backend'] == 'local' else CONFIG['endpoint']}"
          f"   key: {'set from TUHIN_AI_KEY' if CONFIG['key'] else 'not set (add it in Settings)'}")
    print(f"jobs kept in {JOBS_DIR}" + (f"  ({removed} old ones removed)" if removed else ""))
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
        with JOBS_LOCK:
            for j in JOBS.values():
                if j["state"] == "running":
                    j["cancel"] = True
        time.sleep(1)


if __name__ == "__main__":
    main()
