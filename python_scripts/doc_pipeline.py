"""
doc_pipeline.py -- the shared body of pdf_to_xml.py, pdf_to_csv.py and word_to_{xlsx,ppt,xml,csv,image}.py.

PDF  -> X   runs test_pdf.py, the project's own PDF pipeline (the 2B model reads each page, the page is checked
            against the PDF's own text layer), with the output flag for X: --xml / --csv / --xlsx / --pptx.
Word -> X   renders the .docx to a PDF with LibreOffice (the same engine word_to_pdf.py uses), then runs that
            PDF through the same pipeline. The model still does the reading; LibreOffice only draws the pages.
Word -> Image  renders the .docx to a PDF, then every page to a PNG. This one is a pure rendering job: there is
            nothing for a model to read, so none is used.

Every run writes next to the input: <script>_input/ in, <script>_output/ out. Word runs also keep the
intermediate <name>.pdf in the output folder, so a bad result can be traced to the page the model saw.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from blocks import LOCAL_BASE_URL

REPO = Path(__file__).resolve().parent
EXT = {"ppt": "pptx"}      # the script name says ppt, the file and the test_pdf flag say pptx
_SOFFICE_DIRS = (r"C:\Program Files\LibreOffice\program", r"C:\Program Files (x86)\LibreOffice\program")


def find_soffice() -> str:
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for d in _SOFFICE_DIRS:
        exe = os.path.join(d, "soffice.exe")
        if os.path.exists(exe):
            return exe
    raise SystemExit("LibreOffice not found (needed to turn Word into pages). Install it: "
                     "winget install TheDocumentFoundation.LibreOffice  (Windows, admin prompt) or "
                     "sudo apt-get install -y libreoffice-writer (Linux).")


def docx_to_pdf(docx_path: str, out_dir: str, timeout: int = 240) -> str:
    """Render a .docx to PDF with LibreOffice (throwaway profile, so an open LibreOffice cannot hijack it)."""
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([find_soffice(), f"-env:UserInstallation={Path(profile).as_uri()}", "--headless",
                        "--norestore", "--convert-to", "pdf", "--outdir", out_dir, docx_path],
                       capture_output=True, text=True, timeout=timeout)
    pdf = os.path.join(out_dir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
    if not os.path.exists(pdf):
        raise RuntimeError(f"LibreOffice produced no PDF for {docx_path}")
    return pdf


def pdf_to_pngs(pdf_path: str, out_dir: str, dpi: int = 150):
    import pymupdf
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    written = []
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc, start=1):
            path = os.path.join(out_dir, f"{stem}_page{i}.png")
            page.get_pixmap(dpi=dpi).save(path)
            written.append(path)
    return written


def _run_model_pipeline(pdf_path, flag, out_path, md_path, base_url, model, pages):
    cmd = [sys.executable, "-u", str(REPO / "test_pdf.py"), pdf_path, f"--{flag}", out_path, "--out", md_path,
           "--base-url", base_url, "--model", model, "--pages", pages]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    started = time.time()
    rc = subprocess.run(cmd, cwd=REPO, env=env).returncode
    return rc, time.time() - started


def main(source: str, fmt: str, doc: str, in_dir: str, out_dir: str):
    """source is 'pdf' or 'word'; fmt is xml/csv/xlsx/pptx, or 'image' (word only)."""
    ext = ".pdf" if source == "pdf" else ".docx"
    parser = argparse.ArgumentParser(description=doc)
    parser.add_argument("files", nargs="*", help=f"{ext} file(s), globs allowed (default: everything in {in_dir}/)")
    parser.add_argument("--outdir", default=out_dir)
    parser.add_argument("--pages", default="all", help="e.g. 1, 1-3, 2,4 (default: all)")
    if fmt != "image":
        parser.add_argument("--model", default="qwen3-vl:2b-instruct")
        parser.add_argument("--base-url", default=LOCAL_BASE_URL,
                            help=f"the llama-server to use (default: the local 2B at {LOCAL_BASE_URL})")
    args = parser.parse_args()

    paths = []
    for pattern in args.files or [os.path.join(in_dir, "*" + ext)]:
        paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern])
    paths = [p for p in paths if p.lower().endswith(ext)]
    if not paths:
        print(f"no {ext} files found (put some in {in_dir}/)")
        sys.exit(1)

    args.outdir = os.path.abspath(args.outdir)      # the model script runs from another folder, so a relative path would land there
    os.makedirs(args.outdir, exist_ok=True)
    ok = fail = 0
    for path in paths:
        if not os.path.exists(path):
            print(f"{path}: file not found")
            fail += 1
            continue
        name = os.path.splitext(os.path.basename(path))[0]
        print("=" * 72)
        print(f"{path}  ->  {fmt}")
        print("=" * 72)
        try:
            if source == "word":
                pdf = docx_to_pdf(os.path.abspath(path), args.outdir)
                print(f"  rendered {pdf}")
            else:
                pdf = path
            if fmt == "image":
                pngs = pdf_to_pngs(pdf, args.outdir)
                print(f"  {len(pngs)} page image(s) written to {args.outdir}/")
                good = bool(pngs)
            else:
                out_path = os.path.join(args.outdir, f"{name}.{EXT.get(fmt, fmt)}")
                rc, secs = _run_model_pipeline(os.path.abspath(pdf), EXT.get(fmt, fmt), out_path,
                                               os.path.join(args.outdir, f"{name}.md"), args.base_url, args.model,
                                               args.pages)
                good = rc == 0 and os.path.exists(out_path)
                print(f"\n  {'OK' if good else 'FAILED'}: {out_path}  ({secs:.0f}s)")
        except Exception as exc:
            print(f"  FAILED: {exc}")
            good = False
        ok += good
        fail += not good
    if len(paths) > 1:
        print("=" * 72)
        print(f"{ok} succeeded, {fail} failed, {len(paths)} total")
    sys.exit(1 if fail else 0)
