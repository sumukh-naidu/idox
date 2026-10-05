"""
pdf_look.py -- on a DIGITAL page, copy each block's appearance from the PDF itself.

The model reads the page and decides WHAT it says and how it is structured
(headings, paragraphs, tables, reading order). Its blocks are often coarser than the
page: the 35B model returned "Services Agreement / Sample document... / ACME" -- a
title, its subtitle and the lettering of the logo -- as ONE heading, and each clause
heading together with its paragraph as ONE paragraph. measure_block_look() styles a
whole block from its first line, and for a merged block that line often cannot even
be located, so on one agreement most blocks came out as plain default text: blue Word
headings, clause titles not bold, everything packed at the top of the page.

None of that needs guessing on a digital page. Every line in the text layer records
its font, size, weight, colour and position. So for each text block the model
returned, this finds the PDF lines it covers and rebuilds it as one paragraph per
visual paragraph:

  - split where the PDF's style changes (a bold 12pt heading line, then 10.5pt body)
    or where there is a paragraph gap
  - text taken from those lines: wrapped lines joined, short lines (addresses,
    signature blocks) kept as line breaks, hyphenated words rejoined
  - every span keeps its own font, size, bold, italic and colour
  - alignment, indent, vertical position and line spacing from coordinates
  - lettering the model read out of a PICTURE (a logo's text) has no line in the text
    layer; it is dropped when the page has images -- the same rule
    ungrounded_text_blocks() already applies to whole blocks

Tables keep the model's cells; column widths, row heights, font and size come from the
PDF's own table (PyMuPDF find_tables).

The model's blocks still decide ORDER and STRUCTURE; only appearance comes from here.
A block whose lines cannot be found is kept untouched. Scanned pages never come here:
no text layer, nothing to measure. Works on copies; the model's pages are not changed.
"""

import re
import statistics
from collections import Counter
from typing import List, Optional

import pymupdf

from blocks import TableBlock, TextBlock

# PDF base fonts and common PostScript names -> the font Word should use.
FONT_FAMILIES = [
    ("helvetica", "Arial"), ("arial", "Arial"), ("timesnewroman", "Times New Roman"),
    ("times", "Times New Roman"), ("couriernew", "Courier New"), ("courier", "Courier New"),
    ("symbol", "Symbol"), ("zapfdingbats", "Wingdings"),
]
STYLE_SUFFIX = re.compile(r"(PSMT|MT|PS|Std|Pro|LT)$")
LINE_H = 1.15        # Word's single line height, as a multiple of the font size
WRAP_SLACK = 8.0     # points of extra width for the text column in Word
BASELINE = 0.79      # where the baseline sits in a Word line, as a fraction of its height


class StyledText(TextBlock):
    """A text block whose look was measured from the PDF: runs plus geometry."""
    runs: List[dict] = []            # {"text", "font", "size", "bold", "italic", "color"}
    y0: float = 0.0                  # top of the first line on the PDF page (orders pictures)
    top: float = 0.0                 # where Word should start it so the baseline lands as in the PDF
    n_lines: int = 1
    line_h: float = 12.0
    exact: bool = False              # multi-line: set the line spacing exactly to line_h
    indent: float = 0.0              # left indent from the text column, points
    level: int = 0                   # 1 or 2 for headings


class StyledTable(TableBlock):
    """A table whose geometry and type were measured from the PDF's own table."""
    y0: float = 0.0
    y1: float = 0.0
    col_widths: List[float] = []     # points, one per column
    row_heights: List[float] = []    # points, one per row written (header first)
    font: Optional[str] = None
    size_pt: Optional[float] = None


def word_font(pdf_name: str) -> str:
    """'ABCDEF+Helvetica-Bold' -> 'Arial', 'TimesNewRomanPSMT' -> 'Times New Roman',
    'Calibri-Bold' -> 'Calibri'."""
    name = (pdf_name or "").split("+", 1)[-1]
    family = STYLE_SUFFIX.sub("", re.split(r"[-,]", name)[0])
    key = family.lower().replace(" ", "")
    for prefix, word in FONT_FAMILIES:
        if key.startswith(prefix):
            return word
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", family) or "Arial"


def _key(text: str) -> str:
    """Letters and digits only, lowercase: how model text and PDF lines are matched, so
    quote styles, spacing and line-end hyphens never break a match."""
    return re.sub(r"[\W_]+", "", text.lower())


def _span_style(span) -> dict:
    font = span.get("font", "")
    low = font.lower()
    return {"font": word_font(font), "size": round(span["size"] * 2) / 2,
            "bold": bool(span["flags"] & 16) or any(w in low for w in ("bold", "black", "heavy", "semibold")),
            "italic": bool(span["flags"] & 2) or "italic" in low or "oblique" in low,
            "color": f"{span.get('color', 0) & 0xFFFFFF:06X}"}


class _Line:
    def __init__(self, raw_line):
        self.spans = [s for s in raw_line["spans"] if s["text"]]
        self.text = "".join(s["text"] for s in self.spans)
        self.key = _key(self.text)
        self.x0, self.y0, self.x1, self.y1 = raw_line["bbox"]
        self.base = self.spans[0]["origin"][1]
        chars = Counter()
        for s in self.spans:
            st = _span_style(s)
            chars[(st["size"], st["bold"], st["font"])] += len(s["text"].strip())
        (self.size, self.bold, self.font), _ = chars.most_common(1)[0] if chars else ((10.0, False, "Arial"), 0)


def page_lines(pdf_page) -> List[_Line]:
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_LIGATURES
    out = []
    for block in pdf_page.get_text("dict", flags=flags)["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            ln = _Line(line)
            if ln.text.strip():
                out.append(ln)
    return out


def _cover(key: str, lines: List[_Line], used: List[bool], hint: int):
    """The lines that spell out `key`, in order, and the pieces of it no line accounts for."""
    pos, got, missing, nxt = 0, [], [], hint
    while pos < len(key):
        rest = key[pos:]
        order = ([nxt] if nxt < len(lines) else []) + list(range(len(lines)))
        j = next((i for i in order if not used[i] and lines[i].key
                  and (rest.startswith(lines[i].key) or (len(rest) >= 6 and lines[i].key.startswith(rest)))), None)
        if j is not None:
            got.append(j)
            used[j] = True
            pos += min(len(lines[j].key), len(rest))
            nxt = j + 1
            continue
        # Nothing starts here: skip to the nearest later point where an unused line does.
        later = [key.find(l.key, pos + 1) for i, l in enumerate(lines) if not used[i] and len(l.key) >= 4]
        later = [p for p in later if p > pos]
        if not later:
            missing.append(rest)
            break
        missing.append(key[pos:min(later)])
        pos = min(later)
    return got, missing


def _runs(group: List[_Line], col_right: float) -> List[dict]:
    """Spans -> runs, joining lines the way the page shows them."""
    runs = []

    def add(text, style):
        if runs and all(runs[-1][k] == style[k] for k in style):
            runs[-1]["text"] += text
        else:
            runs.append({"text": text, **style})

    for i, ln in enumerate(group):
        for k, span in enumerate(ln.spans):
            text = span["text"]
            if k == 0:
                text = text.lstrip()
            if k == len(ln.spans) - 1:
                text = text.rstrip()
            if text:
                add(text, _span_style(span))
        if i + 1 < len(group) and runs:
            nxt = group[i + 1]
            if abs(nxt.base - ln.base) < 2:
                joiner = "\t"
            elif runs[-1]["text"].endswith("-") and nxt.text.lstrip()[:1].islower():
                runs[-1]["text"] = runs[-1]["text"][:-1]   # docu- / ment -> document
                joiner = ""
            elif ln.x1 < col_right - max(40.0, 4 * ln.size):
                joiner = "\n"                             # a short line: an address, a signature
            else:
                joiner = " "                              # an ordinary wrapped line
            runs[-1]["text"] += joiner
    return runs


def _align(x0, x1, page_w, col_width) -> str:
    centre, width = (x0 + x1) / 2, x1 - x0
    narrow = col_width == 0 or width < col_width * 0.85
    if narrow and abs(centre - page_w / 2) < 24 and x0 > 40 and page_w - x1 > 40:
        return "center"
    if x0 > (page_w - x1) * 3 and x0 > 100:
        return "right"
    return "left"


def _split_paragraphs(lines: List[_Line]) -> List[List[_Line]]:
    """Lines -> paragraphs: a new one where the style changes or the line gap is bigger
    than the paragraph's own line pitch. Lines on one baseline stay together."""
    runs: List[List[_Line]] = []
    for ln in lines:
        if runs and abs(runs[-1][-1].size - ln.size) < 0.6 and runs[-1][-1].bold == ln.bold:
            runs[-1].append(ln)
        else:
            runs.append([ln])
    out = []
    for run in runs:
        gaps = [b.base - a.base for a, b in zip(run, run[1:]) if abs(b.base - a.base) >= 2]
        limit = min(gaps) * 1.35 if len(gaps) >= 2 else 1.45 * run[0].size
        cur = [run[0]]
        for a, b in zip(run, run[1:]):
            if abs(b.base - a.base) >= 2 and b.base - a.base > limit:
                out.append(cur)
                cur = []
            cur.append(b)
        out.append(cur)
    return out


def _styled_paragraphs(group_lines, block, body, page_w, col_left, col_right, col_width):
    out = []
    for g in _split_paragraphs(group_lines):
        runs = _runs(g, col_right)
        text = "".join(r["text"] for r in runs).strip()
        if not text:
            continue
        chars = Counter()
        for r in runs:
            chars[(r["size"], r["bold"])] += len(r["text"].strip())
        size, bold = chars.most_common(1)[0][0]
        x0, x1 = min(l.x0 for l in g), max(l.x1 for l in g)
        align = _align(x0, x1, page_w, col_width)
        bases = []
        for ln in sorted(g, key=lambda l: l.base):
            if not bases or ln.base - bases[-1] >= 2:
                bases.append(ln.base)
        pitch = statistics.median([b - a for a, b in zip(bases, bases[1:])]) if len(bases) > 1 else None
        first_size = max(_span_style(s)["size"] for s in g[0].spans if s["text"].strip())
        line_h = pitch or LINE_H * first_size
        heading = (size >= body * 1.15) or (bold and len(text) < 80 and len(bases) <= 2 and block.kind != "list")
        out.append(StyledText(
            kind="heading" if heading else ("list" if block.kind == "list" else "paragraph"),
            text=text, align=align,
            size="large" if size >= body * 1.15 else "small" if size <= body * 0.85 else "normal",
            bold=bold, runs=runs, y0=min(l.y0 for l in g), top=bases[0] - BASELINE * line_h,
            n_lines=len(bases), line_h=line_h, exact=pitch is not None,
            indent=max(0.0, x0 - col_left) if align == "left" and x0 - col_left > 4 else 0.0,
            level=(1 if size >= body * 1.4 else 2) if heading else 0))
    return out


def _style_table(block: TableBlock, detected, lines: List[_Line]):
    """Match a model table to the PDF's table by shared cell text; copy its geometry."""
    cells = {_key(c) for row in [block.header] + list(block.rows) for c in row if _key(c)}
    best, best_hits = None, 0
    for t in detected:
        try:
            found = {_key(c or "") for row in t.extract() for c in row}
        except Exception:
            continue
        hits = len(cells & found)
        if hits > best_hits:
            best, best_hits = t, hits
    if best is None or best_hits < max(1, len(cells) // 3):
        return block
    x0, y0, x1, y1 = best.bbox
    inside = [l for l in lines if l.y0 >= y0 - 1 and l.y1 <= y1 + 1 and l.x0 >= x0 - 1 and l.x1 <= x1 + 1]
    style = Counter()
    for l in inside:
        style[(l.size, l.font)] += max(1, len(l.key))
    size, font = style.most_common(1)[0][0] if style else (None, None)
    widths = []
    for row in best.rows:
        if all(c is not None for c in row.cells):
            widths = [c[2] - c[0] for c in row.cells]
            break
    width = len(block.header) if block.has_header else len(block.rows[0]) if block.rows else 0
    heights = [r.bbox[3] - r.bbox[1] for r in best.rows]
    n_rows = len(block.rows) + (1 if block.has_header else 0)
    return StyledTable(**block.model_dump(), y0=y0, y1=y1,
                       col_widths=widths if len(widths) == width else [],
                       row_heights=heights if len(heights) == n_rows else [],
                       font=font, size_pt=size)


def restyle_page(page, pdf_page, has_images: bool) -> dict:
    """Rebuild a digital page's blocks with the PDF's own appearance, in place.

    Anything that cannot be matched is left as the model returned it. Returns counts and
    what was dropped, for the log.
    """
    info = {"text": 0, "tables": 0, "kept": 0, "dropped": [], "fonts": Counter(), "lowest": 0.0}
    lines = page_lines(pdf_page)
    if not lines:
        return info
    sizes = Counter()
    for ln in lines:
        sizes[ln.size] += len(ln.key)
        info["fonts"][ln.font] += len(ln.key)
    body = sizes.most_common(1)[0][0]
    page_w = pdf_page.rect.width
    col_left = min(l.x0 for l in lines)
    rights = sorted(l.x1 for l in lines)
    col_right = rights[int(0.95 * (len(rights) - 1))]
    col_width = max(l.x1 - l.x0 for l in lines)
    try:
        detected = pdf_page.find_tables().tables
    except Exception:
        detected = []

    used = [False] * len(lines)
    new_blocks, hint = [], 0
    for block in page.blocks:
        if isinstance(block, TableBlock):
            styled = _style_table(block, detected, lines) if detected else block
            if styled is not block:
                info["tables"] += 1
                info["lowest"] = max(info["lowest"], styled.y1)
            else:
                info["kept"] += 1
            new_blocks.append(styled)
            continue
        key = _key(block.text)
        trial = list(used)
        got, missing = _cover(key, lines, trial, hint) if key else ([], [])
        leftover = "".join(missing)
        # Lettering read off a picture is not in the text layer: drop it when the page
        # has pictures. Anything else that does not match means this block is kept as is.
        droppable = has_images and len(leftover) <= 0.5 * len(key)
        if not got or (len(leftover) > 3 and not droppable):
            new_blocks.append(block)
            info["kept"] += 1
            continue
        used = trial
        hint = got[-1] + 1
        if len(leftover) > 3:
            info["dropped"].append(leftover)
        paras = _styled_paragraphs([lines[i] for i in got], block, body, page_w,
                                   col_left, col_right, col_width)
        for p in paras:
            info["lowest"] = max(info["lowest"], p.top + p.n_lines * p.line_h)
        new_blocks.extend(paras)
        info["text"] += 1
    page.blocks[:] = new_blocks
    return info


def restyle_document(pages, pdf_pages, is_digital, image_sets, layout):
    """Restyle every digital page of a document, on copies.

    Returns (pages, layout, notes). Scanned pages come back as the same objects. The layout
    copy gets the body font, a top margin that includes any picture above the text, and a
    bottom margin low enough for a footer line, so Word does not spill it onto a new page.
    """
    out, notes, fonts, lowest = [], [], Counter(), 0.0
    for i, page in enumerate(pages):
        if not is_digital[i]:
            out.append(page)
            continue
        copy = page.model_copy(deep=True)
        info = restyle_page(copy, pdf_pages[i], bool(image_sets and i < len(image_sets) and image_sets[i]))
        out.append(copy)
        fonts.update(info["fonts"])
        lowest = max(lowest, info["lowest"])
        notes.append(f"page {i + 1}: PDF fonts, sizes and spacing copied onto {info['text']} text block(s) "
                     f"and {info['tables']} table(s)" + (f"; {info['kept']} kept as read" if info["kept"] else ""))
        for d in info["dropped"]:
            notes.append(f"page {i + 1}: dropped lettering read from a picture ({d[:40]!r})")
    new_layout = dict(layout)
    # Word sets the same text a little wider than the PDF did (kerning, rounding), so a
    # line that just fits in the PDF can spill one word onto a new line and push every
    # later paragraph down. A few points of slack on the right keeps the same wrapping.
    new_layout["right_pt"] = max(0.0, new_layout["right_pt"] - WRAP_SLACK)
    if fonts:
        new_layout["body_font"] = fonts.most_common(1)[0][0]
    if image_sets and image_sets[0]:
        top = min(im["bbox"][0] for im in image_sets[0])
        new_layout["top_pt"] = min(new_layout["top_pt"], top)
    if lowest:
        new_layout["bottom_pt"] = max(10.0, min(new_layout["bottom_pt"], new_layout["height_pt"] - lowest - 14))
    return out, new_layout, notes
