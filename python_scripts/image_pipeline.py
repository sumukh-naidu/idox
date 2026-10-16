"""
image_pipeline.py -- the shared body of image_to_ppt.py, image_to_xml.py and image_to_csv.py.

Same extraction as image_to_excel.py and image_to_word.py: the 2B model reads the picture into blocks (headings,
paragraphs, lists, tables), the same clean-ups and checks run, and only the final writer differs. Nothing is
parsed in code -- the model does the reading.

    PowerPoint  one slide; the first heading becomes the title (to_pptx.build_pptx)
    XML         every block as an element, tables as <table>/<row>/<cell> (to_xml.build_xml)
    CSV         text as one line per row, tables as rows and columns, in reading order (to_csv.build_csv)

A raw image has no text layer, so there is no ground truth to compare with. The AI model is the only reader: no OCR is
used anywhere in this pipeline. What is checked is the model's output against itself (check 1, self-consistency) and the
written file, which is read back to prove it opens.
"""

import argparse
import glob
import os
import sys
import time

from blocks import (
    DEFAULT_BASE_URL,
    LOCAL_BASE_URL,
    check_structure,
    drop_duplicate_blocks,
    extract_page,
    fix_trailing_heading_after_table,
    merge_nested_tables,
)
from image_tiles import extract_page_auto
from to_csv import build_csv, verify_csv
from to_pptx import build_pptx, verify_pptx
from to_xml import build_xml, verify_xml

FORMATS = {"pptx": "PowerPoint (.pptx)", "xml": "XML (.xml)", "csv": "CSV (.csv)"}


def _print_blocks(page):
    for b in page.blocks:
        if hasattr(b, "text"):
            print(f"    [{b.kind}] {b.text[:60]}")
        else:
            print(f"    [table] {b.n_data_rows}x{b.n_cols}{' with header' if b.has_header else ''}")
            if b.has_header:
                print(f"        H | {' | '.join(b.header)}")
            for row in b.rows:
                print(f"          | {' | '.join(row)}")


def _checks(page):
    """Check 1, the model's output against itself. Returns the problems found."""
    print("\n  --- checks ---")
    problems, _notes = check_structure(page)
    print(f"  1. self-consistency:       {'PASS' if not problems else 'FAIL'}")
    for p in problems:
        print(f"       - {p}")
    return problems


def run(image_path: str, fmt: str, model: str, out_dir: str, base_url: str = None, split=None) -> bool:
    name = os.path.splitext(os.path.basename(image_path))[0]
    print("=" * 72)
    print(f"{image_path}  ({'base_url ' if base_url else 'model '}{base_url or model})  ->  {FORMATS[fmt]}")
    print("=" * 72)

    started = time.time()
    try:
        page, timing = extract_page_auto(image_path, parts=split, model=model, return_timing=True, base_url=base_url)
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
          f"(load {timing['load_s']:.1f}s, prefill {timing['prefill_s']:.1f}s /{timing['prefill_tokens']}tok, "
          f"generate {timing['generate_s']:.1f}s /{timing['generate_tokens']}tok)")
    _print_blocks(page)

    if not page.blocks:
        print("\n  NOTHING READ -- the model returned no blocks, nothing will be written.\n")
        return False

    structure_problems = _checks(page)

    os.makedirs(out_dir, exist_ok=True)
    final_path = os.path.join(out_dir, f"{name}.{fmt}")
    if fmt == "pptx":
        problems = build_pptx([page], final_path)
    elif fmt == "xml":
        problems = build_xml([page], final_path, source_name=os.path.basename(image_path))
    else:
        problems = build_csv([page], final_path)
    for p in problems:
        print(f"    - {p}")
    if not os.path.exists(final_path):
        print("  NO FILE WRITTEN")
        return False

    # read the SAVED file back: it must open and parse
    try:
        verify = {"pptx": verify_pptx, "xml": verify_xml, "csv": verify_csv}[fmt]
        verify(final_path, "")
        print("  file check: opens and reads back OK")
    except Exception as exc:
        print(f"  FILE CHECK FAILED: the written .{fmt} could not be read back: {exc}")
        return False

    if structure_problems:
        print("  REVIEW NEEDED: the model's output failed its own consistency check (check 1), so the file "
              "may contain an invented, missing or misplaced block. Compare it with the image before using it:")
        for p in structure_problems:
            print(f"       - {p}")
    print(f"  -> {final_path}\n")
    return True


def main(fmt: str, doc: str, in_dir: str, out_dir: str):
    parser = argparse.ArgumentParser(description=doc)
    parser.add_argument("images", nargs="*", help=f"image file(s), globs allowed (default: everything in {in_dir}/)")
    parser.add_argument("--model", default="qwen3-vl:2b-instruct")
    parser.add_argument("--outdir", default=out_dir)
    parser.add_argument("--base-url", default=LOCAL_BASE_URL,
                        help=f"the llama-server to use (default: the local 2B at {LOCAL_BASE_URL}; "
                             f"the remote one is {DEFAULT_BASE_URL})")
    parser.add_argument("--split", default="auto", metavar="N|auto",
                        help="how many horizontal strips to read the picture in. Each strip gets the model's whole image "
                             "budget, so a dense page gives up far more of its words (about 63%% to 99%% on dense test "
                             "pages). 'auto' (default): 2 for a full-page picture (1400 px tall or more), 1 for "
                             "anything shorter. 1 forces one read of the whole picture.")
    args = parser.parse_args()

    IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
    paths = []
    for pattern in args.images or [os.path.join(in_dir, "*")]:
        found = sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern]
        paths.extend(p for p in found if os.path.splitext(p)[1].lower() in IMAGE_EXTS or not args.images)
    paths = [p for p in paths if os.path.isfile(p) or args.images]

    ok = fail = 0
    for path in paths:
        if not os.path.exists(path):
            print(f"{path}: file not found")
            fail += 1
            continue
        if run(path, fmt, args.model, args.outdir, base_url=args.base_url, split=None if args.split == 'auto' else int(args.split)):
            ok += 1
        else:
            fail += 1
    if len(paths) > 1:
        print("=" * 72)
        print(f"{ok} succeeded, {fail} failed, {len(paths)} total")
    sys.exit(1 if fail or not paths else 0)
