"""
image_look.py -- measure a table's look from the PICTURE it was read from: column widths, per-column alignment, header fill.

A picture has no text layer, so nothing here reads any text: no OCR. The model has already said what the cells contain;
this module only looks at pixels, the way pdf_look.py looks at a PDF's drawings.

WHAT IT FINDS. A ruled table is a stack of long horizontal lines (the row borders) crossed by vertical lines (the column
borders). From the lines it gets the cell rectangles. Inside each cell, where the dark pixels sit between the cell's
left and right edges says whether the text is left-aligned, centred or right-aligned. The most common colour in the
header row is its fill (when it is not the page colour).

A table is only restyled when the grid found has exactly the column count the model returned and at least as many rows
as the model's table; otherwise the model's table is left as it is. A table with no ruled lines is left as it is.

Pillow only (no numpy).
"""
from collections import Counter
from typing import List, Optional

from PIL import Image

from blocks import Page, TableBlock

DARK = 185            # a pixel darker than this (0-255) counts for a rule (thin anti-aliased borders are grey)
LINE_SHARE = 0.30     # a pixel row is a horizontal rule when this share of the picture's width is dark
VLINE_SHARE = 0.55    # a pixel column is a vertical rule when this share of the table's height is dark
MIN_TABLE_W = 0.25    # a table is at least this share of the picture's width
THICK_BAND = 8        # a run of dark pixel rows thicker than this is a filled band, not a rule
CELL_PAD = 12         # text starting within this many pixels of its cell's left edge counts as left-aligned


class LookTable(TableBlock):
    """A table whose look was measured from a picture: widths (relative), alignment, header fill."""
    col_widths: List[float] = []
    header_align: List[str] = []
    col_align: List[str] = []
    header_fill: Optional[str] = None
    header_text: Optional[str] = None      # hex colour for the header text when the fill is dark


def _binary(img: Image.Image) -> Image.Image:
    return img.convert("L").point(lambda v: 255 if v < DARK else 0)


def _runs(flags) -> List[tuple]:
    """(start, end) of every run of True in a list of booleans."""
    out, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def _profile(binary: Image.Image, axis: str) -> List[float]:
    """Share of dark pixels per row (axis 'y') or per column (axis 'x'), via a 1-pixel-wide shrink."""
    size = (1, binary.height) if axis == "y" else (binary.width, 1)
    return [v / 255 for v in binary.resize(size, Image.Resampling.BOX).tobytes()]


def _longest_run(values) -> tuple:
    """(start, end) of the longest run of non-zero values, or None."""
    best, start = None, None
    for i, v in enumerate(list(values) + [0]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if best is None or i - start > best[1] - best[0] + 1:
                best = (start, i - 1)
            start = None
    return best


def _dark_extent(binary: Image.Image, y: int):
    """(first, last) of the longest unbroken dark stretch in one pixel row: a ruled line, not a line of text."""
    return _longest_run(binary.crop((0, y, binary.width, y + 1)).tobytes())


def find_grids(img: Image.Image) -> List[dict]:
    """The ruled tables in the picture, top to bottom: {"x0","x1","rows": [y...], "cols": [x...]} in pixels."""
    binary = _binary(img)
    w = binary.width
    rows = _profile(binary, "y")
    rules = []
    for a, b in _runs([r >= LINE_SHARE for r in rows]):
        # a thick band is a dark header fill: its top and bottom edges are the row's two borders
        thick = b - a + 1 > THICK_BAND
        for y in ((a, b) if thick else ((a + b) // 2,)):
            ext = _dark_extent(binary, y)      # a band's own edge rows: its middle is broken by the header text
            if ext and ext[1] - ext[0] >= MIN_TABLE_W * w:
                rules.append((y, ext[0], ext[1]))
    # horizontal rules with the same left/right ends and no big gap between them belong to one table
    tables, current = [], []
    for rule in rules:
        if current and (abs(rule[1] - current[-1][1]) > 6 or abs(rule[2] - current[-1][2]) > 6
                        or rule[0] - current[-1][0] > binary.height * 0.25):
            tables.append(current)
            current = []
        current.append(rule)
    if current:
        tables.append(current)
    grids = []
    for t in tables:
        if len(t) < 3:                      # a header row and one data row need at least three rules
            continue
        x0, x1 = min(r[1] for r in t), max(r[2] for r in t)
        top, bottom = t[0][0], t[-1][0]
        cols = _vertical_rules(binary.crop((x0, top, x1 + 1, bottom + 1)), x0)
        grids.append({"x0": x0, "x1": x1, "rows": [r[0] for r in t], "cols": cols})
    return grids


def _vertical_rules(region: Image.Image, x_offset: int) -> List[int]:
    """Pixel columns (in picture coordinates) of the column borders, including the table's left and right edges.

    A border is a column that is dark over most of the table's height in ONE unbroken stretch (text columns are not).
    """
    prof = _profile(region, "x")
    out = []
    for a, b in _runs([p >= VLINE_SHARE * 0.6 for p in prof]):
        x = (a + b) // 2
        col = region.crop((x, 0, x + 1, region.height)).tobytes()
        run = _longest_run(col)
        if run and run[1] - run[0] + 1 >= VLINE_SHARE * region.height:
            out.append(x_offset + x)
    return out


def _cell(img: Image.Image, x0, y0, x1, y1, inset=3):
    return img.crop((x0 + inset, y0 + inset, max(x0 + inset + 1, x1 - inset), max(y0 + inset + 1, y1 - inset)))


def _background(cell: Image.Image):
    """Most common colour of a cell (its fill), as an RGB tuple."""
    small = cell.convert("RGB")
    small.thumbnail((60, 60))
    return Counter(small.getdata()).most_common(1)[0][0]


def _align(cell: Image.Image, bg) -> Optional[str]:
    """left / center / right for the ink inside one cell, from its distance to the cell's left and right edges."""
    rgb = cell.convert("RGB")
    # ink is whatever differs strongly from the cell's own background
    ink = Image.new("L", rgb.size, 0)
    ink.putdata([255 if sum(abs(p[i] - bg[i]) for i in range(3)) > 150 else 0 for p in rgb.getdata()])
    prof = _profile(ink, "x")
    cols = [i for i, p in enumerate(prof) if p > 0]
    if not cols:
        return None
    left, right = cols[0], len(prof) - 1 - cols[-1]
    width = len(prof)
    if left <= max(CELL_PAD, 0.08 * width):
        return "left"
    if abs(left - right) <= max(6, 0.1 * width):
        return "center"
    if right <= max(CELL_PAD, 0.08 * width):
        return "right"
    return "left"


def _hex(rgb) -> str:
    return "".join(f"{v:02X}" for v in rgb[:3])


def measure_table(img: Image.Image, grid: dict, model: TableBlock) -> Optional[LookTable]:
    """The model's table plus the look measured from its grid, or None when the grid does not fit the table."""
    cols = grid["cols"]
    n = model.n_cols
    n_rows = len(model.rows) + (1 if model.has_header else 0)
    if len(cols) != n + 1 or len(grid["rows"]) - 1 < n_rows:
        return None
    ys = grid["rows"]
    rgb = img.convert("RGB")

    def aligns(row_indices):
        out = []
        for c in range(n):
            votes = Counter()
            for r in row_indices:
                cell = _cell(rgb, cols[c], ys[r], cols[c + 1], ys[r + 1])
                a = _align(cell, _background(cell))
                if a:
                    votes[a] += 1
            out.append(votes.most_common(1)[0][0] if votes else "left")
        return out

    first_data = 1 if model.has_header else 0
    data_rows = list(range(first_data, n_rows))
    head_align = aligns([0]) if model.has_header else []
    col_align = aligns(data_rows)

    fill = text = None
    if model.has_header:
        fills = Counter(_background(_cell(rgb, cols[c], ys[0], cols[c + 1], ys[1])) for c in range(n))
        colour = fills.most_common(1)[0][0]
        if min(colour) < 240:                                 # not the page's own white
            fill = _hex(colour)
            text = "FFFFFF" if sum(colour) / 3 < 110 else None
    widths = [float(cols[c + 1] - cols[c]) for c in range(n)]
    return LookTable(**model.model_dump(), col_widths=widths, header_align=head_align, col_align=col_align,
                     header_fill=fill, header_text=text)


def apply_image_look(page: Page, image_path: str) -> Page:
    """A copy of the page whose tables carry the look measured from the picture. Pages without ruled tables, and
    tables whose grid does not fit, come back exactly as they were."""
    models = [i for i, b in enumerate(page.blocks) if isinstance(b, TableBlock)]
    if not models:
        return page
    try:
        with Image.open(image_path) as im:
            im.load()
            grids = find_grids(im)
            if not grids:
                return page
            blocks = list(page.blocks)
            taken = set()
            for i in models:
                for g, grid in enumerate(grids):
                    if g in taken:
                        continue
                    look = measure_table(im, grid, blocks[i])
                    if look is not None:
                        blocks[i] = look
                        taken.add(g)
                        break
    except Exception:
        return page
    return page.model_copy(update={"blocks": blocks})
