"""
image_tiles.py -- read a dense page picture in horizontal strips, to get more of its words out of the 2B model.

WHY. The model sees every picture at a fixed budget of image tokens (512 in this project). A dense page of small text
spends that budget on the whole page, so the model reads only 77 to 90% of the words. Cutting the picture into 2 or 3
strips and reading each strip separately gives every strip the whole budget: the text is seen at 2x or 3x the detail.
It costs one model read per strip.

HOW THE CUTS ARE CHOSEN. A cut is placed in the emptiest band of rows near where an even split would fall (a gap
between two lines or paragraphs), so no line of text is cut in half and no overlap is needed. If the page has no
empty band there at all (a very tight page), the cut falls on the quietest row with a small overlap, and a block that
the overlap reads twice is removed by the same duplicate check the rest of the pipeline already uses.

The strips are read in order and their blocks are joined in order: a paragraph that runs across a cut becomes two
blocks, one on each side.
"""

import os
import tempfile

from PIL import Image

from blocks import DEFAULT_BASE_URL, Page, TableBlock, drop_duplicate_blocks, extract_page

SEARCH = 0.12           # look for a cut within this fraction of the page height either side of the even split
EMPTY = 4.0             # a row band counts as empty when its mean darkness (0-255) is below this
OVERLAP_PX = 14         # overlap used only when no empty band exists


def _row_ink(img: Image.Image):
    """Mean darkness (0-255) of every pixel row, via a 1-pixel-wide shrink (no numpy needed)."""
    grey = img.convert("L")
    col = grey.resize((1, grey.height), Image.Resampling.BOX)
    background = max(col.tobytes())                       # the page colour: the brightest row
    return [background - v for v in col.tobytes()]


def split_points(img: Image.Image, parts: int):
    """The (top, bottom, overlapped) pixel rows of each strip. parts <= 1 gives the whole page."""
    h = img.height
    if parts <= 1 or h < 200 * parts:
        return [(0, h)]
    ink = _row_ink(img)
    cuts = []
    for k in range(1, parts):
        ideal = round(h * k / parts)
        lo, hi = max(1, ideal - int(h * SEARCH)), min(h - 1, ideal + int(h * SEARCH))
        # the middle of the longest run of empty rows in the window; else the quietest single row
        best_run, run_start = (0, None), None
        for y in range(lo, hi):
            if ink[y] < EMPTY:
                run_start = y if run_start is None else run_start
                if y - run_start + 1 > best_run[0]:
                    best_run = (y - run_start + 1, run_start)
            else:
                run_start = None
        if best_run[0] >= 3:
            cut, overlap = best_run[1] + best_run[0] // 2, False
        else:
            cut, overlap = min(range(lo, hi), key=lambda y: ink[y]), True
        cuts.append((cut, overlap))
    boxes, top = [], 0
    for cut, overlap in cuts:
        boxes.append((top, min(h, cut + (OVERLAP_PX if overlap else 0))))
        top = max(0, cut - (OVERLAP_PX if overlap else 0))
    boxes.append((top, h))
    return boxes


def extract_page_tiled(image_path: str, parts: int = 2, extractor=extract_page, **kwargs):
    """Read the picture in `parts` strips and return (page, timing) like extract_page(return_timing=True).

    kwargs go to the extractor unchanged. `extractor` exists so tests can run without the model.
    """
    with Image.open(image_path) as img:
        img.load()
        boxes = split_points(img, parts)
        if len(boxes) == 1:
            return extractor(image_path, **kwargs)
        pages, total = [], {}
        with tempfile.TemporaryDirectory() as tmp:
            for n, (top, bottom) in enumerate(boxes, start=1):
                strip = os.path.join(tmp, f"strip{n}.png")
                img.crop((0, top, img.width, bottom)).save(strip)
                page, timing = extractor(strip, **kwargs)
                pages.append(page)
                for key, value in (timing or {}).items():
                    total[key] = total.get(key, 0) + value
    blocks = []
    for p in pages:
        blocks = _join_seam(blocks, list(p.blocks))
    merged = Page(analysis=pages[0].analysis, blocks=blocks)
    merged, _ = drop_duplicate_blocks(merged)             # only matters where an overlap was read twice
    return merged, total


def _join_seam(blocks, nxt):
    """Append the next strip's blocks. A table cut in two by a seam is joined back into one table.

    Only when the last block so far and the first block of the next strip are both tables with the same number of
    columns and the lower one has no header row of its own (the model reads a table's continuation as plain rows).
    """
    if (blocks and nxt and isinstance(blocks[-1], TableBlock) and isinstance(nxt[0], TableBlock)
            and blocks[-1].n_cols == nxt[0].n_cols and not nxt[0].has_header):
        top, bottom = blocks[-1], nxt[0]
        rows = list(top.rows) + list(bottom.rows)
        blocks = blocks[:-1] + [top.model_copy(update={"rows": rows, "n_data_rows": len(rows)})]
        nxt = nxt[1:]
    return blocks + nxt


AUTO_MIN_HEIGHT = 1400  # a picture at least this tall is a full page; shorter ones (crops, small screenshots) are read whole


BIG_MODEL_PARAMS = 3e9  # a model of at least this many parameters reads a full page in one go (measured on the 4B)


def model_params(base_url: str):
    """The parameter count of the model the server has loaded (from /v1/models), or None if it cannot be asked."""
    import requests
    try:
        data = requests.get(base_url.rstrip("/") + "/v1/models", timeout=3).json()["data"][0]
        return int(data["meta"]["n_params"])
    except Exception:
        return None


def auto_parts(image_path: str, big_model: bool = False) -> int:
    """How many strips to read a picture in: 2 for a full-page picture, 1 for anything shorter.

    big_model=True (a 3B+ model) reads every picture whole: on the 4B, one read of the dense test pages scored 97/91-99/99%
    of the words against 90/100/99% in two strips, with a better reading order, and takes half the time. The 2B
    still gets two strips, which is what raised its recall.

    The environment variable IDOX_SPLIT (a whole number) overrides the rule for every picture, e.g. IDOX_SPLIT=1 reads
    every picture whole. Used when a model reads a page completely in one go and the extra strips only cost time.
    """
    forced = os.environ.get("IDOX_SPLIT", "").strip()
    if forced.isdigit() and int(forced) >= 1:
        return int(forced)
    with Image.open(image_path) as img:
        return 2 if img.height >= AUTO_MIN_HEIGHT and not big_model else 1


def extract_page_auto(image_path: str, parts: int = None, extractor=extract_page, **kwargs):
    """extract_page(return_timing=True) that reads a full-page picture in 2 strips unless told otherwise.

    parts=None decides from the picture's height (see auto_parts); parts=1 forces one read of the whole picture.
    """
    big = False
    if not parts and extractor is extract_page and os.environ.get("IDOX_SPLIT", "").strip() == "":
        params = model_params(kwargs.get("base_url") or DEFAULT_BASE_URL)
        big = bool(params and params >= BIG_MODEL_PARAMS)
    n = parts if parts else auto_parts(image_path, big_model=big)
    if n > 1:
        result = extract_page_tiled(image_path, parts=n, extractor=extractor, **kwargs)
    else:
        result = extractor(image_path, **kwargs)
    if extractor is not extract_page:
        return result                                   # tests with a stand-in extractor get exactly what it returned
    # Measure the tables' look (column widths, alignment, header fill) from the picture itself: no OCR, pixels only.
    from image_look import apply_image_look
    if isinstance(result, tuple):
        return (apply_image_look(result[0], image_path),) + tuple(result[1:])
    return apply_image_look(result, image_path)
