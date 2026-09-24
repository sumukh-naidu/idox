"""
image_to_pdf.py -- convert a raw IMAGE (not a PDF page) into a PDF, via 2B.

DELIBERATELY CHAINS TWO ALREADY-BUILT, ALREADY-PROVEN PIECES rather than
writing a third PDF-writer from scratch:

    image -> extract_page() -> Page objects -> build_docx() -> temp.docx
                                                                   |
                                                    word_to_pdf.convert()
                                                    (LibreOffice, no model)
                                                                   |
                                                                final.pdf

This is the same "chain existing writers" principle used for the PDF-output
case in the wider image-to-{pdf,word,excel,xml,ppt,csv} requirement: nobody
needs a bespoke PDF-writing library here, because build_docx() + LibreOffice
already produce valid PDFs reliably (proven earlier: 100% word coverage,
every paragraph/cell preserved, on three real documents).

WHY THIS CASE HAS NO GROUND TRUTH -- read before trusting the output blindly
A raw image (unlike a PDF) has no embedded text layer at all -- there is
nothing to compare the model's reading against. This is the exact same
blind spot documented for scanned PDF pages. Closed the same way scanned
PDFs close it: Tesseract OCR reads the image independently (a different
method from the model, so its mistakes don't correlate), and checks 2
(grounding) and 4 (coverage) run against THAT instead of reporting N/A. Still
not ground truth -- OCR is a reading of the pixels, not the document -- but
real, independent evidence instead of nothing.

Usage:
    .venv/bin/python image_to_pdf.py image_input/photo.jpg
    .venv/bin/python image_to_pdf.py image_input/*.png --model qwen3-vl:2b-instruct
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

import ocr
from blocks import (
    DEFAULT_BASE_URL,
    Page,
    check_coverage,
    check_grounding,
    check_structure,
    drop_duplicate_blocks,
    extract_page,
)
from to_docx import build_docx

IMAGE_DIR = "image_input"
PDF_OUT_DIR = "pdf_out_img"


# --- copied from word_to_pdf.py, on purpose -------------------------------
# word_to_pdf.py is a SCRIPT, not a library: it runs its own argparse at
# import time, which would hijack this script's own arguments if imported
# directly. word_to_pdf.py's own docstring states the project's convention
# for exactly this situation: copy small reusable pieces rather than import
# across independent scripts, so neither can break the other.

def find_soffice() -> str:
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    raise SystemExit(
        "LibreOffice not found. Install it with:\n"
        "    sudo apt-get install -y libreoffice-writer"
    )


def docx_to_pdf(docx_path: str, out_dir: str, timeout: int = 180) -> str:
    """Render a .docx to PDF with LibreOffice. Returns the output path."""
    soffice = find_soffice()
    os.makedirs(out_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as profile:
        result = subprocess.run(
            [soffice,
             f"-env:UserInstallation=file://{profile}",
             "--headless", "--norestore",
             "--convert-to", "pdf",
             "--outdir", out_dir,
             docx_path],
            capture_output=True, text=True, timeout=timeout,
        )

    expected = os.path.join(
        out_dir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf"
    )
    if not os.path.exists(expected):
        raise RuntimeError(
            f"conversion produced no file.\n"
            f"  stdout: {result.stdout.strip()}\n"
            f"  stderr: {result.stderr.strip()}"
        )
    return expected


def run(image_path: str, model: str, out_dir: str, base_url: str = None) -> bool:
    name = os.path.splitext(os.path.basename(image_path))[0]
    label = base_url or model
    print("=" * 72)
    print(f"{image_path}  ({'base_url ' if base_url else 'model '}{label})")
    print("=" * 72)

    started = time.time()
    try:
        page, timing = extract_page(
            image_path, model=model, return_timing=True, base_url=base_url
        )
    except Exception as exc:
        print(f"  EXTRACTION FAILED: {exc}")
        return False
    elapsed = time.time() - started

    page, dupes = drop_duplicate_blocks(page)
    for d in dupes:
        print(f"  ! dropped duplicate block (model repeated itself): {d!r}")

    print(f"  extracted {len(page.blocks)} blocks in {elapsed:.1f}s  "
          f"(load {timing['load_s']:.1f}s, prefill {timing['prefill_s']:.1f}s "
          f"/{timing['prefill_tokens']}tok, generate {timing['generate_s']:.1f}s "
          f"/{timing['generate_tokens']}tok)")
    for b in page.blocks:
        preview = b.text[:60] if hasattr(b, "text") else f"table {b.n_data_rows}x{b.n_cols}"
        print(f"    [{b.kind}] {preview}")

    print("\n  --- checks ---")
    problems, notes = check_structure(page)
    print(f"  1. self-consistency:       {'PASS' if not problems else 'FAIL'}")
    for p in problems:
        print(f"       - {p}")

    # A raw image has no text layer -- OCR gives grounding/coverage something
    # real to compare against instead of reporting N/A: a genuinely
    # independent second reading (Tesseract, per-character classification,
    # not a transformer), so its mistakes don't correlate with the model's.
    # Same role OCR plays for a scanned PDF page. Never used to auto-repair --
    # a disagreement is reported, never silently applied, because OCR is a
    # READING, not the document.
    if ocr.have_tesseract():
        ocr_text = ocr.ocr_image(image_path)
        if ocr_text.strip():
            grounding, found, total = check_grounding(page, ocr_text)
            print(f"  2. OCR grounding:          "
                  f"{'PASS' if not grounding else 'FAIL'}"
                  f"   ({found}/{total} strings verified against an "
                  f"independent OCR reading)")
            for p in grounding:
                print(f"       - {p}")

            coverage_problems, coverage, _missing, missing_lines = (
                check_coverage(page, ocr_text)
            )
            if coverage is None:
                print("  4. OCR coverage:           N/A")
            else:
                print(f"  4. OCR coverage:           "
                      f"{'PASS' if not coverage_problems else 'FAIL'}"
                      f"   ({coverage:.0%} of what OCR read was extracted)")
            for p in coverage_problems:
                print(f"       - {p}")
            if missing_lines:
                print(f"       ! {len(missing_lines)} line(s) OCR found but "
                      f"the model did not -- NOT auto-restored (OCR is a "
                      f"reading, not the document):")
                for m in missing_lines[:4]:
                    print(f"           {m[:60]!r}")
        else:
            print("  2/4. OCR grounding/coverage: N/A (OCR returned nothing)")
    else:
        print(f"  2/4. OCR grounding/coverage: N/A ({ocr.install_hint()})")

    print("  NOTE: still not ground truth -- OCR is a reading of the pixels, "
          "not the document itself. Agreement is real evidence; a "
          "disagreement means look at it, not the model is wrong.")

    os.makedirs(out_dir, exist_ok=True)
    temp_docx = os.path.join(out_dir, f".{name}_temp.docx")
    build_docx([page], temp_docx)

    try:
        pdf_path = docx_to_pdf(temp_docx, out_dir)
    finally:
        if os.path.exists(temp_docx):
            os.remove(temp_docx)

    final_path = os.path.join(out_dir, f"{name}.pdf")
    if pdf_path != final_path:
        shutil.move(pdf_path, final_path)

    print(f"  -> {final_path}")
    print()
    return True


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("images", nargs="+", help="image file(s), globs allowed")
parser.add_argument("--model", default="qwen3-vl:2b-instruct")
parser.add_argument("--outdir", default=PDF_OUT_DIR)
parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                    help="the raw llama-server instance to use (default: "
                         "the manually-downloaded HF model on "
                         f"{DEFAULT_BASE_URL}). Pass an empty string to use "
                         "Ollama's own bundled model instead (e.g. for "
                         "--model qwen3-vl:4b-instruct, not present in the "
                         "manually-downloaded set).")
args = parser.parse_args()

paths = []
for pattern in args.images:
    paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[")
                 else [pattern])

ok = fail = 0
for path in paths:
    if not os.path.exists(path):
        print(f"skipping {path}: not found")
        continue
    if run(path, args.model, args.outdir, base_url=args.base_url):
        ok += 1
    else:
        fail += 1

if len(paths) > 1:
    print("=" * 72)
    print(f"{ok} succeeded, {fail} failed, {len(paths)} total")

sys.exit(1 if fail else 0)
