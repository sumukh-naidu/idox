"""
to_xlsx.py -- Tier 3 for spreadsheets: turn extracted Page objects into .xlsx.

NO MODEL IS INVOLVED HERE, exactly as in to_docx.py. The model's job ended when
it produced the Page objects; this decides only how they are written out.

That separation is why this is a new writer rather than a second pipeline. Page
was defined as a STRUCTURAL schema -- TextBlock and TableBlock, never business
fields -- so it was never tied to Word. The same extraction, the same checks
and the same repairs feed both:

    PDF -> Tier 0 + model -> Page --+--> to_docx.py -> .docx
                                    +--> to_xlsx.py -> .xlsx

--------------------------------------------------------------------------
WHY A SPREADSHEET IS A HARDER TARGET THAN A DOCUMENT

A Word document is a FLOW: blocks one after another, and a paragraph placed
slightly wrong still reads correctly. A spreadsheet is a GRID: every value
needs an exact row and column. A column shifted by one is visibly broken, and
a number stored as text silently breaks every formula built on it.

Three decisions shape this file:

  ONE SHEET FOR THE WHOLE DOCUMENT. Every page is stacked into the same sheet
  in reading order. The trade-off to know about: tables from different pages
  share the same columns, so a 4-column table on page 1 and a 3-column table on
  page 3 occupy overlapping column letters. That is fine for reading and
  awkward for sorting or filtering across the whole sheet.

  TABLES AS GRIDS, PROSE AS CONTEXT. Headings and paragraphs go in column A in
  reading order rather than being discarded, so nothing from the PDF is lost
  and the coverage checks still mean something.

  EVERYTHING AS TEXT. "$1,020.00" stays "$1,020.00" rather than becoming the
  number 1020 with currency formatting. Nothing can be silently misconverted
  and verification stays exact -- at the cost of needing manual conversion
  before anything will sum or chart.
"""

from typing import List

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from blocks import Page, TableBlock, normalize, squash

# Excel positions and sizes a floating image in PIXELS, at a 96 dpi screen
# reference -- unlike Word, which works in inches/points. This is what
# converts the size the image was DISPLAYED at on the PDF page (width_in,
# height_in -- the same figures to_docx.py uses) into what openpyxl expects.
EXCEL_IMAGE_DPI = 96

# A cap so one oversized picture cannot blow out the whole sheet. Chosen to
# match the equivalent cap in to_docx.py (a 6.5in-wide text column), so an
# image looks a similar size whether it lands in Word or Excel.
MAX_IMAGE_IN = (6.5, 8.0)

# Rough character-per-pixel and point-per-pixel conversions Excel uses for
# column width / row height, so the cell the image sits in is sized to frame
# it rather than leaving the picture to overflow into neighbouring rows.
PX_PER_CHAR = 7.0
PX_PER_POINT = 96.0 / 72.0

# Excel treats a cell opening with any of these as a formula. A table cell
# reading "-5%" or "=N/A" would be reinterpreted or rejected outright, which
# would break the promise that text is preserved exactly.
FORMULA_STARTS = ("=", "+", "-", "@")


def _write_cell(ws, row: int, col: int, value: str, bold: bool = False):
    """Write one cell as literal text, whatever it contains."""
    cell = ws.cell(row=row, column=col)
    text = "" if value is None else str(value)

    cell.value = text
    if text.startswith(FORMULA_STARTS):
        # The same mechanism Excel uses when a cell is formatted as Text: the
        # content displays unchanged but is never parsed as a formula.
        cell.quotePrefix = True
    cell.alignment = Alignment(vertical="top", wrap_text=False)
    if bold:
        cell.font = Font(bold=True)
    return cell


def _autosize(ws, max_width: int = 60):
    """Widen columns to fit their content, within reason.

    Excel's default column shows about eight characters, which makes every
    table look empty. Capped, so one long paragraph in column A does not
    produce a column wider than the screen.
    """
    widths = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            longest = max((len(part) for part in str(cell.value).split("\n")),
                          default=0)
            widths[cell.column] = min(
                max(widths.get(cell.column, 0), longest + 2), max_width)
    for col, width in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = width


def _add_image(ws, row: int, img: dict, problems: List[str], where: str) -> int:
    """Embed one extracted picture in its OWN cell (column A), own row.

    "Own cell" is why this is a single row rather than the image floating
    across several: the row height and column A width are both set to frame
    the image, so nothing above or below it shares that row, and the picture
    does not overlap a table or a paragraph placed next to it.

    Sizing is taken from width_in/height_in -- how big the image was DISPLAYED
    on the PDF page, the same figures to_docx.py uses -- never from raw pixel
    dimensions. A 1352x969 logo shown at 6.13in comes out 6.13in in Excel too,
    not some arbitrary blown-up size. This is what "maintaining the same
    format as the image" means here: the aspect ratio and displayed scale of
    the source are preserved, just translated into Excel's pixel/point/char
    units instead of Word's inches.

    Returns the number of grid rows this image occupies, so the caller knows
    how far to advance past it.
    """
    width_in = min(img.get("width_in") or 0, MAX_IMAGE_IN[0]) or 2.0
    height_in = min(img.get("height_in") or 0, MAX_IMAGE_IN[1]) or 2.0

    # If only one dimension was usable, rescale the other to keep the image's
    # real aspect ratio rather than distorting it into a square.
    px = img.get("px") or (0, 0)
    if px[0] and px[1]:
        ratio = px[1] / px[0]
        if img.get("width_in") and not img.get("height_in"):
            height_in = width_in * ratio
        elif img.get("height_in") and not img.get("width_in"):
            width_in = height_in / ratio

    try:
        xl_img = XLImage(img["path"])
    except Exception as exc:
        problems.append(f"{where}: could not embed {img['path']}: {exc}")
        return 1

    xl_img.width = width_in * EXCEL_IMAGE_DPI
    xl_img.height = height_in * EXCEL_IMAGE_DPI

    # Frame the image in its cell: widen column A and heighten this row to
    # match, so the picture sits inside its own bounds rather than spilling
    # over the table above/below it or the text alongside it.
    col_letter = get_column_letter(1)
    want_chars = (width_in * EXCEL_IMAGE_DPI) / PX_PER_CHAR
    want_points = (height_in * EXCEL_IMAGE_DPI) / PX_PER_POINT

    current = ws.column_dimensions[col_letter].width or 0
    ws.column_dimensions[col_letter].width = max(current, want_chars)
    ws.row_dimensions[row].height = max(
        ws.row_dimensions[row].height or 0, want_points)

    ws.add_image(xl_img, f"{col_letter}{row}")
    return 1


def build_xlsx(pages: List[Page], path: str, image_sets: List[list] = None,
               sheet_name: str = "Extracted"):
    """Write every page into one sheet. Returns a list of problems found.

    image_sets, when given, is one list of extracted images per page, each
    carrying the fraction of that page's text sitting above it -- the same
    structure build_docx() consumes. That fraction is what places a picture in
    reading order among the blocks: "65% of the text is above this image"
    becomes "insert it after 65% of this page's blocks". A page with no images
    contributes an empty list and nothing about that page's output changes.

    Each image is embedded in its own dedicated cell (see _add_image) rather
    than mixed into a table or a text cell, so it never obscures data.
    """
    problems: List[str] = []
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]
    row = 1

    for pno, page in enumerate(pages, start=1):
        images = (image_sets[pno - 1] if image_sets
                  and pno - 1 < len(image_sets) else [])

        # Same interleaving trick as to_docx.py: turn each image's "fraction
        # of text above me" into a block index to sit after, sorted so several
        # images on one page stay in page order.
        n = len(page.blocks)
        # See the matching comment in to_docx.py: with an empty page every
        # image's position collapses to 0, tying every tuple on its first
        # element -- without key=, Python would fall back to comparing the
        # image dicts themselves, which crashes (dicts have no ordering).
        pending = sorted(
            ((min(n, round(im.get("frac_above", 1.0) * n)), im) for im in images),
            key=lambda t: t[0],
        )

        for bno, block in enumerate(page.blocks):
            where = f"page {pno} block[{bno}]"

            while pending and pending[0][0] <= bno:
                row += 1          # blank row before the image
                row += _add_image(ws, row, pending.pop(0)[1], problems, where)
                row += 1          # blank row after the image

            if isinstance(block, TableBlock):
                if not block.rows:
                    problems.append(f"{where}: table skipped (no rows)")
                    continue

                width = (len(block.header) if block.has_header
                         else len(block.rows[0]))
                if width == 0:
                    problems.append(f"{where}: table skipped (no columns)")
                    continue

                if block.has_header:
                    for i, label in enumerate(block.header[:width], start=1):
                        _write_cell(ws, row, i, label, bold=True)
                    row += 1

                for r, source_row in enumerate(block.rows):
                    # Force the row onto the grid. A ragged row is a real
                    # extraction fault, but a spreadsheet row hanging into a
                    # neighbouring column is worse than a padded one.
                    if len(source_row) != width:
                        problems.append(
                            f"{where} row {r}: had {len(source_row)} cells, "
                            f"grid is {width} -- "
                            f"{'padded' if len(source_row) < width else 'truncated'}"
                        )
                    cells = (list(source_row) + [""] * width)[:width]
                    for i, value in enumerate(cells, start=1):
                        _write_cell(ws, row, i, value)
                    row += 1

                row += 1          # blank row after each table
                continue

            text = block.text.strip()
            if not text:
                problems.append(f"{where}: empty {block.kind}, skipped")
                continue

            _write_cell(ws, row, 1, text, bold=(block.kind == "heading"))
            row += 2              # blank row after each piece of prose

        # Anything whose fraction landed past the last block goes at the end.
        for _, im in pending:
            row += 1
            row += _add_image(ws, row, im, problems, f"page {pno} end")
            row += 1

        row += 1                  # an extra blank row between pages

    _autosize(ws)
    wb.save(path)
    return problems


def verify_xlsx(path: str, source_text: str):
    """Read the written .xlsx back and check it against the PDF's own text.

    Same principle as verify_docx(): "a file was produced" and "the file
    contains the document" are different claims, and only the second is worth
    anything. This reads the SAVED workbook, not the data we meant to write.

    Returns (coverage, missing_words).
    """
    wb = load_workbook(path, read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for value in row:
                if value is not None:
                    parts.append(str(value))
    wb.close()

    written = normalize(" ".join(parts))
    written_squashed = squash(" ".join(parts))

    words = [w for w in normalize(source_text).split() if len(w) > 3]
    if not words:
        return 1.0, []

    missing = sorted({
        w for w in words
        if w not in written and squash(w) not in written_squashed
    })
    coverage = 1 - len(missing) / len(set(words))
    return coverage, missing
