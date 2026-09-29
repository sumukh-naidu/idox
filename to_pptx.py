"""
to_pptx.py -- Tier 3 for slide decks: turn extracted Page objects into .pptx.

NO MODEL IS INVOLVED HERE, exactly as in to_docx.py and to_xlsx.py. The model's
job ended when it produced the Page objects; this only decides how they get
written out. That is the whole reason this is a new writer rather than a new
pipeline -- to_xlsx.py already made the case for this, word for word: Page was
defined as a STRUCTURAL schema, never tied to Word, so the same extraction,
the same checks and the same repairs feed all three:

    PDF -> Tier 0 + model -> Page --+--> to_docx.py -> .docx
                            --------+--> to_xlsx.py -> .xlsx
                            --------+--> to_pptx.py -> .pptx

--------------------------------------------------------------------------
WHY A SLIDE IS A HARDER TARGET THAN A DOCUMENT OR A SHEET

A Word document flows; a page that runs long just continues onto the next
page, unremarked. A slide does not flow -- it is a fixed canvas, and content
that overflows it is simply cut off or crushed. Two decisions follow from
that, made deliberately, not as a first draft to improve on later:

  ONE PDF PAGE IS ALWAYS ONE SLIDE. A page's content is never split across
  several slides to make it fit. If a page is dense, the slide is dense --
  visually honest about what was actually on the source page, rather than
  inventing a split point the source never had.

  THE FIRST HEADING ON THE PAGE BECOMES THE SLIDE TITLE. Any other headings
  that follow it become bold sub-headers inside the slide body instead of
  titles of their own -- a page keeps mapping to exactly one slide either way.
"""

from typing import List

from pptx import Presentation
from pptx.util import Emu, Inches, Pt

from blocks import Page, TableBlock, normalize, squash

# 16:9, the modern default -- a blank Presentation() defaults to 4:3 (10x7.5in)
# unless told otherwise.
SLIDE_WIDTH_IN = 13.333
SLIDE_HEIGHT_IN = 7.5

MARGIN_IN = 0.5
CONTENT_WIDTH_IN = SLIDE_WIDTH_IN - 2 * MARGIN_IN

# Fallback only, used if a slide's own title placeholder geometry can't be
# read for some reason. Normally build_pptx() reads the REAL title box's own
# bottom edge per slide instead of trusting a fixed number -- confirmed
# necessary: this guessed value (1.4in) sat 0.15in ABOVE the title
# placeholder's own actual bottom edge (1.55in) on the real default
# template, so the first body text box overlapped the title on every slide.
BODY_TOP_IN = 1.4
BODY_GAP_IN = 0.15

# A picture or table is capped against the slide's own ACTUAL remaining
# height (see _remaining_height()), never a flat number alone -- a fixed cap
# is not enough by itself, confirmed on a real deck: an image positioned
# already 4.1in down a 7.5in slide, capped only to a flat 5in ceiling, still
# ran 1in past the bottom edge, over whatever content should have followed.
MAX_ELEMENT_HEIGHT_IN = 5.0

# Leaves a visible bottom margin so nothing is placed flush against the
# slide's own edge.
BOTTOM_MARGIN_IN = 0.4

# A rough, well-established estimate of how wide one character renders at a
# given font size (in points): about half the point size. There is no way to
# ask python-pptx to actually measure wrapped text -- it does not render --
# so this is what stands in for it when estimating a text box's real height.
CHAR_WIDTH_FACTOR = 0.52
LINE_HEIGHT_FACTOR = 1.25  # line spacing as a multiple of font size


def _remaining_height(top_in: float) -> float:
    """How much vertical room is actually left on the slide from here down."""
    return max(SLIDE_HEIGHT_IN - BOTTOM_MARGIN_IN - top_in, 0.3)


def _wrapped_line_count(text: str, font_pt: float, width_in: float) -> int:
    """Estimate how many visual lines `text` will wrap to at this width.

    The bug this replaces: counting one block as "one line" regardless of its
    real length. Confirmed on a real deck -- a multi-sentence paragraph
    ("Lorem ipsum dolor sit amet, consectetur...") estimated at one line's
    worth of height (~0.3in) actually needed several, so its real text
    overflowed the box and collided with the table placed right below it.
    """
    chars_per_line = max(int((width_in * 72) / (font_pt * CHAR_WIDTH_FACTOR)), 1)
    lines = 0
    for raw_line in text.splitlines() or [""]:
        lines += max(-(-len(raw_line) // chars_per_line), 1)  # ceil division
    return lines


def _title_only_layout(prs: Presentation):
    """The built-in 'Title Only' layout -- a title placeholder, nothing else.

    Deliberately not 'Title and Content': that layout's own content
    placeholder expects either text OR a table OR a picture, never a mix, and
    a real page routinely has prose AND a table AND an image on it. Every
    other shape on the slide is added manually instead, positioned by this
    module, so mixed content never has to fight a placeholder for space.
    """
    for layout in prs.slide_layouts:
        if layout.name == "Title Only":
            return layout
    return prs.slide_layouts[5]  # index 5 is "Title Only" on the default template


def _add_text_block(slide, lines: List[dict], top_in: float,
                    problems: List[str], where: str) -> float:
    """Add one textbox holding a run of consecutive text blocks.

    lines is [{"text", "bold", "bullet"}, ...]. Returns the new top_in for
    whatever comes next -- estimated from actual wrapped line count (see
    _wrapped_line_count()), since python-pptx cannot report back how tall a
    text box actually rendered, and a block-count estimate under-sizes any
    real paragraph longer than one short line.
    """
    wanted_in = 0.1  # top/bottom internal padding
    for line in lines:
        font_pt = 18 if line["bold"] else 14
        n_wrapped = _wrapped_line_count(line["text"], font_pt, CONTENT_WIDTH_IN)
        wanted_in += n_wrapped * (font_pt * LINE_HEIGHT_FACTOR) / 72

    height_in = min(wanted_in, _remaining_height(top_in))
    if height_in < wanted_in * 0.6:
        problems.append(
            f"{where}: slide is overcrowded -- text compressed to "
            f"{height_in:.1f}in (wanted {wanted_in:.1f}in); may run past its "
            f"own box"
        )
    box = slide.shapes.add_textbox(
        Inches(MARGIN_IN), Inches(top_in), Inches(CONTENT_WIDTH_IN), Inches(height_in)
    )
    tf = box.text_frame
    tf.word_wrap = True

    for i, line in enumerate(lines):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.text = line["text"]
        para.font.size = Pt(14 if not line["bold"] else 18)
        para.font.bold = line["bold"]
        if line["bullet"]:
            para.level = 1

    return top_in + height_in + 0.15


def _add_table(slide, block: TableBlock, top_in: float,
               problems: List[str], where: str) -> float:
    """Add one TableBlock as a real PowerPoint table. Returns the new top_in."""
    if not block.rows:
        problems.append(f"{where}: table skipped (no rows)")
        return top_in

    width = len(block.header) if block.has_header else len(block.rows[0])
    if width == 0:
        problems.append(f"{where}: table skipped (no columns)")
        return top_in

    n_rows = len(block.rows) + (1 if block.has_header else 0)
    row_height_in = 0.35
    wanted_in = n_rows * row_height_in
    height_in = min(wanted_in, MAX_ELEMENT_HEIGHT_IN, _remaining_height(top_in))
    if height_in < wanted_in * 0.6:
        problems.append(
            f"{where}: slide is overcrowded -- table compressed to "
            f"{height_in:.1f}in (wanted {wanted_in:.1f}in); rows may be hard "
            f"to read"
        )

    graphic_frame = slide.shapes.add_table(
        n_rows, width,
        Inches(MARGIN_IN), Inches(top_in), Inches(CONTENT_WIDTH_IN), Inches(height_in),
    )
    table = graphic_frame.table

    r0 = 0
    if block.has_header:
        for c, label in enumerate(block.header[:width]):
            table.cell(0, c).text = label
        r0 = 1

    for r, row in enumerate(block.rows):
        if len(row) != width:
            problems.append(
                f"{where} row {r}: had {len(row)} cells, grid is {width} "
                f"-- {'padded' if len(row) < width else 'truncated'}"
            )
        cells = (list(row) + [""] * width)[:width]
        for c, value in enumerate(cells):
            table.cell(r + r0, c).text = value

    for para in [c.text_frame.paragraphs[0] for row in table.rows for c in row.cells]:
        para.font.size = Pt(11)

    return top_in + height_in + 0.2


def _add_image(slide, img: dict, top_in: float, problems: List[str], where: str) -> float:
    """Add one extracted picture at its displayed size, capped to fit.

    Capped against BOTH a flat ceiling AND however much room is actually left
    on the slide from top_in down -- confirmed necessary on a real deck: an
    image placed partway down a slide, capped only against the flat ceiling,
    ran a full inch past the slide's own bottom edge, over whatever content
    should have followed it.
    """
    width_in = min(img.get("width_in") or 2.0, CONTENT_WIDTH_IN)
    height_in = img.get("height_in") or 2.0
    cap_in = min(MAX_ELEMENT_HEIGHT_IN, _remaining_height(top_in))
    if height_in > cap_in:
        scale = cap_in / height_in
        width_in, height_in = width_in * scale, cap_in
        if scale < 0.5:
            problems.append(
                f"{where}: slide is overcrowded -- image shrunk to "
                f"{height_in:.1f}in tall to fit remaining space"
            )

    try:
        slide.shapes.add_picture(
            img["path"], Inches(MARGIN_IN), Inches(top_in),
            width=Inches(width_in), height=Inches(height_in),
        )
    except Exception as exc:
        problems.append(f"{where}: could not insert {img['path']}: {exc}")
        return top_in

    return top_in + height_in + 0.2


def build_pptx(pages: List[Page], path: str, image_sets: List[list] = None):
    """Write one slide per page. Returns a list of problems found.

    image_sets, when given, is one list of extracted images per page, each
    carrying the fraction of that page's text sitting above it -- the same
    structure build_docx()/build_xlsx() consume, placing a picture in reading
    order among the blocks with no coordinates of its own.
    """
    problems: List[str] = []
    prs = Presentation()
    prs.slide_width = Inches(SLIDE_WIDTH_IN)
    prs.slide_height = Inches(SLIDE_HEIGHT_IN)
    layout = _title_only_layout(prs)

    for pno, page in enumerate(pages, start=1):
        images = (image_sets[pno - 1] if image_sets
                  and pno - 1 < len(image_sets) else [])

        slide = prs.slides.add_slide(layout)
        blocks = list(page.blocks)

        # The first heading becomes the slide title; it is consumed here and
        # never written again as a body line.
        title_idx = next(
            (i for i, b in enumerate(blocks)
             if not isinstance(b, TableBlock) and b.kind == "heading"),
            None,
        )
        if title_idx is not None:
            slide.shapes.title.text = blocks.pop(title_idx).text
        else:
            slide.shapes.title.text = ""

        n = len(blocks)
        pending = sorted(
            ((min(n, round(im["frac_above"] * n)), im) for im in images),
            key=lambda t: t[0],
        )

        # The real title box's own bottom edge, not a guessed constant --
        # confirmed necessary: a fixed 1.4in guess sat above the actual
        # title placeholder's bottom edge (1.55in) on the real template,
        # overlapping the first body text box on every slide.
        title_shape = slide.shapes.title
        if title_shape.top is not None and title_shape.height is not None:
            top_in = (title_shape.top + title_shape.height) / 914400 + BODY_GAP_IN
        else:
            top_in = BODY_TOP_IN
        pending_lines: List[dict] = []

        def flush_text(where: str):
            nonlocal top_in, pending_lines
            if pending_lines:
                top_in = _add_text_block(slide, pending_lines, top_in, problems, where)
                pending_lines = []

        for bno, block in enumerate(blocks):
            where = f"page {pno} block[{bno}]"

            while pending and pending[0][0] <= bno:
                flush_text(where)
                top_in = _add_image(slide, pending.pop(0)[1], top_in, problems, where)

            if isinstance(block, TableBlock):
                flush_text(where)
                top_in = _add_table(slide, block, top_in, problems, where)
                continue

            text = block.text.strip()
            if not text:
                problems.append(f"{where}: empty {block.kind}, skipped")
                continue

            pending_lines.append({
                "text": text,
                "bold": block.kind == "heading",
                "bullet": block.kind in ("paragraph", "list", "caption"),
            })

        flush_text(f"page {pno} end")
        for _, im in pending:
            top_in = _add_image(slide, im, top_in, problems, f"page {pno} end")

    prs.save(path)
    return problems


def verify_pptx(path: str, source_text: str):
    """Read the written .pptx back and check it against the PDF's own text.

    Same principle as verify_docx()/verify_xlsx(): "a file was produced" and
    "the file contains the document" are different claims, and only the
    second is worth anything. Reads the SAVED deck, not the data meant to
    be written.

    Returns (coverage, missing_words).
    """
    prs = Presentation(path)
    parts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text_frame.text)
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        parts.append(cell.text)

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
