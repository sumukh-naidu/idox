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

HOW IT PROVES EVERYTHING WAS CONVERTED. Writing a file is not the same as the file
containing the page, so each JPG is read back from disk and checked against what the
PDF itself says is on that page (see check_page()):

  1. DIMENSIONS   the JPG is exactly the page's size at the chosen dpi.
  2. NOT BLANK    a page that has text, images or drawings did not come out white.
  3. TEXT LINES   (digital pages) every line in the PDF's text layer sits at a spot
                  where the JPG has ink -- the position of each line is looked up in
                  the PDF, then that region of the JPG is checked. No OCR involved,
                  so no OCR misreads can cause a false alarm.
  4. OCR WORDS    (digital pages, needs tesseract) an independent reading of the JPG
                  must contain the PDF's own words. OCR is a reading, not the
                  document, so a small shortfall is expected and only a real gap
                  (below 90%) fails.

A scanned page has no text layer, so 3 and 4 are reported as not applicable rather
than passed: checks 1 and 2 still apply.

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

import ocr
from blocks import normalize, squash

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


# A region of the JPG counts as "has ink" when its brightest and darkest pixels
# differ by more than this (0-255 greyscale). Handles dark text on white, white
# text on black, and text over a photo alike; a blank region differs by ~0-3
# even after JPEG compression noise.
INK_CONTRAST = 40
OCR_MIN_COVERAGE = 0.90


def _has_ink(gray: Image.Image, bbox, scale: float) -> bool:
    x0, y0, x1, y1 = (int(v * scale) for v in bbox)
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(max(x1, x0 + 1), gray.width), min(max(y1, y0 + 1), gray.height)
    if x1 <= x0 or y1 <= y0:
        return False
    lo, hi = gray.crop((x0, y0, x1, y1)).getextrema()
    return hi - lo > INK_CONTRAST


def check_page(page, jpg_path: str, pix_w: int, pix_h: int, page_dpi: int,
               use_ocr: bool):
    """Verify one saved JPG against the PDF page it came from.

    Returns (problems, info). info carries what was measured, for the report.
    """
    problems, info = [], {}

    with Image.open(jpg_path) as im:
        rgb = im.convert("RGB")
    gray = rgb.convert("L")

    # 1. dimensions
    want_w = page.rect.width * page_dpi / 72.0
    want_h = page.rect.height * page_dpi / 72.0
    info["size"] = rgb.size
    if rgb.size != (pix_w, pix_h) or abs(rgb.width - want_w) > 1.5 \
            or abs(rgb.height - want_h) > 1.5:
        problems.append(f"size {rgb.size} but the page at {page_dpi} dpi should "
                        f"be about {want_w:.0f}x{want_h:.0f}")

    # 2. not blank, when the PDF says the page has content
    source_text = page.get_text()
    has_text = bool(source_text.strip())
    has_content = (has_text or bool(page.get_images(full=True))
                   or bool(page.get_drawings()))
    lo, hi = gray.getextrema()
    info["blank_pdf_page"] = not has_content
    if has_content and hi - lo <= INK_CONTRAST:
        problems.append("the page has content in the PDF but the JPG is blank")

    if not has_text:
        # Nothing to compare the text against. A page with no content at all is
        # simply blank; one with images or drawings but no text is a scan.
        info["lines"] = None
        info["scanned"] = has_content
        info["ocr"] = None
        return problems, info

    # 3. every text line of the PDF sits on ink in the JPG
    scale = rgb.width / page.rect.width
    lines = [
        ("".join(sp["text"] for sp in ln["spans"]).strip(), ln["bbox"])
        for blk in page.get_text("dict")["blocks"] if blk["type"] == 0
        for ln in blk["lines"]
    ]
    lines = [(t, b) for t, b in lines if t]
    bare = [t for t, b in lines if not _has_ink(gray, b, scale)]
    info["lines"] = (len(lines) - len(bare), len(lines))
    if bare:
        shown = "; ".join(repr(t[:40]) for t in bare[:3])
        more = f" (+{len(bare) - 3} more)" if len(bare) > 3 else ""
        problems.append(f"{len(bare)} text line(s) have no ink in the JPG: "
                        f"{shown}{more}")

    # 4. independent OCR reading of the JPG contains the PDF's own words
    info["ocr"] = None
    if use_ocr and ocr.have_tesseract():
        read = ocr.ocr_image(jpg_path)
        got, got_sq = normalize(read), squash(read)
        words = {w for w in normalize(source_text).split() if len(w) > 3}
        if words:
            missing = sorted(w for w in words
                             if w not in got and squash(w) not in got_sq)
            cov = 1 - len(missing) / len(words)
            info["ocr"] = cov
            if cov < OCR_MIN_COVERAGE:
                problems.append(
                    f"OCR read only {cov:.0%} of the PDF's words from the JPG "
                    f"({len(missing)} missing, e.g. {', '.join(missing[:6])})")
    return problems, info


def run(pdf_path: str, out_dir: str, dpi: int, quality: int, pages: str,
        use_ocr: bool = True) -> bool:
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
    total_bytes = 0
    results = []            # one (pno, problems, info) per page that was written
    failed_pages = []       # pages that could not be written at all
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
            print(f"  page {pno + 1}: FAILED to write: {exc}")
            failed_pages.append(pno + 1)
            continue

        total_bytes += os.path.getsize(path)
        # Verified from the SAVED file, not from the pixmap in memory.
        try:
            problems, info = check_page(page, path, pix.width, pix.height,
                                        page_dpi, use_ocr)
        except Exception as exc:
            problems, info = [f"could not read the JPG back: {exc}"], {}
        results.append((pno + 1, problems, info))

        parts = [f"{pix.width}x{pix.height}px", f"{os.path.getsize(path) / 1024:.0f} KB"]
        if info.get("blank_pdf_page"):
            parts.append("blank page in the PDF")
        if info.get("lines"):
            parts.append(f"text lines {info['lines'][0]}/{info['lines'][1]} on ink")
        elif info.get("scanned"):
            parts.append("no text layer (scan) -- text checks N/A")
        if info.get("ocr") is not None:
            parts.append(f"OCR {info['ocr']:.0%} of PDF words")
        print(f"  page {pno + 1}: {', '.join(parts)}  "
              f"[{'PASS' if not problems else 'FAIL'}]")
        for p in problems:
            print(f"       - {p}")

    written = len(results)
    n_bad = sum(1 for _, problems, _ in results if problems)
    ok = written == len(targets) and n_bad == 0

    print()
    print(f"  --- verification (read back from the saved JPGs) ---")
    print(f"  pages converted:  {written}/{len(targets)}"
          + (f"   NOT written: {failed_pages}" if failed_pages else ""))
    print(f"  pages verified:   {written - n_bad}/{written}")
    n_text = sum(1 for _, _, i in results if i.get("lines"))
    n_scan = sum(1 for _, _, i in results if i.get("scanned"))
    if n_scan:
        print(f"  note: {n_scan} page(s) have no text layer (scanned). Size and "
              f"blank checks ran; text checks cannot, so those pages are "
              f"verified as 'drawn', not as 'every word present'.")
    if n_text and use_ocr and not ocr.have_tesseract():
        print(f"  note: tesseract not installed -- OCR word check skipped "
              f"({ocr.install_hint()})")
    print(f"  {total_bytes / 1024:.0f} KB total, {time.time() - started:.1f}s")
    print(f"  VERDICT: {'PASS -- every page converted and verified' if ok else 'FAIL -- see the problems above'}")
    print()
    return ok


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
parser.add_argument("--no-ocr", action="store_true",
                    help="skip the OCR word check (the other checks still run)")
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
    if run(path, args.outdir, args.dpi, args.quality, args.pages,
           use_ocr=not args.no_ocr):
        ok += 1
    else:
        fail += 1

if len(paths) > 1:
    print("=" * 72)
    print(f"{ok} succeeded, {fail} failed, {len(paths)} total")

sys.exit(1 if fail else 0)
