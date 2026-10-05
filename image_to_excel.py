"""
image_to_excel.py -- convert a raw IMAGE (not a PDF page) into an Excel .xlsx.

Same extraction as every other image pipeline in this project (image_to_word.py,
image_to_pdf.py) -- only the final writer differs. build_xlsx() already exists
(built for the PDF pipeline) and already handles exactly what an image needs:
tables become real spreadsheet grids, prose goes into column A as context, and
nothing here is table-specific extraction logic -- extract_page() never assumed
what was on the page to begin with, so it already works for a photo of a form,
a screenshot of a spreadsheet, a page of pure prose, or anything in between.

No --mode flag here, deliberately, unlike image_to_word.py/image_to_pdf.py: the
entire point of this conversion is turning the image's CONTENT into an editable
spreadsheet, so a "just embed the picture, skip extraction" mode would defeat
the purpose rather than offer a real alternative the way it does for a Word
document or a PDF snapshot.

WHY THIS CASE HAS NO GROUND TRUTH -- read before trusting the output blindly.
A raw image has no embedded text layer at all -- there is nothing to compare
the model's reading against, the same blind spot as a scanned PDF page.
checks 2 (grounding) and 4 (coverage) are answered here using an independent
OCR reading instead of reporting N/A -- OCR is still not ground truth, only a
second, differently-fallible opinion.

Usage:
    .venv/bin/python image_to_excel.py image_to_excel_input/table_photo.jpg
    .venv/bin/python image_to_excel.py image_to_excel_input/*.png
"""

import argparse
import glob
import os
import sys
import time

import ocr
from blocks import (
    DEFAULT_BASE_URL,
    LOCAL_BASE_URL,
    TableBlock,
    check_coverage,
    check_grounding,
    split_table_problems,
    check_structure,
    drop_duplicate_blocks,
    extract_page,
    fix_trailing_heading_after_table,
    merge_nested_tables,
)
from to_xlsx import build_xlsx

IMAGE_DIR = "image_to_excel_input"
XLSX_OUT_DIR = "image_to_excel_output"


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

    page, fixed_order = fix_trailing_heading_after_table(page)
    if fixed_order:
        print("  ! moved a trailing heading back in front of its table")

    page, nested_merges = merge_nested_tables(page)
    for m in nested_merges:
        print(f"  ! {m}")

    print(f"  extracted {len(page.blocks)} blocks in {elapsed:.1f}s  "
          f"(load {timing['load_s']:.1f}s, prefill {timing['prefill_s']:.1f}s "
          f"/{timing['prefill_tokens']}tok, generate {timing['generate_s']:.1f}s "
          f"/{timing['generate_tokens']}tok)")
    for b in page.blocks:
        if hasattr(b, "text"):
            print(f"    [{b.kind}] {b.text[:60]}")
        else:
            print(f"    [table] {b.n_data_rows}x{b.n_cols}"
                  f"{' with header' if b.has_header else ''}")
            if b.has_header:
                print(f"        H | {' | '.join(b.header)}")
            for row in b.rows:
                print(f"          | {' | '.join(row)}")

    # The whole reason to pick Excel over Word is expecting tabular data --
    # if the model found none at all, writing a near-empty spreadsheet (or
    # one that's just prose in column A) would misleadingly look like a
    # successful conversion of something that was never suited to this
    # format in the first place. There's no way to know this in advance
    # without actually reading the image (a passport photo and a real table
    # look identical until read), so this is checked here, after
    # extraction, not before it.
    has_table = any(isinstance(b, TableBlock) for b in page.blocks)
    if not has_table:
        print()
        print("  NO TABLE DETECTED -- this image does not appear to contain "
              "tabular data.")
        print("  Excel conversion needs a table to be meaningful; nothing "
              "will be written.")
        print("  Please check the image and re-upload one that contains a "
              "real table, or use image_to_word.py instead if this is "
              "meant to be a text document, not a spreadsheet.")
        print()
        return False

    print("\n  --- checks ---")
    problems, notes = check_structure(page)
    print(f"  1. self-consistency:       {'PASS' if not problems else 'FAIL'}")
    for p in problems:
        print(f"       - {p}")

    # Same reasoning as image_to_word.py: a raw image has no text layer, so
    # OCR stands in as an independent second reading for grounding/coverage
    # instead of reporting N/A. Never used to auto-repair -- a disagreement
    # is reported, never silently applied.
    if ocr.have_tesseract():
        ocr_text = ocr.ocr_image(image_path)
        if ocr_text.strip():
            grounding, found, total = check_grounding(page, ocr_text)
            hard, table_only = split_table_problems(grounding)
            verdict = "FAIL" if hard else ("differs" if table_only else "PASS")
            print(f"  2. OCR grounding:          {verdict}"
                  f"   ({found}/{total} strings verified against an "
                  f"independent OCR reading"
                  f"{'; table cells OCR could not confirm are advisory, it reads ruled tables badly' if verdict == 'differs' else ''})")
            for p in grounding:
                print(f"       - {p}")

            coverage_problems, coverage, _missing, missing_lines = (
                check_coverage(page, ocr_text, row_tolerant=True)
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
    final_path = os.path.join(out_dir, f"{name}.xlsx")
    xl_problems = build_xlsx([page], final_path)
    for p in xl_problems:
        print(f"    - {p}")

    print(f"  -> {final_path}")
    print()
    return True


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("images", nargs="+", help="image file(s), globs allowed")
parser.add_argument("--model", default="qwen3-vl:2b-instruct")
parser.add_argument("--outdir", default=XLSX_OUT_DIR)
parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                    help="the raw llama-server instance to use (default: "
                         f"the remote Qwen3-VL-8B endpoint at {DEFAULT_BASE_URL}"
                         f"). Pass --base-url {LOCAL_BASE_URL} for this "
                         "machine's local 2B model instead, or an empty "
                         "string to use Ollama's own bundled model (e.g. "
                         "for --model qwen3-vl:4b-instruct).")
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
