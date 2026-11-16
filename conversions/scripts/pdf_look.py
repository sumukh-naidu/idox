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
PDF's own table (PyMuPDF find_tables). A table the PDF has but the model did not return is
built from the PDF's own cells.

The model's blocks decide what KIND each piece is (heading, paragraph, list); the PDF
decides what is there and in what ORDER. Every piece is placed by the PDF lines it covers,
each line is used once (text the model wrote twice is dropped), and lines no block covered
are put back where they sit. A block whose lines cannot be found is kept untouched, in its
place. Scanned pages never come here: no text layer, nothing to measure. Works on copies;
the model's pages are not changed.
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


def _starts(rest: str, line_key: str) -> bool:
    return bool(line_key) and (rest.startswith(line_key) or (len(rest) >= 6 and line_key.startswith(rest)))


def _cover(key: str, lines: List[_Line], used: List[bool], hint: int):
    """The unused lines that spell out `key`, in order; how much of `key` repeats lines an
    earlier block or a table already holds; and the pieces of it no line accounts for.

    The model sometimes writes the same text twice, e.g. a title block that swallows the
    heading, caption and paragraph below it, then those three again as their own blocks.
    Text found in an already-used line is a repeat, not a gap, so it is skipped rather than
    counted as missing."""
    pos, got, missing, nxt, repeated = 0, [], [], hint, 0
    while pos < len(key):
        rest = key[pos:]
        order = ([nxt] if nxt < len(lines) else []) + list(range(len(lines)))
        j = next((i for i in order if not used[i] and _starts(rest, lines[i].key)), None)
        if j is not None:
            got.append(j)
            used[j] = True
            pos += min(len(lines[j].key), len(rest))
            nxt = j + 1
            continue
        taken = [i for i in range(len(lines)) if used[i] and _starts(rest, lines[i].key)]
        if taken:
            step = min(max(len(lines[i].key) for i in taken), len(rest))
            repeated += step
            pos += step
            continue
        # Nothing starts here: skip to the nearest later point where a line does.
        later = [key.find(l.key, pos + 1) for l in lines if len(l.key) >= 4]
        later = [p for p in later if p > pos]
        if not later:
            missing.append(rest)
            break
        missing.append(key[pos:min(later)])
        pos = min(later)
    return got, missing, repeated


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
    """Match a model table to the PDF's table by shared cell text; copy its geometry.

    Returns (block, index of the PDF table it matched, or None)."""
    cells = {_key(c) for row in [block.header] + list(block.rows) for c in row if _key(c)}
    best, best_hits, best_i = None, 0, None
    for i, t in enumerate(detected):
        try:
            found = {_key(c or "") for row in t.extract() for c in row}
        except Exception:
            continue
        hits = len(cells & found)
        if hits > best_hits:
            best, best_hits, best_i = t, hits, i
    if best is None or best_hits < max(1, len(cells) // 3):
        return block, None
    return _with_geometry(block, best, lines), best_i


def _with_geometry(block: TableBlock, best, lines: List[_Line]):
    x0, y0, x1, y1 = best.bbox
    inside = [l for l in lines if _inside(l, best.bbox)]
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


def _inside(line: _Line, bbox) -> bool:
    x0, y0, x1, y1 = bbox
    return line.y0 >= y0 - 1 and line.y1 <= y1 + 1 and line.x0 >= x0 - 1 and line.x1 <= x1 + 1


def _table_from_pdf(t, lines: List[_Line]) -> Optional[TableBlock]:
    """A table the PDF has but the model did not return, built from the PDF's own cells.

    The cells are the text layer's exact characters, so nothing is read or guessed. Empty
    rows are left out: find_tables() often reports a phantom empty last row. The first row
    is the header when its text is bold. None when there is no real table (under 2x2)."""
    try:
        rows = [["" if c is None else " ".join(str(c).split()) for c in r] for r in t.extract()]
    except Exception:
        return None
    rows = [r for r in rows if any(r)]
    if len(rows) < 2 or max(len(r) for r in rows) < 2:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    first = [l for l in lines if _inside(l, t.rows[0].bbox)] if t.rows else []
    has_header = bool(first) and all(l.bold for l in first)
    header, body = (rows[0], rows[1:]) if has_header else ([], rows)
    block = TableBlock(kind="table", has_header=has_header, n_data_rows=len(body), n_cols=width,
                       header=header, rows=body)
    return _with_geometry(block, t, lines)


def restyle_page(page, pdf_page, has_images: bool) -> dict:
    """Rebuild a digital page's blocks with the PDF's own appearance, in place.

    Anything that cannot be matched is left as the model returned it. Returns counts and
    what was dropped, for the log.
    """
    info = {"text": 0, "tables": 0, "kept": 0, "built": 0, "dropped": [], "repeats": [], "restored": [],
            "fonts": Counter(), "lowest": 0.0}
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

    # The model decides what KIND each piece is; on a digital page the PDF decides what is
    # there and in what order. The model's blocks are unreliable for both: it has merged a
    # title with the heading, caption and paragraph below it and then written those again
    # as blocks of their own, and returned a whole page, table included, as one heading.
    # So every block is placed by the PDF lines it covers, each line is used once, tables
    # come from the PDF's own table finder, and lines no block covered are put back.
    built = {k: _table_from_pdf(t, lines) for k, t in enumerate(detected)}
    table_of = [next((k for k, t in enumerate(detected) if built[k] and _inside(ln, t.bbox)), None)
                for ln in lines]
    used = [k is not None for k in table_of]   # a real table's lines belong to the table
    placed, hint, last, tables_done = [], 0, -1.0, set()

    def table_at(k):
        idx = [i for i, tk in enumerate(table_of) if tk == k]
        return min(idx) if idx else sum(1 for ln in lines if ln.y0 < detected[k].bbox[1]) - 0.5

    def lowest(block):
        bottom = block.y1 if isinstance(block, StyledTable) else block.top + block.n_lines * block.line_h
        info["lowest"] = max(info["lowest"], bottom)

    for block in page.blocks:
        if isinstance(block, TableBlock):
            styled, k = _style_table(block, detected, lines) if detected else (block, None)
            if k is None:
                last += 0.001                       # not one of the PDF's tables: keep its place
                placed.append((last, block))
                info["kept"] += 1
            elif k in tables_done:
                info["repeats"].append(f"table: {' | '.join(block.header or (block.rows or [[]])[0])}")
            else:
                tables_done.add(k)
                for i, ln in enumerate(lines):
                    if _inside(ln, detected[k].bbox):
                        used[i] = True
                last = table_at(k)
                placed.append((last, styled))
                lowest(styled)
                info["tables"] += 1
            continue
        key = _key(block.text)
        trial = list(used)
        got, missing, repeated = _cover(key, lines, trial, hint) if key else ([], [], 0)
        leftover = "".join(missing)
        # Lettering read off a picture is not in the text layer: drop it when the page
        # has pictures. Anything else that does not match means this block is kept as is.
        droppable = has_images and len(leftover) <= 0.5 * len(key)
        if not got and repeated and (len(leftover) <= 3 or droppable):
            info["repeats"].append(block.text)      # everything it says is already placed
            continue
        if not got or (len(leftover) > 3 and not droppable):
            used = trial                            # its lines are in it: never add them again
            last = (min(got) if got else last) + 0.001
            placed.append((last, block))
            info["kept"] += 1
            continue
        used = trial
        hint = got[-1] + 1
        if len(leftover) > 3:
            info["dropped"].append(leftover)
        for run in _runs_of(sorted(got)):
            for p in _styled_paragraphs([lines[i] for i in run], block, body, page_w,
                                        col_left, col_right, col_width):
                placed.append((run[0], p))
                lowest(p)
        last = max(got)
        info["text"] += 1

    # The PDF's tables the model did not return, from the PDF's own cells.
    for k in range(len(detected)):
        if built[k] and k not in tables_done:
            placed.append((table_at(k), built[k]))
            lowest(built[k])
            info["built"] += 1
    # Lines no block accounted for (left out by the model, or misread so they could not be
    # matched) go back where they sit, so no line of the page is lost. Bare page numbers and
    # fragments of 1-3 characters are left out, as the content check does.
    for run in _runs_of([i for i, ln in enumerate(lines)
                         if not used[i] and len(ln.key) > 3 and not ln.key.isdigit()]):
        filler = TextBlock(kind="paragraph", text=" ".join(lines[i].text for i in run),
                           align="left", size="normal", bold=False)
        for p in _styled_paragraphs([lines[i] for i in run], filler, body, page_w,
                                    col_left, col_right, col_width):
            placed.append((run[0], p))
            lowest(p)
        info["restored"].append(filler.text)
    placed.sort(key=lambda item: item[0])
    page.blocks[:] = [b for _, b in placed]
    return info


def _runs_of(indices: List[int]) -> List[List[int]]:
    """[0, 4, 5, 6, 9] -> [[0], [4, 5, 6], [9]]: a paragraph never spans lines of other content."""
    runs: List[List[int]] = []
    for i in indices:
        if runs and i == runs[-1][-1] + 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


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
        for r in info["repeats"]:
            notes.append(f"page {i + 1}: dropped text the model wrote twice ({r[:50]!r})")
        if info["built"]:
            notes.append(f"page {i + 1}: {info['built']} table(s) the model missed, built from the PDF's own cells")
        for r in info["restored"]:
            notes.append(f"page {i + 1}: put back a line no block covered ({r[:50]!r})")
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
