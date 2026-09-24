"""
image_to_word.py -- convert a raw IMAGE (not a PDF page) into a Word .docx.

Two modes -- the same user gate as --scan-mode in test_pdf.py, same reasoning:
whether the model is needed at all depends only on whether you want a picture
or you want text.

  --mode text   (default) image -> extract_page() -> Page objects ->
                build_docx() -> final.docx. The model reads the page and
                writes back editable, selectable text and tables.

  --mode image  image -> build_docx() -> final.docx, model NEVER called. The
                original picture is embedded in the .docx as-is -- a snapshot,
                not a reading. Costs a fraction of a second instead of
                however long extraction takes, same trade-off as
                --scan-mode image for a scanned PDF page.

  --mode both   does BOTH of the above, into two SEPARATE files -- never
                mixed into one, same rule as --scan-mode both: final.docx
                (the editable text transcript) and final_scan.docx (the
                original picture, embedded as-is).

WHY THIS CASE HAS NO GROUND TRUTH (--mode text only) -- read before trusting
the output blindly. A raw image (unlike a PDF) has no embedded text layer at
all -- there is nothing to compare the model's reading against. This is the
exact same blind spot documented for scanned PDF pages: checks 2 (grounding)
and 4 (coverage) cannot run, because there is no ground truth to check
against. The extraction here is UNVERIFIED by design, not because of a bug.

Usage:
    .venv/bin/python image_to_word.py image_to_word_input/photo.jpg
    .venv/bin/python image_to_word.py image_to_word_input/*.png --base-url http://127.0.0.1:8090
    .venv/bin/python image_to_word.py image_to_word_input/photo.jpg --mode image
"""

import argparse
import glob
import hashlib
import os
import sys
import time

from PIL import Image

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

IMAGE_DIR = "image_to_word_input"
DOCX_OUT_DIR = "image_to_word_output"

# A screenshot/photo has no physical "page size" the way a PDF does, so there
# is no real DPI to convert pixels to inches with. 96 is the standard
# screen-image assumption (matches to_xlsx.py's EXCEL_IMAGE_DPI) -- close
# enough for a sensible default size, and _add_images() clamps it to the
# page's text-column width regardless, so an oversized photo cannot overflow
# the page.
ASSUMED_DPI = 96


def embed_only(image_path: str, out_dir: str, name: str) -> bool:
    """--mode image (or the image half of --mode both): copy the picture
    into a .docx, no model call at all."""
    with open(image_path, "rb") as fh:
        data = fh.read()
    w, h = Image.open(image_path).size

    page = Page(analysis={"n_blocks": 0, "table_column_counts": []}, blocks=[])
    image_entry = {
        "path": image_path,
        "sha1": hashlib.sha1(data).hexdigest(),
        "bytes": len(data),
        "ext": os.path.splitext(image_path)[1].lstrip("."),
        "bbox": (0, h),
        "width_in": w / ASSUMED_DPI,
        "height_in": h / ASSUMED_DPI,
        "px": (w, h),
        "frac_above": 0.0,
        "text_before": None,
        "text_after": None,
    }

    os.makedirs(out_dir, exist_ok=True)
    final_path = os.path.join(out_dir, f"{name}.docx")
    build_docx([page], final_path, image_sets=[[image_entry]])

    print(f"  embedded as-is, no model call -- {w}x{h}px")
    print(f"  -> {final_path}")
    print()
    return True


def run(image_path: str, model: str, out_dir: str, base_url: str = None,
        mode: str = "text") -> bool:
    name = os.path.splitext(os.path.basename(image_path))[0]
    label = base_url or model
    print("=" * 72)
    print(f"{image_path}  ({'base_url ' if base_url else 'model '}{label})"
          f"  --mode {mode}")
    print("=" * 72)

    if mode == "image":
        return embed_only(image_path, out_dir, name)

    if mode == "both":
        # Two separate files, never mixed -- same rule as --scan-mode both.
        ok_image = embed_only(image_path, out_dir, f"{name}_scan")
        ok_text = run(image_path, model, out_dir, base_url=base_url,
                      mode="text")
        return ok_image and ok_text

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

    # A raw image has no text layer -- there is no document to check against,
    # only the picture itself. OCR gives grounding/coverage something real to
    # compare against instead of reporting N/A: a genuinely independent second
    # reading (Tesseract, per-character classification, not a transformer),
    # so its mistakes don't correlate with the model's. Same role OCR plays
    # for a scanned PDF page. Never used to auto-repair -- a disagreement is
    # reported, never silently applied, because OCR is a READING, not the
    # document.
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
    final_path = os.path.join(out_dir, f"{name}.docx")
    build_docx([page], final_path)

    print(f"  -> {final_path}")
    print()
    return True


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("images", nargs="+", help="image file(s), globs allowed")
parser.add_argument("--model", default="qwen3-vl:2b-instruct")
parser.add_argument("--outdir", default=DOCX_OUT_DIR)
parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                    help="the raw llama-server instance to use (default: "
                         "the manually-downloaded HF model on "
                         f"{DEFAULT_BASE_URL}). Pass an empty string to use "
                         "Ollama's own bundled model instead (e.g. for "
                         "--model qwen3-vl:4b-instruct, not present in the "
                         "manually-downloaded set).")
parser.add_argument("--mode", choices=("text", "image", "both"), default="text",
                    help="'text' (default) reads the image with the model "
                         "and writes editable text/tables. 'image' embeds "
                         "the picture as-is and does NOT run the model at "
                         "all -- fast, but not editable. 'both' writes both, "
                         "as two SEPARATE files (name.docx and "
                         "name_scan.docx), never mixed into one.")
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
    if run(path, args.model, args.outdir, base_url=args.base_url,
           mode=args.mode):
        ok += 1
    else:
        fail += 1

if len(paths) > 1:
    print("=" * 72)
    print(f"{ok} succeeded, {fail} failed, {len(paths)} total")

sys.exit(1 if fail else 0)
