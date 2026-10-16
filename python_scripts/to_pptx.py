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

  A PAGE THAT FITS IS ONE SLIDE; A PAGE THAT DOES NOT FIT CONTINUES. Nothing is squeezed to fit (that crushed text and
  ran it off the slide). The extra slides are titled "<title> (cont.)". A run of text breaks between its lines (one
  over-long paragraph at a sentence end), a table breaks between rows with its header repeated, and a table that fits
  a fresh slide is moved whole rather than split. This replaces the earlier rule "one PDF page is always one slide".

  THE FIRST HEADING ON THE PAGE BECOMES THE SLIDE TITLE. Any other headings
  that follow it become bold sub-headers inside the slide body instead of
  titles of their own -- a page keeps mapping to exactly one slide either way.
"""

import re
from typing import List

from pptx import Presentation
from pptx.enum.text import PP_ALIGN
from pptx.dml.color import RGBColor
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

# A slide title is a short line. The model often returns one coarse "heading" block holding a title AND what
# follows it ("Services Agreement\nSample document...\nACME", or "4. Payment\nThe Buyer shall pay..."), which
# used to become one huge title that ran off the slide (KNOWN_ISSUES #5).
MAX_TITLE_CHARS = 90


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

    _apply_table_look(table, block, width)
    return top_in + height_in + 0.2


_PPT_ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}


def _apply_table_look(table, block, width: int):
    """Copy the source table's look when it was measured (PDF/Word sources): column widths, per-column
    alignment, and the header's fill colour. A table without measurements keeps PowerPoint's default style."""
    widths = getattr(block, "col_widths", None) or []
    if len(widths) == width and sum(widths) > 0:
        total = sum(widths)
        for c, w in enumerate(widths):
            table.columns[c].width = Inches(CONTENT_WIDTH_IN * w / total)
    head_align = getattr(block, "header_align", None) or []
    col_align = getattr(block, "col_align", None) or []
    fill = getattr(block, "header_fill", None)
    for r, row in enumerate(table.rows):
        is_head = block.has_header and r == 0
        aligns = head_align if (is_head and head_align) else col_align
        for c, cell in enumerate(row.cells):
            if c < len(aligns):
                cell.text_frame.paragraphs[0].alignment = _PPT_ALIGN.get(aligns[c], PP_ALIGN.LEFT)
            if is_head and fill:
                cell.fill.solid()
                cell.fill.fore_color.rgb = RGBColor.from_string(fill)
                light = sum(int(fill[i:i + 2], 16) for i in (0, 2, 4)) / 3 > 150
                for run in cell.text_frame.paragraphs[0].runs:
                    run.font.color.rgb = RGBColor(0, 0, 0) if light else RGBColor(255, 255, 255)
                    run.font.bold = True


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


# "1. Parties", "2.1 Scope", "(a) Term", "A. Overview": a short numbered or lettered line that opens a clause.
_CLAUSE_HEAD = re.compile(r"^\s*(\d+(\.\d+)*[.)]?|\([a-z0-9]{1,3}\)|[A-Z][.)])\s+\S")
MAX_CLAUSE_HEAD_CHARS = 40


def _split_headings(blocks):
    """Split a block into a heading and a paragraph when the model joined a heading to the text after it.

    Two cases, both only when the first line is short and the rest is longer:
    - a "heading" block holding a title plus more text ("Services Agreement" / "Sample document...").
    - a "paragraph" block whose first line is a numbered clause heading ("1. Parties" / "This services...").
    A two-line title ("Annual Report" / "2026") stays whole, and so does a list.
    """
    out = []
    for b in blocks:
        if isinstance(b, TableBlock) or b.kind not in ("heading", "paragraph"):
            out.append(b)
            continue
        lines = [ln.strip() for ln in b.text.splitlines() if ln.strip()]
        rest = "\n".join(lines[1:])
        first = lines[0] if lines else ""
        if b.kind == "heading":
            split = len(lines) > 1 and len(first) <= MAX_TITLE_CHARS and len(rest) > len(first)
        else:
            split = (len(lines) > 1 and len(first) <= MAX_CLAUSE_HEAD_CHARS and len(rest) > len(first)
                     and bool(_CLAUSE_HEAD.match(first)) and first[-1] not in ".,;:")
        if split:
            out.append(b.model_copy(update={"kind": "heading", "text": first, "bold": True}))
            out.append(b.model_copy(update={"kind": "paragraph", "text": rest, "bold": False}))
        else:
            out.append(b)
    return out


TABLE_ROW_IN = 0.35          # height of one table row; _add_table() uses the same figure


def _text_height_in(lines: List[dict]) -> float:
    """Estimated height of one text box holding these lines (the same estimate _add_text_block() uses)."""
    wanted = 0.1
    for line in lines:
        font_pt = 18 if line["bold"] else 14
        wanted += _wrapped_line_count(line["text"], font_pt, CONTENT_WIDTH_IN) * (font_pt * LINE_HEIGHT_FACTOR) / 72
    return wanted


def _split_line(line: dict, room_in: float):
    """Split one text line that is taller than room_in into (piece that fits, remainder), at a sentence or word end."""
    font_pt = 18 if line["bold"] else 14
    rows_fit = max(int((room_in - 0.1) / (font_pt * LINE_HEIGHT_FACTOR / 72)), 1)
    per_row = max(int((CONTENT_WIDTH_IN * 72) / (font_pt * CHAR_WIDTH_FACTOR)), 1)
    budget = int(rows_fit * per_row * 0.9)
    text = line["text"]
    if len(text) <= budget:
        return line, None
    cut = max(text.rfind(". ", 0, budget), text.rfind("? ", 0, budget), text.rfind("! ", 0, budget))
    if cut < budget * 0.4:                       # no sentence end in the second half of the budget: cut at a word
        cut = text.rfind(" ", 0, budget)
    if cut <= 0:
        cut = budget
    piece = text[:cut + 1].strip()
    rest = text[cut + 1:].strip()
    return dict(line, text=piece), (dict(line, text=rest) if rest else None)


def build_pptx(pages: List[Page], path: str, image_sets: List[list] = None):
    """Write the pages as slides. Returns a list of problems found.

    A page that fits is ONE slide. A page that does not fit CONTINUES onto further slides titled "<title> (cont.)":
    a run of text breaks between lines (a single over-long paragraph at a sentence end), a table breaks between rows
    with its header row repeated, and a picture that no longer fits moves to the next slide. Nothing is squeezed to
    fit, so no text is crushed or runs off the slide.

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
        blocks = _split_headings(list(page.blocks))

        # The first heading that is a short line becomes the slide title; it is
        # consumed here and never written again as a body line. A "heading"
        # that is really a whole paragraph is never used as a title.
        title_idx = next(
            (i for i, b in enumerate(blocks)
             if not isinstance(b, TableBlock) and b.kind == "heading"
             and len(b.text.strip()) <= MAX_TITLE_CHARS),
            None,
        )
        title_text = blocks.pop(title_idx).text if title_idx is not None else ""

        n = len(blocks)
        pending = sorted(
            ((min(n, round(im["frac_above"] * n)), im) for im in images),
            key=lambda t: t[0],
        )

        state = {"slide": None, "top": 0.0, "start": 0.0, "count": 0}

        def start_slide():
            """A fresh slide for this page (the first one, or a continuation)."""
            slide = prs.slides.add_slide(layout)
            state["count"] += 1
            # The "Title Only" placeholder ships left-aligned in a 9in box, so a short
            # title sits off to the left with dead space beside it. The REAL title
            # placeholder is kept (outline view and screen readers need it) but is
            # stretched across the full slide width and centred.
            ts = slide.shapes.title
            t_top, t_height = ts.top, ts.height          # read BEFORE moving it: setting only left/width zeroes these
            if t_top is not None and t_height is not None:
                top = (t_top + t_height) / 914400 + BODY_GAP_IN
            else:
                top = BODY_TOP_IN
                t_top, t_height = Inches(0.30), Inches(1.25)
            title_text_to_show = (title_text if state["count"] == 1
                                  else f"{title_text} (cont.)".strip())
            ts.left, ts.top = Inches(0.0), t_top
            ts.width, ts.height = Inches(SLIDE_WIDTH_IN), t_height
            tf = ts.text_frame
            tf.word_wrap = True
            tf.text = title_text_to_show
            para = tf.paragraphs[0]
            para.font.size = Pt(28)
            para.font.bold = True
            para.alignment = PP_ALIGN.CENTER
            state.update(slide=slide, top=top, start=top)

        def room():
            return SLIDE_HEIGHT_IN - BOTTOM_MARGIN_IN - state["top"]

        def at_top():
            return state["top"] <= state["start"] + 1e-6

        def full_room():
            return SLIDE_HEIGHT_IN - BOTTOM_MARGIN_IN - state["start"]

        start_slide()
        pending_lines: List[dict] = []

        def flush_text(where: str):
            nonlocal pending_lines
            lines = pending_lines
            pending_lines = []
            while lines:
                fit, used = [], 0
                for line in lines:
                    if _text_height_in(fit + [line]) <= room():
                        fit.append(line)
                    else:
                        break
                rest = lines[len(fit):]
                if not fit:
                    if not at_top():
                        start_slide()
                        continue
                    # one line taller than a whole slide: split it at a sentence end
                    piece, remainder = _split_line(lines[0], room())
                    fit = [piece]
                    rest = ([remainder] if remainder else []) + lines[1:]
                state["top"] = _add_text_block(state["slide"], fit, state["top"], problems, where)
                lines = rest
                if lines:
                    start_slide()

        def emit_table(block: TableBlock, where: str):
            if not block.rows:
                problems.append(f"{where}: table skipped (no rows)")
                return
            head = 1 if block.has_header else 0
            rows = list(block.rows)
            while rows:
                cap = min(room(), MAX_ELEMENT_HEIGHT_IN)
                need = (len(rows) + head) * TABLE_ROW_IN
                if need <= cap:
                    take = len(rows)
                elif not at_top() and need <= min(full_room(), MAX_ELEMENT_HEIGHT_IN):
                    start_slide()                # the whole table fits a fresh slide: move it, do not split it
                    continue
                else:
                    take = int(cap / TABLE_ROW_IN) - head
                    if take < 2 and not at_top():
                        start_slide()
                        continue
                    take = max(take, 1)
                    left = len(rows) - take
                    if 0 < left < 3 and take > 4:
                        take -= 3 - left         # never leave a slide holding one or two orphan rows
                part = block.model_copy(update={"rows": rows[:take], "n_data_rows": len(rows[:take])})
                state["top"] = _add_table(state["slide"], part, state["top"], problems, where)
                rows = rows[take:]
                if rows:
                    start_slide()

        def emit_image(img: dict, where: str):
            height_in = min(img.get("height_in") or 2.0, MAX_ELEMENT_HEIGHT_IN)
            if not at_top() and height_in > room():
                start_slide()
            state["top"] = _add_image(state["slide"], img, state["top"], problems, where)

        for bno, block in enumerate(blocks):
            where = f"page {pno} block[{bno}]"

            while pending and pending[0][0] <= bno:
                flush_text(where)
                emit_image(pending.pop(0)[1], where)

            if isinstance(block, TableBlock):
                flush_text(where)
                emit_table(block, where)
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
            emit_image(im, f"page {pno} end")

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
