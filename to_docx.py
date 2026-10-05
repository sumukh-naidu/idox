"""
to_docx.py -- Tier 3: turn extracted Page objects into a real .docx file.

NO MODEL IS INVOLVED HERE, and that is deliberate.

A .docx is not a text file. It is a zip archive of XML parts held together by
relationship ids, content-type declarations and style references that must all
resolve. Asking any language model -- small or large -- to emit that directly
is a known way to produce a file that looks plausible in a text dump and then
refuses to open. python-docx builds the real thing from a data structure, so
the output is openable by construction.

So the division of labour is:

    the model   decides WHAT is on the page   (blocks, tables, text)
    this file   decides HOW it is written out (Word styles, real table grids)

The model's job ends at the Page object. Nothing here asks it anything.

--------------------------------------------------------------------------
WHAT THIS HAS TO DEFEND AGAINST

The extraction is not guaranteed clean, and Word is less forgiving than a
terminal print. Specifically:

  - rows that disagree with the header width. Observed repeatedly: a table
    claiming 6 columns with a 7-cell row. python-docx would raise IndexError,
    so rows are padded or truncated to the grid and the damage is reported
    rather than crashing the run.

  - spanning divider rows (text in cell 1, the rest empty). These are merged
    back into a single full-width cell, which is what they were in the PDF.

  - empty headers, tables with no rows, blocks with no text. All skipped
    rather than producing an empty husk of a table.

verify_docx() then reads the finished file back and compares it against the
PDF's own text, because "it wrote a file" and "the file contains the document"
are different claims.
"""

import hashlib
from collections import Counter
from typing import List

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches, Pt, RGBColor
from docx.table import Table
from docx.text.paragraph import Paragraph

from pdf_look import StyledTable, StyledText

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def apply_layout(doc, layout: dict):
    """Match the Word page to the source PDF's geometry.

    Without this, a 2-page PDF becomes a 4-page Word document -- not because
    anything was added, but because Word's defaults are roomier than the
    source at every level. Measured against one real report:

        text column   PDF 6.56 in   Word 6.00 in   (1.25 in default margins)
        body font     PDF 10 pt     Word 11 pt
        line spacing  PDF 1.0       Word 1.08
        space after   PDF ~0        Word 8 pt on EVERY paragraph

    Each is small; compounded over a page of text they add 40-50% of vertical
    space. Matching them does not guarantee identical pagination -- reflowed
    text never breaks in exactly the same places -- but it removes the
    systematic inflation, which is what turns 2 pages into 4.
    """
    section = doc.sections[0]
    section.page_width = Pt(layout["width_pt"])
    section.page_height = Pt(layout["height_pt"])
    section.left_margin = Pt(layout["left_pt"])
    section.right_margin = Pt(layout["right_pt"])
    section.top_margin = Pt(layout["top_pt"])
    section.bottom_margin = Pt(layout["bottom_pt"])

    normal = doc.styles["Normal"]
    normal.font.size = Pt(layout["body_pt"])
    if layout.get("body_font"):
        normal.font.name = layout["body_font"]
    pf = normal.paragraph_format
    pf.line_spacing = 1.0
    pf.space_after = Pt(2)
    pf.space_before = Pt(0)

    # Word's heading styles carry generous space_before, which on a document
    # with several consecutive headings pushes content onto another page.
    for name in ("Heading 1", "Heading 2", "Heading 3", "Title"):
        try:
            hpf = doc.styles[name].paragraph_format
        except KeyError:
            continue
        hpf.space_before = Pt(6)
        hpf.space_after = Pt(2)
        hpf.line_spacing = 1.0

from blocks import Page, TableBlock, normalize, squash


def _neutral_headings(doc):
    """Word's heading styles are blue, in a theme font. Headings copied from a PDF carry
    their own font, size and colour on every run, so the styles are made plain: the
    heading stays a real heading (navigation pane, outline) without Word's look."""
    for name in ("Heading 1", "Heading 2", "Heading 3", "Title"):
        try:
            style = doc.styles[name]
        except KeyError:
            continue
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.font.bold = False
        rpr = style.element.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is not None:
            for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
                fonts.attrib.pop(qn(attr), None)


def _style_run(run, spec: dict):
    run.font.name = spec["font"]
    run.font.size = Pt(spec["size"])
    run.bold = spec["bold"]
    run.italic = spec["italic"]
    run.font.color.rgb = RGBColor.from_string(spec["color"])


def _add_styled_text(doc, b: StyledText, cur):
    """One paragraph copied from the PDF. `cur` is where the previous element ended, in
    PDF points; the gap to where this one starts becomes its space before, so the
    vertical rhythm of the page is kept and any overshoot is absorbed by the next gap."""
    para = doc.add_heading("", level=b.level or 2) if b.kind == "heading" else doc.add_paragraph()
    for r in b.runs:
        _style_run(para.add_run(r["text"]), r)
    para.alignment = ALIGNMENTS.get(b.align, WD_ALIGN_PARAGRAPH.LEFT)
    pf = para.paragraph_format
    pf.space_after = Pt(0)
    pf.left_indent = Pt(round(b.indent, 1)) if b.indent else None
    if b.exact:
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        pf.line_spacing = Pt(round(b.line_h, 1))
    gap = max(0.0, b.top - cur) if cur is not None else 0.0
    pf.space_before = Pt(round(gap, 1))
    start = (cur + gap) if cur is not None else b.top
    return para, start + b.n_lines * b.line_h


def _style_table(table, block: StyledTable):
    """Column widths, row heights, font and size from the PDF's own table."""
    table.autofit = False
    if block.col_widths:
        for i, w in enumerate(block.col_widths):
            table.columns[i].width = Pt(w)
            for cell in table.columns[i].cells:
                cell.width = Pt(w)
    median = sorted(block.row_heights)[len(block.row_heights) // 2] if block.row_heights else None
    for i, row in enumerate(table.rows):
        h = block.row_heights[i] if len(block.row_heights) == len(table.rows) else median
        if h:
            row.height = Pt(round(h, 1))
            row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
        for cell in row.cells:
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for para in cell.paragraphs:
                para.paragraph_format.space_before = Pt(0)
                para.paragraph_format.space_after = Pt(0)
                for run in para.runs:
                    if block.font:
                        run.font.name = block.font
                    if block.size_pt:
                        run.font.size = Pt(block.size_pt)

# Which Word style each block kind becomes. Keeping this as a table rather
# than a chain of ifs makes it obvious what is supported and easy to change.
HEADING_LEVELS = {"heading": 1}


def _add_table(doc, block: TableBlock, problems: List[str], where: str):
    """Write one TableBlock as a real Word table."""
    if not block.rows:
        problems.append(f"{where}: table skipped (no rows)")
        return None

    # A headerless table -- a plain grid of numbers, say -- takes its width
    # from the data instead, and gets no bold first row.
    width = len(block.header) if block.has_header else len(block.rows[0])
    if width == 0:
        problems.append(f"{where}: table skipped (no columns)")
        return None

    if block.has_header:
        table = doc.add_table(rows=1, cols=width)
        table.style = "Table Grid"
        for i, label in enumerate(block.header[:width]):
            cell = table.rows[0].cells[i]
            cell.text = label
            for para in cell.paragraphs:
                for run in para.runs:
                    run.bold = True
    else:
        table = doc.add_table(rows=0, cols=width)
        table.style = "Table Grid"

    for r, row in enumerate(block.rows):
        # Force the row onto the grid. A ragged row is a real extraction fault,
        # but crashing the conversion helps nobody -- record it and continue.
        if len(row) != width:
            problems.append(
                f"{where} row {r}: had {len(row)} cells, grid is {width} "
                f"-- {'padded' if len(row) < width else 'truncated'}"
            )
        cells = (list(row) + [""] * width)[:width]

        out = table.add_row().cells
        for i, value in enumerate(cells):
            out[i].text = value

        # A row with text only in the first cell is a spanning divider in the
        # source. Merge it back so the Word table looks like the PDF did.
        if cells[0] and not any(c.strip() for c in cells[1:]) and width > 1:
            merged = out[0].merge(out[width - 1])
            merged.text = cells[0]
            for para in merged.paragraphs:
                for run in para.runs:
                    run.bold = True
    return table


ALIGNMENTS = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
}


def _unescape(text: str) -> str:
    r"""Turn literal backslash-n into a real line break.

    The model sometimes emits the two characters \ and n instead of a newline,
    so a five-line address arrived as one run-on line reading
    "Dr. P.N. Cundall,\nMining Surveys Ltd.,\nHolroyd Road,...". The schema now
    asks for real breaks, but a prose instruction is advisory -- this repairs
    whatever still slips through rather than trusting it not to.
    """
    return text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")


def _write_lines(para, text, italic=False, size=None):
    """Write text into a paragraph, keeping its line breaks as line breaks.

    Word treats a newline inside a run as nothing at all, so the lines of an
    address would collapse into one. Each line becomes its own run with an
    explicit break between them, which keeps them visually separate while
    staying a single paragraph -- which is what they are on the page.
    """
    lines = _unescape(text).split("\n")
    for i, line in enumerate(lines):
        if i:
            para.add_run().add_break()
        run = para.add_run(line)
        if italic:
            run.italic = True
        if size:
            run.font.size = size
    return para


def _size_scale(pages):
    """Work out what each size label should mean for THIS document.

    The model labels blocks large/normal/small, but its anchor drifts: on a
    scanned letter it marked every block except the letterhead as 'small', so
    the entire body rendered at 8.5pt.

    Prose in the field description did not fix that, so it is settled by
    measurement instead: whichever label most blocks carry IS the body size,
    by definition. That is the same rule _body_size() uses on the digital path,
    where the dominant font size on the page defines the body. The other labels
    scale relative to whatever that turns out to be.
    """
    base = {"small": 0.8, "normal": 1.0, "large": 1.6}

    # Weighted by CHARACTERS, not by block count. The body of a document is
    # whatever carries the most text, which is the same rule the digital path
    # uses to find the body font size. Counting blocks instead let a page with
    # one long paragraph and three short headings decide that headings were
    # the body -- on one scan that made 'large' the anchor and collapsed the
    # actual body text to 0.5x, rendering it at 5.5pt.
    counts = Counter()
    for page in pages:
        for b in page.blocks:
            if isinstance(b, TableBlock):
                continue
            counts[getattr(b, "size", "normal")] += len(b.text)

    if not counts:
        return base

    anchor = base[counts.most_common(1)[0][0]]
    return {k: v / anchor for k, v in base.items()}


def _apply_look(para, block, layout, is_heading=False, scale=None):
    """Apply the block's observed appearance: alignment, size, weight.

    These come from the model looking at the page, because on a scan there is
    no other source -- no text layer means no font sizes and no coordinates.
    Sizes are relative to the document's body size so the result scales with
    whatever the source used.
    """
    para.alignment = ALIGNMENTS.get(getattr(block, "align", "left"),
                                    WD_ALIGN_PARAGRAPH.LEFT)

    body = (layout or {}).get("body_pt", 11)
    scale = scale or {"large": 1.6, "normal": 1.0, "small": 0.8}
    factor = scale.get(getattr(block, "size", "normal"), 1.0)

    # A floor and ceiling, because the labels are the model's judgement and it
    # can be badly skewed. Whatever it reports, body text must stay readable
    # and a heading must not become a billboard: one scan produced 5.5pt
    # paragraphs before this existed. 8pt is small print; nothing legitimate
    # in a document needs to be smaller.
    point = max(8.0, min(body * factor, body * 2.2))

    for run in para.runs:
        if getattr(block, "bold", False):
            run.bold = True
        # A heading style already carries its own size; only override when the
        # model saw something other than ordinary body text.
        if factor != 1.0 or not is_heading:
            if run.font.size is None or factor != 1.0:
                run.font.size = Pt(round(point, 1))


def _anchor_image(para, im, layout, problems, where):
    """Put a picture in `para` as a floating one, at its exact page position, with no
    room reserved for it in the text flow. For a logo beside a title: it sits where the
    PDF has it and the title keeps its own place instead of being pushed below it."""
    max_in = (layout["width_pt"] - layout["left_pt"] - layout["right_pt"]) / 72.0
    width = min(im["width_in"], max_in)
    try:
        run = para.add_run()
        run.add_picture(im["path"], width=Inches(width))
    except Exception as exc:
        problems.append(f"{where}: could not insert {im['path']}: {exc}")
        return
    inline = run._r.xpath(".//wp:inline")[0]
    emu = 12700
    anchor = parse_xml(
        f'<wp:anchor {nsdecls("wp", "a")} distT="0" distB="0" distL="0" distR="0" simplePos="0" '
        f'relativeHeight="251658240" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
        f'<wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="page"><wp:posOffset>{int(im["x"][0] * emu)}</wp:posOffset></wp:positionH>'
        f'<wp:positionV relativeFrom="page"><wp:posOffset>{int(im["bbox"][0] * emu)}</wp:posOffset></wp:positionV>'
        f'<wp:extent cx="{inline.extent.cx}" cy="{inline.extent.cy}"/>'
        f'<wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>'
        f'<wp:docPr id="{inline.docPr.id}" name="{inline.docPr.name}"/><wp:cNvGraphicFramePr/></wp:anchor>')
    anchor.append(inline.graphic)
    inline.getparent().replace(inline, anchor)


def _add_images(doc, images, layout, problems, where, cur=None, page_break=False):
    """Insert extracted pictures at their displayed size.

    Width is taken from how big the image was ON THE PAGE, not its pixel
    dimensions, so a 1352x969 logo shown at 6.13in comes out 6.13in rather
    than 18in. It is then clamped to the text column, because a picture wider
    than the margins silently overflows the page in Word.

    When the page's text was copied from the PDF (`cur` is set), a picture also
    keeps its side of the page (left, centre or right) and its vertical position.
    Returns (cur, page_break): the new cursor, and whether a pending page break
    is still waiting for a paragraph to carry it.
    """
    max_in = 6.5
    if layout:
        max_in = (layout["width_pt"] - layout["left_pt"]
                  - layout["right_pt"]) / 72.0

    for im in images:
        width = min(im["width_in"], max_in)
        try:
            doc.add_picture(im["path"], width=Inches(width))
        except Exception as exc:
            problems.append(f"{where}: could not insert {im['path']}: {exc}")
            continue
        para = doc.paragraphs[-1]
        if page_break:
            para.paragraph_format.page_break_before = True
            page_break = False
        if cur is not None and layout and "x" in im:
            pf = para.paragraph_format
            left = layout["left_pt"]
            right = layout["width_pt"] - layout["right_pt"]
            x0, x1 = im["x"]
            if right - x1 < 12:
                para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            elif abs((x0 + x1) / 2 - layout["width_pt"] / 2) < 24 and x0 - left > 24:
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            elif x0 - left > 4:
                pf.left_indent = Pt(round(x0 - left, 1))
            top = im["bbox"][0]
            gap = max(0.0, top - cur)
            pf.space_before = Pt(round(gap, 1))
            pf.space_after = Pt(0)
            cur = cur + gap + im["height_in"] * 72.0 * (width / im["width_in"])
    return cur, page_break


def build_docx(pages: List[Page], path: str, title: str = None,
               layout: dict = None, image_sets: List[list] = None):
    """Write extracted pages to a .docx. Returns a list of problems found.

    pages is one Page per PDF page, in order. Each gets a page break after it,
    so the Word document keeps the source's pagination.

    layout, when given, matches the Word page geometry and text size to the
    source PDF -- see apply_layout(). Without it Word's roomier defaults
    inflate the document onto extra pages.

    image_sets, when given, is one list of extracted images per page, each
    carrying the fraction of that page's text sitting above it. The model's
    blocks have no coordinates, so that fraction is what places a picture in
    reading order: "65% of the text is above this logo" becomes "insert it
    after 65% of the blocks". A page with no images contributes an empty list
    and nothing about its output changes.
    """
    problems: List[str] = []
    doc = Document()

    # What "normal" means is decided by the document, not by the label.
    scale = _size_scale(pages)

    if layout:
        apply_layout(doc, layout)
    if layout and any(isinstance(b, (StyledText, StyledTable)) for pg in pages for b in pg.blocks):
        _neutral_headings(doc)

    # The filename heading is skipped when matching the source layout: the PDF
    # has no such line, so adding one guarantees the first page differs.
    if title and not layout:
        doc.add_heading(title, level=0)

    break_before = False
    for pno, page in enumerate(pages, start=1):
        images = (image_sets[pno - 1] if image_sets
                  and pno - 1 < len(image_sets) else [])

        # Turn each image's "fraction of text above me" into a block index to
        # sit after. Sorted so several images on one page stay in page order,
        # and popped from the front as the blocks are written out.
        n = len(page.blocks)
        # key=... is load-bearing, not style: with an empty page (n == 0 --
        # exactly the separate scan document under --scan-mode both) every
        # image's position collapses to 0, so every tuple ties on its first
        # element. Without a key, Python falls back to comparing the second
        # element (the image dict) to break the tie, and dicts have no
        # ordering -- a real crash, reproduced with 2 images on one blank
        # page. The key restricts comparison to the position only; Python's
        # sort is stable, so tied images simply keep their original order.
        # On a page whose text was copied from the PDF every block knows where it sat,
        # so a picture goes before the first block that starts below its top, and the
        # page is laid out against a vertical cursor (PDF points) to keep its spacing.
        # Mixed pages, and pages whose blocks are not in top-to-bottom order (columns),
        # use the fraction heuristic as before.
        page_styled = bool(layout) and any(isinstance(b, (StyledText, StyledTable)) for b in page.blocks)
        tops = [b.y0 for b in page.blocks if isinstance(b, (StyledText, StyledTable))]
        by_position = (page_styled and all(isinstance(b, (StyledText, StyledTable)) for b in page.blocks)
                       and all(y2 >= y1 - 2 for y1, y2 in zip(tops, tops[1:])))
        cur = layout["top_pt"] if page_styled else None

        def slot(im):
            if by_position:
                return sum(1 for y in tops if y < im["bbox"][0] - 1)
            return min(n, round(im["frac_above"] * n))

        pending = sorted(((slot(im), im) for im in images), key=lambda t: t[0])

        # A table is a different kind of element than a paragraph in Word's
        # own format, and does not inherit the Normal style's space_after the
        # way one paragraph inherits it from the paragraph before it. Left
        # alone, a table sits flush against the text on both sides -- true on
        # every page, not something specific to any one document. Tracking
        # the most recently written paragraph, and whether a table was just
        # written, is what lets the gap be added explicitly on both sides.
        last_para = None
        after_table = False

        for bno, block in enumerate(page.blocks):
            where = f"page {pno} block[{bno}]"

            floats = []
            while pending and pending[0][0] <= bno:
                im = pending.pop(0)[1]
                # A picture the next paragraph starts beside (it overlaps vertically and is
                # narrower than the column) floats; any other keeps its own line, inline.
                beside = (by_position and isinstance(block, StyledText) and block.kind != "list"
                          and "x" in im and block.top < im["bbox"][1]
                          and im["width_in"] * 72.0 < 0.6 * (layout["width_pt"] - layout["left_pt"] - layout["right_pt"]))
                if beside:
                    floats.append(im)
                    continue
                cur, break_before = _add_images(doc, [im], layout, problems, where, cur, break_before)
                last_para = None

            if isinstance(block, StyledTable):
                if last_para is not None and cur is not None:
                    last_para.paragraph_format.space_after = Pt(round(max(0.0, block.y0 - cur), 1))
                if break_before:
                    doc.add_page_break()
                    break_before = False
                table = _add_table(doc, block, problems, where)
                if table is not None:
                    _style_table(table, block)
                cur = (max(cur, block.y0) if cur is not None else block.y0) + (block.y1 - block.y0)
                last_para, after_table = None, False
                continue

            if isinstance(block, StyledText) and block.kind != "list" and block.text.strip():
                para, cur = _add_styled_text(doc, block, cur)
                for im in floats:
                    _anchor_image(para, im, layout, problems, where)
                if break_before:
                    para.paragraph_format.page_break_before = True
                    break_before = False
                last_para, after_table = para, False
                continue

            if page_styled:
                cur = None                      # an unstyled block: the position is unknown from here

            if isinstance(block, TableBlock):
                if last_para is not None:
                    last_para.paragraph_format.space_after = Pt(6)
                if break_before:
                    doc.add_page_break()
                    break_before = False
                _add_table(doc, block, problems, where)
                after_table = True
                continue

            text = block.text.strip()
            if not text:
                problems.append(f"{where}: empty {block.kind}, skipped")
                continue

            if block.kind in HEADING_LEVELS:
                para = doc.add_heading("", level=HEADING_LEVELS[block.kind])
                _write_lines(para, text)
                _apply_look(para, block, layout, is_heading=True, scale=scale)

            elif block.kind == "list":
                # The model returns a list as one text block; split it back
                # into bullets on line breaks and common bullet characters.
                items = [
                    line.strip(" •◦▪-–\t")
                    for line in _unescape(text).splitlines()
                    if line.strip(" •◦▪-–\t")
                ]
                para = None
                for item in items or [text]:
                    para = doc.add_paragraph(item, style="List Bullet")

            elif block.kind == "caption":
                para = doc.add_paragraph()
                _write_lines(para, text, italic=True, size=Pt(9))
                _apply_look(para, block, layout, scale=scale)

            else:  # paragraph, image, anything else
                para = doc.add_paragraph()
                _write_lines(para, text)
                _apply_look(para, block, layout, scale=scale)

            if after_table and para is not None:
                para.paragraph_format.space_before = Pt(6)
            after_table = False
            if para is not None:
                last_para = para
                if break_before:
                    para.paragraph_format.page_break_before = True
                    break_before = False

        # Anything whose fraction landed past the last block goes at the end.
        if pending:
            cur, break_before = _add_images(doc, [im for _, im in pending], layout, problems,
                                            f"page {pno} end", cur, break_before)

        if break_before:                        # this page wrote nothing to carry the break
            doc.add_page_break()
            break_before = False
        if pno < len(pages):
            # A paragraph that is only a page break sits at the foot of the page and can
            # spill onto a new one when the page is full. Where the next page was copied
            # from the PDF, its first paragraph carries the break instead.
            nxt_styled = bool(layout) and any(isinstance(b, (StyledText, StyledTable)) for b in pages[pno].blocks)
            if nxt_styled:
                break_before = True
            else:
                doc.add_page_break()

    doc.save(path)
    return problems


def verify_images(path: str, image_sets):
    """Check 5: did the pictures actually survive into the Word file?

    Nothing checked this before. An image could fail to insert, insert twice,
    or land in the wrong place, and every other check would still pass --
    checks 1-4 are all about text, and verify_docx() counts words.

    Three things, all read back from the SAVED file rather than from what we
    meant to write:

      COUNT     one inline shape per extracted image. Catches a silent
                insertion failure or a duplicate.
      BYTES     sha1 of the image stored in the .docx against the sha1 of the
                bytes taken out of the PDF. Proves it is the same picture,
                not a re-encoded or mismatched one.
      POSITION  the coordinate check. The PDF says what text sits immediately
                above and below the image; this confirms the same text sits
                either side of it in the document. frac_above is a heuristic,
                and until now its result was never verified -- "roughly the
                right place" becomes pass or fail.

    Returns a list of problems; empty means all three passed.
    """
    expected = [im for page in (image_sets or []) for im in page]
    doc = Document(path)
    # Pictures can be inline or floating (anchored beside a title), so count the drawings.
    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    shapes = list(doc.element.body.iter(f"{W}drawing"))

    problems = []

    if len(shapes) != len(expected):
        problems.append(
            f"COUNT: {len(expected)} image(s) extracted from the PDF but "
            f"{len(shapes)} in the .docx"
        )

    # --- bytes -----------------------------------------------------------
    for i, (shape, im) in enumerate(zip(shapes, expected)):
        try:
            rid = next(shape.iter(f"{A}blip")).get(f"{R}embed")
            blob = doc.part.related_parts[rid].blob
        except Exception as exc:
            problems.append(f"BYTES: image {i} unreadable in the .docx: {exc}")
            continue
        got = hashlib.sha1(blob).hexdigest()
        if got != im.get("sha1"):
            problems.append(
                f"BYTES: image {i} differs -- PDF sha1 {im.get('sha1', '?')[:12]}, "
                f".docx sha1 {got[:12]}"
            )

    # --- position --------------------------------------------------------
    # Walk the body in order, recording text and where the pictures fall.
    sequence = []
    for child in doc.element.body.iterchildren():
        if child.tag == f"{W}p":
            para = Paragraph(child, doc)
            if para._p.findall(f".//{W}drawing"):
                sequence.append(("image", None))
            if para.text.strip():                 # a floating picture shares its paragraph with text
                sequence.append(("text", normalize(para.text)))
        elif child.tag == f"{W}tbl":
            table = Table(child, doc)
            cells = " ".join(c.text for r in table.rows for c in r.cells)
            sequence.append(("text", normalize(cells)))

    positions = [i for i, (kind, _) in enumerate(sequence) if kind == "image"]

    for i, (pos, im) in enumerate(zip(positions, expected)):
        want_before = im.get("text_before")
        want_after = im.get("text_after")

        # Text level with a picture (a logo beside a title) has no clear before/after in
        # the PDF, so the expected neighbour may be a few paragraphs away.
        if want_before:
            texts = [t for k, t in reversed(sequence[:pos]) if k == "text"]
            prev = texts[0] if texts else ""
            if not any(normalize(want_before)[:40] in t for t in texts[:3]):
                problems.append(
                    f"POSITION: image {i} should follow "
                    f"{want_before[:40]!r} but follows {prev[:40]!r}"
                )
        if want_after:
            texts = [t for k, t in sequence[pos + 1:] if k == "text"]
            nxt = texts[0] if texts else ""
            if not any(normalize(want_after)[:40] in t for t in texts[:3]):
                problems.append(
                    f"POSITION: image {i} should precede "
                    f"{want_after[:40]!r} but precedes {nxt[:40]!r}"
                )

    return problems


def verify_docx(path: str, source_text: str):
    """Read the written .docx back and check it against the PDF's own text.

    "A file was produced" and "the file contains the document" are different
    claims. This checks the second one, by reading every paragraph and every
    table cell out of the saved file and measuring how much of the PDF's text
    is accounted for.

    Returns (coverage, missing_words). Coverage is the fraction of the PDF's
    words (longer than 3 characters) that appear somewhere in the document.
    """
    doc = Document(path)

    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    written = normalize(" ".join(parts))
    written_squashed = squash(" ".join(parts))

    words = [w for w in normalize(source_text).split() if len(w) > 3]
    if not words:
        return 1.0, []

    # squash() catches words that differ only in spacing, such as
    # {"model","method"} against {"model", "method"}.
    missing = sorted({
        w for w in words
        if w not in written and squash(w) not in written_squashed
    })
    coverage = 1 - len(missing) / len(set(words))
    return coverage, missing
