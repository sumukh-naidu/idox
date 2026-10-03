"""
tiff_to_pdf.py -- convert a (multi-page) TIFF into a SEARCHABLE PDF.

Every TIFF frame becomes one PDF page:

    the picture   the frame, embedded losslessly and unchanged, so the page looks
                  exactly like the scan
    the text      an invisible OCR text layer on top of it, so the PDF can be
                  searched and its text copied

NO MODEL IS INVOLVED HERE. The look of the page is copied, not re-created, so
there is nothing for a model to misread. The only reader is Tesseract, and only
for the invisible text layer. (image_to_pdf.py is the model-based route: it
re-typesets the page from what the model reads, which is slower and can change
letters. Use it only when a rebuilt, re-typeset PDF is wanted.)

Output:   tiff_to_pdf_input/report.tiff  ->  tiff_to_pdf_output/report.pdf

PAGE SIZE is the frame's physical size: pixels / dpi * 72 points, using the dpi
stored in the TIFF, so a 300 dpi A4 scan becomes an A4 page. A frame with no
usable dpi is assumed to be 300 dpi and the report says so.

THE TEXT LAYER IS ONLY AS GOOD AS OCR. Tesseract misses text printed white on a
dark background and reads accented letters as plain ones. The picture is always
exact; the searchable text may be incomplete on such pages. That is why the
verdict rests on the picture checks, and the OCR figure is shown as information.

HOW IT PROVES EVERYTHING WAS CONVERTED. The saved PDF is opened again and every
page is checked against the TIFF frame it came from (see check_page()):

  1. PAGES        the PDF has exactly as many pages as the TIFF has frames.
  2. PAGE SIZE    each page is the frame's physical size.
  3. ONE PICTURE  each page holds exactly one image, covering the whole page.
  4. PIXEL-EXACT  that image, pulled back out of the PDF, is byte-for-byte the
                  TIFF frame.
  5. TEXT LAYER   the words Tesseract read from the frame (kept from building the
                  text layer, not read a second time) are really present in the
                  saved PDF's text, so searching works.

Usage:
    .venv/bin/python tiff_to_pdf.py tiff_to_pdf_input/report.tiff
    .venv/bin/python tiff_to_pdf.py tiff_to_pdf_input/*.tif*
    .venv/bin/python tiff_to_pdf.py scan.tiff --no-text     # picture only, no OCR
"""

import argparse
import glob
import io
import os
import sys
import tempfile
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pymupdf
import pytesseract
from PIL import Image, ImageSequence

import ocr
from blocks import normalize, squash

TIFF_DIR = "tiff_to_pdf_input"
PDF_OUT_DIR = "tiff_to_pdf_output"

DEFAULT_DPI = 300
# A stored resolution below this is not a real dpi (some writers store 1 or 0).
MIN_REAL_DPI = 10
TEXT_LAYER_MIN = 0.90

# Tesseract normally spreads ONE page over several threads. With several pages
# in flight at once that oversubscribes the CPU and is slower than one thread
# per page, so each tesseract process is limited to one thread (set here, before
# any is started, and inherited by every one).
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
DEFAULT_WORKERS = min(4, os.cpu_count() or 1)


def to_rgb(frame: Image.Image) -> Image.Image:
    """Any TIFF mode -> plain RGB, the one form a PDF page can hold losslessly.

    Fax-style 1-bit, greyscale, palette, CMYK, transparent and 16-bit frames all
    occur in real TIFFs. Transparency is flattened onto white, since a scan has
    no meaningful "see-through".
    """
    mode = frame.mode
    if mode in ("RGBA", "LA", "PA") or (mode == "P" and "transparency" in frame.info):
        rgba = frame.convert("RGBA")
        base = Image.new("RGB", rgba.size, "white")
        base.paste(rgba, mask=rgba.getchannel("A"))
        return base
    if mode in ("I;16", "I;16L", "I;16B", "I"):
        return frame.convert("I").point(lambda v: v * (1 / 256)).convert("L").convert("RGB")
    if mode == "F":
        return frame.point(lambda v: v * 255).convert("L").convert("RGB")
    return frame.convert("RGB")


def _varies(frame: Image.Image) -> bool:
    """Does the ORIGINAL frame contain more than one tone?"""
    ext = frame.getextrema()
    if frame.mode in ("RGB", "RGBA", "CMYK", "LA", "PA"):
        return any(lo != hi for lo, hi in ext)
    return ext[0] != ext[1]


def frame_dpi(frame: Image.Image):
    """(x_dpi, y_dpi, assumed) -- the stored resolution, or 300 when unusable."""
    stored = frame.info.get("dpi")
    if stored and stored[0] >= MIN_REAL_DPI and stored[1] >= MIN_REAL_DPI:
        return float(stored[0]), float(stored[1]), False
    return float(DEFAULT_DPI), float(DEFAULT_DPI), True


def text_layer_pdf(rgb: Image.Image, dpi_x: float, dpi_y: float) -> bytes:
    """Tesseract's text-only PDF for one frame: invisible text, no picture."""
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        # The dpi has to be in the file, or tesseract guesses one and the text
        # ends up laid out for a different page size (and reads worse).
        rgb.save(tmp, dpi=(dpi_x, dpi_y))
        return pytesseract.image_to_pdf_or_hocr(
            tmp, extension="pdf", config="-c textonly_pdf=1")
    finally:
        os.unlink(tmp)


def check_page(page, doc, rgb: Image.Image, dpi_x: float, dpi_y: float,
               with_text: bool, source_varies: bool = True,
               layer_text: str = "", layer_error: str = None):
    """Verify one PDF page (read back from the saved file) against its frame.

    Returns (problems, info).
    """
    problems, info = [], {}

    # 1b. the colour conversion kept the picture. The checks below compare the PDF
    # with the converted RGB image, so a conversion that turned a real scan
    # solid black or white would otherwise still "match". If the original frame
    # has more than one tone, the converted one must too.
    lo, hi = rgb.convert("L").getextrema()
    if source_varies and lo == hi:
        problems.append("the TIFF frame has content but converting it to RGB "
                        "left one flat colour -- the conversion lost the image")

    # 2. page size
    want_w = rgb.width / dpi_x * 72.0
    want_h = rgb.height / dpi_y * 72.0
    info["size_pt"] = (page.rect.width, page.rect.height)
    if abs(page.rect.width - want_w) > 0.5 or abs(page.rect.height - want_h) > 0.5:
        problems.append(f"page is {page.rect.width:.1f}x{page.rect.height:.1f}pt "
                        f"but the frame's physical size is {want_w:.1f}x{want_h:.1f}pt")

    # 3. exactly one picture, covering the page
    images = page.get_images(full=True)
    info["images"] = len(images)
    if len(images) != 1:
        problems.append(f"{len(images)} images on the page, expected exactly 1")
    else:
        bbox = page.get_image_bbox(images[0])
        if (abs(bbox.x0) > 0.5 or abs(bbox.y0) > 0.5
                or abs(bbox.width - page.rect.width) > 0.5
                or abs(bbox.height - page.rect.height) > 0.5):
            problems.append(f"the picture covers {bbox} but the page is "
                            f"{page.rect}")

        # 4. pixel-exact: pull the picture back out of the PDF
        try:
            back = Image.open(io.BytesIO(doc.extract_image(images[0][0])["image"]))
            same = back.convert("RGB").tobytes() == rgb.tobytes() \
                and back.size == rgb.size
        except Exception as exc:
            same = False
            problems.append(f"could not read the picture back: {exc}")
        info["exact"] = same
        if not same and not any("could not read" in p for p in problems):
            problems.append("the picture in the PDF is not identical to the "
                            "TIFF frame")

    # 5. the words OCR read from the frame are really in the PDF's text.
    # layer_text is what Tesseract produced for this frame when the text layer
    # was built -- the SAME reading, kept, not a second OCR run (which was
    # measured at 63s of a 145s run and, being deterministic, told us nothing
    # new). The check is whether that text survived into the saved file.
    info["text"] = None
    if with_text and layer_error is not None:
        # Without this, a page whose text layer failed would look exactly like a
        # page with no text on it (no words to look for) and pass.
        problems.append(f"the text layer could not be built: {layer_error}")
    elif with_text:
        words = {w for w in normalize(layer_text).split() if len(w) > 3}
        pdf_text = page.get_text()
        got, got_sq = normalize(pdf_text), squash(pdf_text)
        info["words"] = len(words)
        if words:
            missing = sorted(w for w in words
                             if w not in got and squash(w) not in got_sq)
            cov = 1 - len(missing) / len(words)
            info["text"] = cov
            if cov < TEXT_LAYER_MIN:
                problems.append(
                    f"only {cov:.0%} of the words OCR read are in the PDF's text "
                    f"layer ({len(missing)} missing, e.g. {', '.join(missing[:6])})")
    return problems, info


def run(tiff_path: str, out_dir: str, with_text: bool,
        workers: int = DEFAULT_WORKERS) -> bool:
    name = os.path.splitext(os.path.basename(tiff_path))[0]
    print("=" * 72)
    print(tiff_path)
    print("=" * 72)

    try:
        tif = Image.open(tiff_path)
        n_frames = getattr(tif, "n_frames", 1)
    except Exception as exc:
        print(f"  COULD NOT OPEN: {exc}")
        return False

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.pdf")
    print(f"  {n_frames} frame(s) -> {path}"
          + (f"  (OCR on {workers} page(s) at a time)" if with_text
             else "  (picture only, no text layer)"))

    started = time.time()
    doc = pymupdf.open()
    dpis = []          # (dpi_x, dpi_y) used for each page, for the checks below
    layer_texts = []   # the OCR text of each page's text layer, for the checks

    # The picture goes on every page straight away (fast). OCR is the slow part,
    # so it runs in worker threads while the next frames are being placed; the
    # results are laid on their pages in page order. Only a few pages are held
    # in flight at once -- a 300 dpi frame is about 25 MB -- so memory stays flat
    # however long the TIFF is.
    pool = ThreadPoolExecutor(max_workers=workers) if with_text else None
    pending = deque()          # (page number, future), in page order
    layer_failed = {}          # page number -> why its text layer could not be built

    def finish(item):
        i, future = item
        try:
            overlay = pymupdf.open("pdf", future.result())
            # The page is looked up by number HERE, not held from when it was
            # created: PyMuPDF invalidates Page objects it handed out earlier
            # whenever another page is added, and pages are still being added
            # while earlier ones wait for OCR ("page is None").
            pdf_page = doc[i]
            pdf_page.show_pdf_page(pdf_page.rect, overlay, 0)
            layer_texts[i] = overlay[0].get_text()
        except Exception as exc:
            layer_failed[i] = str(exc)
            print(f"  frame {i + 1}: text layer FAILED: {exc}")

    for i, fr in enumerate(ImageSequence.Iterator(tif)):
        rgb = to_rgb(fr)
        dx, dy, assumed = frame_dpi(fr)
        dpis.append((dx, dy))
        layer_texts.append("")
        if fr.mode != "RGB":
            print(f"  frame {i + 1}: mode {fr.mode} converted to RGB")
        if assumed:
            print(f"  frame {i + 1}: no usable dpi stored -- assumed {DEFAULT_DPI}")

        page = doc.new_page(width=rgb.width / dx * 72.0,
                            height=rgb.height / dy * 72.0)
        buf = io.BytesIO()
        rgb.save(buf, "PNG")
        page.insert_image(page.rect, stream=buf.getvalue())

        if with_text:
            pending.append((i, pool.submit(text_layer_pdf, rgb, dx, dy)))
            while len(pending) >= workers * 2:
                finish(pending.popleft())

    while pending:
        finish(pending.popleft())
    if pool:
        pool.shutdown()
    tif.close()

    if doc.page_count == 0:
        print("  VERDICT: FAIL -- the TIFF has no readable frames")
        return False

    try:
        doc.save(path, deflate=True)
    except Exception as exc:
        print(f"  FAILED to write the PDF: {exc}")
        return False
    doc.close()
    size = os.path.getsize(path)
    print(f"  wrote {size / 1024:.0f} KB in {time.time() - started:.1f}s "
          f"-- now reading it back to verify")

    # Everything below reads the SAVED PDF and the ORIGINAL TIFF again.
    results = []
    saved = pymupdf.open(path)
    tif = Image.open(tiff_path)
    for i, fr in enumerate(ImageSequence.Iterator(tif)):
        if i >= saved.page_count:
            results.append((i + 1, ["page is missing from the PDF"], {}))
            print(f"  page {i + 1}: page is missing from the PDF  [FAIL]")
            continue
        try:
            problems, info = check_page(saved[i], saved, to_rgb(fr),
                                        dpis[i][0], dpis[i][1], with_text,
                                        source_varies=_varies(fr),
                                        layer_text=layer_texts[i],
                                        layer_error=layer_failed.get(i))
        except Exception as exc:
            problems, info = [f"could not verify the page: {exc}"], {}
        results.append((i + 1, problems, info))

        parts = []
        if info.get("size_pt"):
            parts.append(f"{info['size_pt'][0]:.0f}x{info['size_pt'][1]:.0f}pt")
        if info.get("exact"):
            parts.append("picture pixel-exact")
        if info.get("text") is not None:
            parts.append(f"text layer {info['text']:.0%} of {info['words']} OCR words")
        elif with_text and info.get("words") == 0:
            parts.append("no text found on the page")
        print(f"  page {i + 1}: {', '.join(parts)}  "
              f"[{'PASS' if not problems else 'FAIL'}]")
        for p in problems:
            print(f"       - {p}")
    tif.close()

    pages_ok = saved.page_count == n_frames
    n_bad = sum(1 for _, problems, _ in results if problems)
    ok = pages_ok and n_bad == 0
    n_pages = saved.page_count
    saved.close()

    print()
    print("  --- verification (read back from the saved PDF) ---")
    print(f"  pages in PDF:     {n_pages} (TIFF has {n_frames} frame(s))"
          f"  [{'PASS' if pages_ok else 'FAIL'}]")
    print(f"  pages verified:   {len(results) - n_bad}/{len(results)}")
    if with_text:
        print("  note: the text layer is Tesseract's reading. It can miss text "
              "printed white on dark backgrounds and turns accented letters into "
              "plain ones; the picture on every page is exact regardless.")
    print(f"  {size / 1024:.0f} KB total, {time.time() - started:.1f}s")
    print(f"  VERDICT: {'PASS -- every frame converted and verified' if ok else 'FAIL -- see the problems above'}")
    print()
    return ok


parser = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("tiffs", nargs="+", help="TIFF file(s), globs allowed")
parser.add_argument("--outdir", default=PDF_OUT_DIR,
                    help=f"where the PDFs go (default {PDF_OUT_DIR})")
parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="pages read by OCR at the same time (default "
                         f"{DEFAULT_WORKERS}; 1 = one page at a time)")
parser.add_argument("--no-text", action="store_true",
                    help="picture only: skip OCR and write no text layer")
args = parser.parse_args()

if not args.no_text and not ocr.have_tesseract():
    raise SystemExit(f"a searchable PDF needs OCR, and {ocr.install_hint()}\n"
                     f"(or pass --no-text for a picture-only PDF)")

paths = []
for pattern in args.tiffs:
    paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[")
                 else [pattern])

ok = fail = 0
for path in paths:
    if not os.path.exists(path):
        print(f"skipping {path}: not found")
        continue
    if run(path, args.outdir, with_text=not args.no_text,
           workers=max(args.workers, 1)):
        ok += 1
    else:
        fail += 1

if len(paths) > 1:
    print("=" * 72)
    print(f"{ok} succeeded, {fail} failed, {len(paths)} total")

sys.exit(1 if fail else 0)
