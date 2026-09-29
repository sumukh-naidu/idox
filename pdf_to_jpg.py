"""
pdf_to_jpg.py -- convert a PDF into JPG images, one JPG per page.

NO MODEL IS INVOLVED HERE, and that is deliberate. The other converters in this
project need a model because their target is EDITABLE content (Word text, Excel
cells), and a PDF has no structure left to read that from. A JPG has no such
requirement: it is a picture of the page, so the correct conversion is to draw
the page exactly as a PDF viewer would. PyMuPDF does that, deterministically, in
about a second per page -- text, tables, logos and scans all come out exactly as
they look, with nothing to drop, misread or reorder.

Works identically on digital and scanned PDFs, since both are just drawn.

Output layout: every PDF gets its own subfolder, one JPG per page, so several
PDFs never mix and a multi-page PDF stays together:

    pdf_to_jpg_input/report.pdf  ->  pdf_to_jpg_output/report/page_001.jpg
                                                              page_002.jpg
                                                              ...

Usage:
    .venv/bin/python pdf_to_jpg.py pdf_to_jpg_input/report.pdf
    .venv/bin/python pdf_to_jpg.py pdf_to_jpg_input/*.pdf
    .venv/bin/python pdf_to_jpg.py report.pdf --pages 1-3 --dpi 200 --quality 95
"""

import argparse
import glob
import os
import sys
import time

import pymupdf
from PIL import Image

PDF_DIR = "pdf_to_jpg_input"
JPG_OUT_DIR = "pdf_to_jpg_output"

# A page whose rendered longest side would exceed this is drawn at a lower dpi
# instead. Some scanners declare the page box in PIXELS rather than points (see
# _sane_page_rect() in test_pdf.py), so a 200 dpi scan can claim to be 23.6 x
# 30.6 inches; at 150 dpi that is a 3500 x 4600 px image for a page that is
# really Letter. The cap keeps such files a sensible size.
MAX_SIDE_PX = 5000


def parse_pages(spec: str, page_count: int):
    """'all', '2', '1-3' or '1,3-4' -> zero-based page indexes, in order."""
    if spec in ("all", ""):
        return list(range(page_count))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, int(b)))
        else:
            out.append(int(part) - 1)
    return [p for p in out if 0 <= p < page_count]


def run(pdf_path: str, out_dir: str, dpi: int, quality: int, pages: str) -> bool:
    name = os.path.splitext(os.path.basename(pdf_path))[0]
    print("=" * 72)
    print(pdf_path)
    print("=" * 72)

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        print(f"  COULD NOT OPEN: {exc}")
        return False

    if doc.needs_pass:
        print("  SKIPPED: the PDF is password protected")
        return False

    targets = parse_pages(pages, doc.page_count)
    if not targets:
        print(f"  SKIPPED: no pages match --pages {pages!r} "
              f"(the PDF has {doc.page_count})")
        return False

    pdf_out = os.path.join(out_dir, name)
    os.makedirs(pdf_out, exist_ok=True)
    print(f"  {doc.page_count} page(s), converting {len(targets)} at {dpi} dpi, "
          f"JPG quality {quality} -> {pdf_out}/")

    started = time.time()
    written = 0
    total_bytes = 0
    for pno in targets:
        page = doc[pno]
        page_dpi = dpi
        longest_in = max(page.rect.width, page.rect.height) / 72.0
        if longest_in * dpi > MAX_SIDE_PX:
            page_dpi = max(int(MAX_SIDE_PX / longest_in), 30)
            print(f"  page {pno + 1}: declared {page.rect.width / 72:.1f}x"
                  f"{page.rect.height / 72:.1f}in is very large -- drawn at "
                  f"{page_dpi} dpi instead of {dpi}")

        path = os.path.join(pdf_out, f"page_{pno + 1:03d}.jpg")
        try:
            # alpha=False: JPEG has no transparency, so the page is drawn on
            # white rather than failing or coming out black.
            pix = page.get_pixmap(dpi=page_dpi, alpha=False)
            pix.save(path, jpg_quality=quality)
        except Exception as exc:
            print(f"  page {pno + 1}: FAILED: {exc}")
            continue

        written += 1
        total_bytes += os.path.getsize(path)
        print(f"  page {pno + 1}: {pix.width}x{pix.height}px, "
              f"{os.path.getsize(path) / 1024:.0f} KB -> {path}")

    # Reading the files back is the only honest way to claim they exist and
    # are valid pictures -- same principle as verify_docx() in to_docx.py.
    bad = 0
    for pno in targets:
        path = os.path.join(pdf_out, f"page_{pno + 1:03d}.jpg")
        try:
            with Image.open(path) as im:
                im.verify()
        except Exception:
            bad += 1

    print(f"  {written}/{len(targets)} page(s) written, "
          f"{total_bytes / 1024:.0f} KB total, {time.time() - started:.1f}s "
          f"-- check: {'PASS' if written == len(targets) and not bad else 'FAIL'}"
          f" ({bad} unreadable)")
    print()
    return written == len(targets) and not bad


parser = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("pdfs", nargs="+", help="PDF file(s), globs allowed")
parser.add_argument("--outdir", default=JPG_OUT_DIR,
                    help=f"where the per-PDF folders go (default {JPG_OUT_DIR})")
parser.add_argument("--dpi", type=int, default=150,
                    help="render resolution (default 150)")
parser.add_argument("--quality", type=int, default=90,
                    help="JPG quality 1-100 (default 90)")
parser.add_argument("--pages", default="all",
                    help="e.g. 1, 1-3, 2,4 (default: all)")
args = parser.parse_args()

if not 1 <= args.quality <= 100:
    raise SystemExit("--quality must be between 1 and 100")
if args.dpi < 10:
    raise SystemExit("--dpi must be at least 10")

paths = []
for pattern in args.pdfs:
    paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[")
                 else [pattern])

ok = fail = 0
for path in paths:
    if not os.path.exists(path):
        print(f"skipping {path}: not found")
        continue
    if run(path, args.outdir, args.dpi, args.quality, args.pages):
        ok += 1
    else:
        fail += 1

if len(paths) > 1:
    print("=" * 72)
    print(f"{ok} succeeded, {fail} failed, {len(paths)} total")

sys.exit(1 if fail else 0)
