"""
pdf_to_tiff.py -- convert a PDF into ONE multi-page TIFF (one frame per page).

NO MODEL IS INVOLVED HERE, for the same reason as pdf_to_jpg.py: a TIFF is a
picture of the page, so the correct conversion is to draw each page exactly as a
PDF viewer would. PyMuPDF draws it, Pillow writes the frames. Works identically
on digital and scanned PDFs.

Compared with JPG, two things change:

  - TIFF holds every page in ONE file, which is the usual reason to want it.
  - LZW and Deflate are LOSSLESS, so the file can be checked far more strictly
    than a JPG: the saved pixels must equal a fresh render of the page exactly.

Output:   pdf_to_tiff_input/report.pdf  ->  pdf_to_tiff_output/report.tiff

HOW IT PROVES EVERYTHING WAS CONVERTED. Writing a file is not the same as the
file containing the document, so the saved TIFF is opened again and every frame
is checked against the PDF (see check_frame()):

  1. FRAMES       the TIFF has exactly as many frames as pages requested.
  2. DIMENSIONS   each frame is the page's size at the chosen dpi.
  3. DPI          the resolution stored in the file matches what was drawn.
  4. PIXEL-EXACT  each frame is byte-for-byte identical to a fresh render of that
                  page. Only possible because the compression is lossless.
  5. NOT BLANK    a page with text, images or drawings did not come out white.
  6. TEXT LINES   (digital pages) every line in the PDF's text layer sits on ink
                  in the frame, looked up by coordinates. No OCR involved.
  7. OCR WORDS    (digital pages, needs tesseract) an independent reading of the
                  frame must contain the PDF's own words. OCR is a reading, not
                  the document, so only a real gap (below 90%) fails.

A scanned page has no text layer, so 6 and 7 are reported as not applicable
rather than passed. Check 4 already proves the frame equals the render.

Usage:
    .venv/bin/python pdf_to_tiff.py pdf_to_tiff_input/report.pdf
    .venv/bin/python pdf_to_tiff.py pdf_to_tiff_input/*.pdf
    .venv/bin/python pdf_to_tiff.py report.pdf --pages 1-3 --dpi 200 --compression deflate
"""

import argparse
import glob
import os
import sys
import tempfile
import time

import pymupdf
from PIL import Image

import ocr
from blocks import normalize, squash

PDF_DIR = "pdf_to_tiff_input"
TIFF_OUT_DIR = "pdf_to_tiff_output"

COMPRESSIONS = {"lzw": "tiff_lzw", "deflate": "tiff_deflate"}

# A page whose rendered longest side would exceed this is drawn at a lower dpi
# instead. Some scanners declare the page box in PIXELS rather than points (see
# _sane_page_rect() in test_pdf.py), so a scan can claim to be 23.6 x 30.6 inches.
# At 300 dpi that is 7,000 x 9,000 px -- a huge frame for what is really a Letter
# page. A3 at 300 dpi is 4,962 px, so genuine large pages are not affected.
MAX_SIDE_PX = 5000

# A region has "ink" when its brightest and darkest pixels differ by more than
# this (0-255 greyscale): dark on white, white on black, text over a photo.
INK_CONTRAST = 40
OCR_MIN_COVERAGE = 0.90


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


def page_dpi_for(page, dpi: int) -> int:
    longest_in = max(page.rect.width, page.rect.height) / 72.0
    if longest_in * dpi > MAX_SIDE_PX:
        return max(int(MAX_SIDE_PX / longest_in), 30)
    return dpi


def render(page, dpi: int):
    """Draw one page. alpha=False draws on white, since a frame has no need of
    transparency and a transparent page would otherwise come out black."""
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return img


def _has_ink(gray: Image.Image, bbox, scale: float) -> bool:
    x0, y0, x1, y1 = (int(v * scale) for v in bbox)
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(max(x1, x0 + 1), gray.width), min(max(y1, y0 + 1), gray.height)
    if x1 <= x0 or y1 <= y0:
        return False
    lo, hi = gray.crop((x0, y0, x1, y1)).getextrema()
    return hi - lo > INK_CONTRAST


def check_frame(page, frame: Image.Image, want_dpi: int, use_ocr: bool):
    """Verify one frame read back from the saved TIFF against its PDF page.

    Returns (problems, info). info carries what was measured, for the report.
    """
    problems, info = [], {}
    rgb = frame.convert("RGB")
    gray = rgb.convert("L")

    # 2. dimensions
    want_w = page.rect.width * want_dpi / 72.0
    want_h = page.rect.height * want_dpi / 72.0
    info["size"] = rgb.size
    if abs(rgb.width - want_w) > 1.5 or abs(rgb.height - want_h) > 1.5:
        problems.append(f"size {rgb.size} but the page at {want_dpi} dpi should "
                        f"be about {want_w:.0f}x{want_h:.0f}")

    # 3. dpi stored in the file
    stored = frame.info.get("dpi")
    info["dpi"] = stored
    if not stored or abs(stored[0] - want_dpi) > 0.5 or abs(stored[1] - want_dpi) > 0.5:
        problems.append(f"the file stores dpi {stored} but the page was drawn "
                        f"at {want_dpi}")

    # 4. pixel-exact against a fresh render (lossless compression makes this valid)
    fresh = page.get_pixmap(dpi=want_dpi, alpha=False)
    exact = (rgb.size == (fresh.width, fresh.height)
             and rgb.tobytes() == fresh.samples)
    info["exact"] = exact
    if not exact:
        if rgb.size == (fresh.width, fresh.height):
            a, b = rgb.tobytes(), fresh.samples
            n = sum(1 for x, y in zip(a, b) if x != y)
            problems.append(f"pixels differ from a fresh render "
                            f"({n:,} of {len(b):,} bytes)")
        else:
            problems.append("pixels cannot match a fresh render (different size)")

    # 5. not blank, when the PDF says the page has content
    source_text = page.get_text()
    has_text = bool(source_text.strip())
    has_content = (has_text or bool(page.get_images(full=True))
                   or bool(page.get_drawings()))
    lo, hi = gray.getextrema()
    info["blank_pdf_page"] = not has_content
    if has_content and hi - lo <= INK_CONTRAST:
        problems.append("the page has content in the PDF but the frame is blank")

    if not has_text:
        # Nothing to compare text against. A page with no content is simply
        # blank; one with images or drawings but no text is a scan.
        info["lines"] = None
        info["ocr"] = None
        info["scanned"] = has_content
        return problems, info

    # 6. every text line of the PDF sits on ink
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
        problems.append(f"{len(bare)} text line(s) have no ink in the frame: "
                        f"{shown}{more}")

    # 7. an independent OCR reading contains the PDF's own words
    info["ocr"] = None
    if use_ocr and ocr.have_tesseract():
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            # Tell tesseract the real resolution. Without a dpi in the file it
            # guesses (about 70), and its layout analysis then misses words --
            # measured: 88% instead of 100% on the same page.
            rgb.save(tmp, dpi=(want_dpi, want_dpi))
            read = ocr.ocr_image(tmp)
        finally:
            os.unlink(tmp)
        got, got_sq = normalize(read), squash(read)
        words = {w for w in normalize(source_text).split() if len(w) > 3}
        if words:
            missing = sorted(w for w in words
                             if w not in got and squash(w) not in got_sq)
            cov = 1 - len(missing) / len(words)
            info["ocr"] = cov
            if cov < OCR_MIN_COVERAGE:
                problems.append(
                    f"OCR read only {cov:.0%} of the PDF's words from the frame "
                    f"({len(missing)} missing, e.g. {', '.join(missing[:6])})")
    return problems, info


def run(pdf_path: str, out_dir: str, dpi: int, compression: str, pages: str,
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

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.tiff")
    dpis = [page_dpi_for(doc[p], dpi) for p in targets]
    print(f"  {doc.page_count} page(s), converting {len(targets)} at {dpi} dpi, "
          f"{compression.upper()} compression -> {path}")
    for pno, d in zip(targets, dpis):
        if d != dpi:
            page = doc[pno]
            print(f"  page {pno + 1}: declared {page.rect.width / 72:.1f}x"
                  f"{page.rect.height / 72:.1f}in is very large -- drawn at "
                  f"{d} dpi instead of {dpi}")

    started = time.time()

    # Frames are produced one at a time and handed straight to the writer, so a
    # long PDF never holds every 300 dpi page in memory at once (one Letter page
    # is about 25 MB).
    def frame(i):
        img = render(doc[targets[i]], dpis[i])
        # Each frame carries its OWN dpi and compression.
        img.encoderinfo = {"dpi": (dpis[i], dpis[i]),
                           "compression": COMPRESSIONS[compression]}
        return img

    try:
        first = frame(0)
        first.save(path, "TIFF", save_all=True,
                   append_images=(frame(i) for i in range(1, len(targets))),
                   dpi=(dpis[0], dpis[0]),
                   compression=COMPRESSIONS[compression])
    except Exception as exc:
        print(f"  FAILED to write the TIFF: {exc}")
        return False
    size = os.path.getsize(path)
    print(f"  wrote {size / 1024:.0f} KB in {time.time() - started:.1f}s "
          f"-- now reading it back to verify")

    # Everything below reads the SAVED file, not what was in memory.
    results = []
    try:
        tif = Image.open(path)
        n_frames = getattr(tif, "n_frames", 1)
    except Exception as exc:
        print(f"  VERDICT: FAIL -- the saved TIFF cannot be opened: {exc}")
        return False

    for i, (pno, d) in enumerate(zip(targets, dpis)):
        if i >= n_frames:
            results.append((pno + 1, ["frame is missing from the TIFF"], {}))
            print(f"  page {pno + 1}: frame is missing from the TIFF  [FAIL]")
            continue
        try:
            tif.seek(i)
            problems, info = check_frame(doc[pno], tif, d, use_ocr)
        except Exception as exc:
            problems, info = [f"could not read the frame back: {exc}"], {}
        results.append((pno + 1, problems, info))

        w, h = info.get("size", (0, 0))
        parts = [f"{w}x{h}px"]
        if info.get("exact"):
            parts.append("pixel-exact")
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
    tif.close()

    frames_ok = n_frames == len(targets)
    n_bad = sum(1 for _, problems, _ in results if problems)
    ok = frames_ok and n_bad == 0

    print()
    print("  --- verification (read back from the saved TIFF) ---")
    print(f"  frames in file:   {n_frames} (expected {len(targets)})"
          f"  [{'PASS' if frames_ok else 'FAIL'}]")
    print(f"  pages verified:   {len(results) - n_bad}/{len(results)}")
    n_text = sum(1 for _, _, i in results if i.get("lines"))
    n_scan = sum(1 for _, _, i in results if i.get("scanned"))
    if n_scan:
        print(f"  note: {n_scan} page(s) have no text layer (scanned). Every check "
              f"except the text and OCR ones ran, so those pages are verified as "
              f"'pixel-exact to the PDF', not as 'every word present'.")
    if n_text and use_ocr and not ocr.have_tesseract():
        print(f"  note: tesseract not installed -- OCR word check skipped "
              f"({ocr.install_hint()})")
    print(f"  {size / 1024:.0f} KB total, {time.time() - started:.1f}s")
    print(f"  VERDICT: {'PASS -- every page converted and verified' if ok else 'FAIL -- see the problems above'}")
    print()
    return ok


parser = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("pdfs", nargs="+", help="PDF file(s), globs allowed")
parser.add_argument("--outdir", default=TIFF_OUT_DIR,
                    help=f"where the TIFF files go (default {TIFF_OUT_DIR})")
parser.add_argument("--dpi", type=int, default=300,
                    help="render resolution (default 300)")
parser.add_argument("--compression", choices=sorted(COMPRESSIONS), default="lzw",
                    help="lossless compression: lzw (default, opens everywhere) "
                         "or deflate (about 30%% smaller, a few old viewers "
                         "cannot open it)")
parser.add_argument("--pages", default="all",
                    help="e.g. 1, 1-3, 2,4 (default: all)")
parser.add_argument("--no-ocr", action="store_true",
                    help="skip the OCR word check (the other checks still run)")
args = parser.parse_args()

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
    if run(path, args.outdir, args.dpi, args.compression, args.pages,
           use_ocr=not args.no_ocr):
        ok += 1
    else:
        fail += 1

if len(paths) > 1:
    print("=" * 72)
    print(f"{ok} succeeded, {fail} failed, {len(paths)} total")

sys.exit(1 if fail else 0)
