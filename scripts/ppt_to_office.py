"""
ppt_to_office.py -- convert a PowerPoint deck (.pptx) into Word (.docx), Excel (.xlsx)
or CSV.

    --to docx   one Word document: a section per slide -- the slide title as a heading,
                its text (bullets as lists), real Word tables, chart data as tables with
                the chart's picture, SmartArt text, pictures, speaker notes
    --to xlsx   one workbook, the slides one after another: tables as real grids (numbers
                as numbers), chart data as grids, text in column A, pictures in place
    --to csv    one CSV per table found in the deck (slide tables, chart data, tables in
                pictures, tab-separated text); a deck with no table gives deck_text.csv

WHERE THE CONTENT COMES FROM. Everything the deck stores is read from the file itself with
python-pptx -- never retyped by the model:
  - titles, text frames (paragraphs; bulleted paragraphs become lists), grouped shapes
  - tables, cell by cell
  - CHARTS: the chart's own data (categories and series values) becomes a table
  - SmartArt: the text in its data part
  - pictures: the original image file
  - lines of tab-separated text in a text box (a table typed as text) become a table

WHAT THE MODEL (the Qwen 2B) DOES -- on every slide:
  STRUCTURE  it reads the slide's blocks (numbered, as read from the file) and plans how
                they are written: a title for a slide without one, the role of each text
                block (heading, paragraph, list, caption), whether each table's first row is
                its header, a short name for each table (its caption, and its CSV file name),
                and text blocks that are really tables. Each part is used only where the slide
                agrees: a title or name only if its words are on the slide, a heading only if
                short, a list only if it has several lines, a "text table" only if its lines
                split into equal columns. It never writes the slide's text.
Pictures are copied into the document exactly, byte for byte, as the deck holds them (a format
Word cannot hold, such as EMF, is taken as LibreOffice draws it); the model does not read them.
If the model server is down (or --no-ai), the slides are written from the file alone.

HOW IT PROVES NOTHING WAS LOST. The written file is read back: the words the deck holds
(titles, text, tables, chart names and categories, SmartArt) must be in it (to_docx /
to_xlsx coverage checks); every CSV is read back and compared cell by cell.

Needs LibreOffice Impress only for the pictures of charts and SmartArt in Word / Excel
(sudo apt-get install -y libreoffice-impress); without it their data is still converted.
Usage:
    .venv/bin/python scripts/ppt_to_office.py input/ppt_to_word_input/deck.pptx --to docx
    .venv/bin/python scripts/ppt_to_office.py deck.pptx --to xlsx --slides 2-5
    .venv/bin/python scripts/ppt_to_office.py deck.pptx --to csv --no-ai
"""

import argparse
import csv
import glob
import os
import re
import sys
import tempfile
import time
import zipfile
from xml.etree import ElementTree as ET

import pymupdf

import ppt_convert as pc
from blocks import LayoutAnalysis, Page, TableBlock, TextBlock

OUT_DIRS = {"docx": "output/ppt_to_word_output", "xlsx": "output/ppt_to_excel_output", "csv": "output/ppt_to_csv_output"}
DEFAULT_BASE_URL = pc.DEFAULT_BASE_URL
WORD_IMAGE_TYPES = ("png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff")   # what Word / Excel can hold as they are
os.environ.setdefault("IDOX_TIMEOUT", "300")      # a structure plan that takes longer is given up
MIN_COVERAGE = 0.97
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


# ---------------------------------------------------------------------------
# reading the deck
# ---------------------------------------------------------------------------
def num(v) -> str:
    """A chart value as text: 120.0 -> "120", 4.25 -> "4.25", empty -> ""."""
    if v is None:
        return ""
    if isinstance(v, float):
        v = float(f"{v:.12g}")             # 8.200000000000001 as stored -> 8.2, as PowerPoint shows it
        return str(int(v)) if v.is_integer() else str(v)
    return str(v)


def bulleted(par, shape) -> bool:
    """A paragraph is a bullet when the slide's body placeholder holds it (bullets come
    from the layout) or it carries its own bullet character / numbering."""
    ppr = par._p.find(f"{A}pPr")
    if ppr is not None and (ppr.find(f"{A}buNone") is not None):
        return False
    if ppr is not None and (ppr.find(f"{A}buChar") is not None or ppr.find(f"{A}buAutoNum") is not None):
        return True
    try:
        return shape.is_placeholder and shape.placeholder_format.type in (2, 7)     # BODY, OBJECT
    except Exception:
        return False


def text_blocks(shape, is_title: bool) -> list:
    """A text frame as blocks: paragraphs, bullet lists, and tab-separated lines as a table."""
    out, bullets, plain = [], [], []

    def flush_bullets():
        if bullets:
            out.append(TextBlock(kind="list", text="\n".join(bullets), align="left", size="normal", bold=False))
            bullets.clear()

    def flush_plain():
        if not plain:
            return
        # two or more lines with the same number of tabs (1+) in a row are a table typed as text
        i = 0
        para = []
        while i < len(plain):
            n = plain[i].count("\t")
            j = i
            while n and j + 1 < len(plain) and plain[j + 1].count("\t") == n:
                j += 1
            if n and j > i:
                if para:
                    out.append(TextBlock(kind="paragraph", text="\n".join(para), align="left", size="normal", bold=False))
                    para = []
                rows = [[c.strip() for c in ln.split("\t")] for ln in plain[i:j + 1]]
                out.append(TableBlock(kind="table", has_header=True, n_data_rows=len(rows) - 1, n_cols=n + 1,
                                      header=rows[0], rows=rows[1:]))
                i = j + 1
            else:
                para.append(plain[i])
                i += 1
        if para:
            out.append(TextBlock(kind="paragraph", text="\n".join(para), align="left", size="normal", bold=False))
        plain.clear()

    if is_title:
        t = " ".join(shape.text_frame.text.replace("\v", " ").split())
        return [TextBlock(kind="heading", text=t, align="left", size="large", bold=True)] if t else []
    for par in shape.text_frame.paragraphs:
        lines = [ln for ln in par.text.replace("\r", "").split("\v")]
        if not any(ln.strip() for ln in lines):
            continue
        if bulleted(par, shape):
            flush_plain()
            bullets.extend(("  " * par.level) + ln.strip() for ln in lines if ln.strip())
        else:
            flush_bullets()
            plain.extend(ln.rstrip() for ln in lines if ln.strip())
    flush_bullets()
    flush_plain()
    return out


def smartart_texts(slide, shape) -> list:
    """The text of a SmartArt graphic, from its data part (python-pptx does not read it)."""
    rid = None
    for el in shape._element.iter():
        if el.tag.endswith("}relIds"):
            rid = el.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}dm")
    if not rid:
        return []
    try:
        part = slide.part.related_part(rid)
        root = ET.fromstring(part.blob)
        return [t.text.strip() for t in root.iter(f"{A}t") if t.text and t.text.strip()]
    except Exception:
        return []


def walk(shapes, key=()):
    """Every shape in reading order: (sort key, shape, in_group)."""
    items = []
    for sh in shapes:
        top = sh.top if sh.top is not None else 0
        left = sh.left if sh.left is not None else 0
        k = key + (round(top / 228600), left)          # rows of about 0.25 in, then left to right
        if sh.shape_type == 6 and hasattr(sh, "shapes"):
            items += [(k + kk[len(k):], s, True) for kk, s, _ in walk(sh.shapes, k)]
        else:
            items.append((k, sh, bool(key)))
    return sorted(items, key=lambda x: x[0])


def read_slide(slide, index: int) -> dict:
    """One slide as an ordered list of parts:
        ("blocks", [Block, ...])
        ("picture", {"blob", "ext", "name", "box"})
        ("drawing", {"what": "chart" | "SmartArt", "box"})   a picture to cut from the drawn slide
       plus its title, its speaker notes and all the text it stores."""
    parts, texts, aligns = [], [], []
    title = ""
    for _, sh, in_group in walk(slide.shapes):
        box = None if in_group or sh.left is None else (sh.left / 12700, sh.top / 12700,
                                                        (sh.left + sh.width) / 12700, (sh.top + sh.height) / 12700)
        if getattr(sh, "has_table", False):
            rows = [[c.text.replace("\v", " ").strip() for c in r.cells] for r in sh.table.rows]
            if rows:
                head = bool(sh.table.first_row) and len(rows) > 1
                parts.append(("blocks", [TableBlock(kind="table", has_header=head,
                                                    n_data_rows=len(rows) - (1 if head else 0), n_cols=len(rows[0]),
                                                    header=rows[0] if head else [], rows=rows[1:] if head else rows)]))
                texts += [c for r in rows for c in r]
        elif getattr(sh, "has_chart", False):
            ch = sh.chart
            try:
                ctitle = ch.chart_title.text_frame.text.strip() if ch.has_title else ""
            except Exception:
                ctitle = ""
            try:
                plot = ch.plots[0]
                cats = [str(c) for c in plot.categories]
                series = [(s.name or f"Series {i + 1}", list(s.values)) for i, s in enumerate(plot.series)]
            except Exception:
                cats, series = [], []
            label = f"Chart: {ctitle}" if ctitle else "Chart"
            blk = [TextBlock(kind="caption", text=f"{label} (the chart's own data)", align="left", size="normal", bold=False)]
            if cats and series:
                rows = [[cat] + [num(vals[i]) if i < len(vals) else "" for _, vals in series] for i, cat in enumerate(cats)]
                blk.append(TableBlock(kind="table", has_header=True, n_data_rows=len(rows), n_cols=len(series) + 1,
                                      header=["Category"] + [n for n, _ in series], rows=rows))
                texts += cats + [n for n, _ in series]
            if ctitle:
                texts.append(ctitle)
            if box:
                parts.append(("drawing", {"what": "chart", "box": box}))
            parts.append(("blocks", blk))
        elif sh.shape_type == 13 and hasattr(sh, "image"):
            try:
                parts.append(("picture", {"blob": sh.image.blob, "ext": sh.image.ext, "name": sh.name,
                                          "box": box or (0, 0, sh.width / 12700 if sh.width else 200,
                                                         sh.height / 12700 if sh.height else 150)}))
            except Exception:
                pass
        elif sh.shape_type == 7:                                            # an embedded object (OLE)
            if box:
                parts.append(("drawing", {"what": "embedded object", "box": box}))
        elif any(el.tag.endswith("}relIds") for el in sh._element.iter()):      # SmartArt
            st = smartart_texts(slide, sh)
            if box:
                parts.append(("drawing", {"what": "SmartArt", "box": box}))
            if st:
                parts.append(("blocks", [TextBlock(kind="list", text="\n".join(st), align="left", size="normal",
                                                   bold=False)]))
                texts += st
        elif getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip():
            is_title = False
            try:
                is_title = sh.is_placeholder and sh.placeholder_format.type in (1, 3)     # TITLE, CENTER_TITLE
            except Exception:
                pass
            blks = text_blocks(sh, is_title and not title)
            if is_title and not title and blks:
                title = blks[0].text
            parts.append(("blocks", blks))
            texts.append(sh.text_frame.text)
            # where each text block sits, to read its alignment off the drawn slide later; the
            # alignment written on its first paragraph is the fallback when nothing is drawn
            try:
                first = next(p for p in sh.text_frame.paragraphs if p.text.strip())
                written = {1: "left", 2: "center", 3: "right"}.get(int(first.alignment)) if first.alignment else None
            except Exception:
                written = None
            for b in blks:
                if isinstance(b, TextBlock):
                    aligns.append((b, box, written))
    notes = ""
    if slide.has_notes_slide:
        try:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        except Exception:
            notes = ""
    return {"index": index, "title": title, "parts": parts, "notes": notes, "text": "\n".join(texts),
            "hidden": slide._element.get("show") == "0", "aligns": aligns}


def measured_align(page, block, box):
    """"center" / "right" / "left" for a text block, read off the slide as Impress drew it:
    where its first line sits inside its own box. None if the line cannot be found. This sees
    alignment that comes from the slide's layout or theme, which the text itself does not say."""
    if page is None or box is None:
        return None
    first = next((ln for ln in block.text.splitlines() if ln.strip()), "")
    key = re.sub(r"\W+", "", first.lower())[:20]
    if not key:
        return None
    x0, y0, x1, y1 = box
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            t = re.sub(r"\W+", "", "".join(s["text"] for s in ln["spans"]).lower())
            lx0, ly0, lx1, ly1 = ln["bbox"]
            if key[:12] not in t or not (y0 - 4 <= (ly0 + ly1) / 2 <= y1 + 4):
                continue
            width = x1 - x0
            if lx1 - lx0 > 0.9 * width:
                return None                 # a line filling its box says nothing about alignment
            if abs((lx0 + lx1) / 2 - (x0 + x1) / 2) <= max(6, 0.04 * width):
                return "center"
            if (x1 - lx1) < (lx0 - x0) and (x1 - lx1) <= max(8, 0.05 * width):
                return "right"
            return "left"
    return None


# ---------------------------------------------------------------------------
# the model: text inside pictures
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# the model: the structure of every slide
# ---------------------------------------------------------------------------
from typing import List, Literal
from pydantic import BaseModel, Field


class BlockRole(BaseModel):
    id: int = Field(description="The block number from the list.")
    role: Literal["heading", "paragraph", "list", "caption"]


class TableInfo(BaseModel):
    id: int = Field(description="The table's block number.")
    has_header: bool = Field(description="True if its first row holds column labels.")
    name: str = Field(description="A short name for the table, at most 6 words, from words on the slide.")


class SlideStructure(BaseModel):
    title: str = Field(description="The slide's title, at most 10 words, from words on the slide.")
    roles: List[BlockRole] = Field(max_length=30, description="The role of every TEXT block.")
    tables: List[TableInfo] = Field(max_length=10, description="One entry per TABLE block; [] if none.")
    text_tables: List[int] = Field(max_length=5, description="Numbers of TEXT blocks that are really a table "
                                                             "(rows of values lined up in columns); [] if none.")


STRUCTURE_TEMPLATE = """You plan how ONE presentation slide is written into a {target}. A program has
already read every block of the slide from the file and copies the text itself; you never
write or change the slide's text. You read the numbered blocks and answer with a JSON plan.

RULES
1. title: the slide's title, built from words on the slide. If a block is clearly the title,
   use its text.
2. roles: one entry for every TEXT block (not tables):
   - heading    a short line that introduces what follows (a title, a section name)
   - paragraph  ordinary sentences
   - list       several short points, one per line
   - caption    a short label under or over a table, chart or picture (a source, a note)
3. tables: one entry for every TABLE block. has_header is true when the first row holds
   column labels (words such as Region, Name, Total) rather than data. name: a short name
   for the table from words on the slide (for example "Regional sales").
4. text_tables: TEXT blocks whose lines are rows of values lined up in columns (separated by
   tabs or several spaces). [] if none.
5. Use only block numbers from the list. Never invent text."""


def ask_structure(base_url: str, target: str, blocks: list):
    """The model's plan for a slide's blocks: (SlideStructure, seconds). Raises on failure."""
    import requests

    lines = []
    for i, b in enumerate(blocks, 1):
        if isinstance(b, TableBlock):
            first = b.header if b.has_header else (b.rows[0] if b.rows else [])
            second = b.rows[0] if b.has_header and b.rows else (b.rows[1] if len(b.rows) > 1 else [])
            lines.append(f"[{i}] TABLE {len(b.rows) + (1 if b.has_header else 0)} rows x {b.n_cols} columns; "
                         f"row 1: {' | '.join(first)[:120]}; row 2: {' | '.join(second)[:120]}")
        else:
            t = b.text.replace("\t", " <tab> ")
            n = len(b.text.splitlines())
            lines.append(f"[{i}] TEXT ({n} line{'s' if n != 1 else ''}): {t[:300]}")
    body = {
        "model": os.environ.get("IDOX_MODEL", "manual") or "manual",
        "messages": [{"role": "system", "content": STRUCTURE_TEMPLATE.format(target=target)},
                     {"role": "user", "content": "BLOCKS OF THE SLIDE:\n" + "\n".join(lines)}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "slide_structure", "schema": SlideStructure.model_json_schema()}},
        "temperature": 0, "repeat_penalty": 1.1, "n_predict": 700,
    }
    key = os.environ.get("IDOX_API_KEY", "").strip()
    t0 = time.time()
    resp = requests.post(f"{base_url.rstrip('/')}/v1/chat/completions", json=body,
                         headers={"Authorization": f"Bearer {key}"} if key else {},
                         timeout=int(os.environ.get("IDOX_TIMEOUT", "300") or 300))
    if not resp.ok:
        raise RuntimeError(f"the server answered {resp.status_code}: {resp.text[:120].strip()}")
    content = resp.json()["choices"][0]["message"].get("content") or ""
    return SlideStructure.model_validate_json(content), time.time() - t0


def split_columns(text: str):
    """A text block as a grid: lines split on tabs or on 2+ spaces. None unless there are 2+
    lines, all with the same number (2+) of columns."""
    rows = [[c.strip() for c in re.split(r"\t|\s{2,}", ln.strip()) if c.strip()] for ln in text.splitlines() if ln.strip()]
    if len(rows) < 2 or len({len(r) for r in rows}) != 1 or len(rows[0]) < 2:
        return None
    return rows


def apply_structure(plan, blocks: list, slide_text: str, has_title: bool):
    """The plan applied where the slide agrees with it. Returns (blocks, title or None,
    table names {block index: name}, notes)."""
    notes, names, applied = [], {}, 0
    out = list(blocks)
    n = len(out)
    # roles: only where the text fits the role
    for r in plan.roles:
        i = r.id - 1
        if not 0 <= i < n or isinstance(out[i], TableBlock):
            notes.append(f"role for block {r.id} ignored (not a text block)")
            continue
        b = out[i]
        lines = [ln for ln in b.text.splitlines() if ln.strip()]
        if r.role == "heading" and (len(b.text) > 120 or len(lines) > 2):
            notes.append(f"block {r.id} kept as {b.kind}: too long for a heading")
            continue
        if r.role == "list" and len(lines) < 2:
            notes.append(f"block {r.id} kept as {b.kind}: one line is not a list")
            continue
        if r.role != b.kind and b.kind != "heading":
            out[i] = b.model_copy(update={"kind": r.role, "bold": r.role == "heading" or b.bold,
                                          "size": "small" if r.role == "caption" else b.size})
            applied += 1
    # tables: header flag and a grounded name
    for t in plan.tables:
        i = t.id - 1
        if not 0 <= i < n or not isinstance(out[i], TableBlock):
            notes.append(f"table entry {t.id} ignored (not a table)")
            continue
        tb = out[i]
        if t.has_header != tb.has_header:
            allrows = ([tb.header] if tb.has_header else []) + tb.rows
            if len(allrows) >= 2:
                head, body = (allrows[0], allrows[1:]) if t.has_header else ([], allrows)
                out[i] = TableBlock(kind="table", has_header=t.has_header, n_data_rows=len(body),
                                    n_cols=tb.n_cols, header=head, rows=body)
                applied += 1
        if t.name.strip() and pc.grounded(t.name, slide_text):
            names[i] = t.name.strip()
        elif t.name.strip():
            notes.append(f"table name {t.name!r} not on the slide, not used")
    # text that is really a table: only if it splits into a consistent grid
    for tid in plan.text_tables:
        i = tid - 1
        if not 0 <= i < n or isinstance(out[i], TableBlock):
            continue
        rows = split_columns(out[i].text)
        if rows:
            out[i] = TableBlock(kind="table", has_header=True, n_data_rows=len(rows) - 1, n_cols=len(rows[0]),
                                header=rows[0], rows=rows[1:])
            applied += 1
        else:
            notes.append(f"block {tid} called a table, but its lines do not split into equal columns: kept as text")
    title = None
    if not has_title and ("\n" in plan.title.strip() or len(plan.title.split()) > 10):
        notes.append(f"title {plan.title[:40]!r}... is not a title (several lines or over 10 words), not used")
    elif not has_title and plan.title.strip() and pc.grounded(plan.title, slide_text):
        title = plan.title.strip()
    elif not has_title and plan.title.strip():
        notes.append(f"title {plan.title!r} not on the slide, not used")
    return out, title, names, notes, applied


def page_of(blocks: list) -> Page:
    return Page(analysis=LayoutAnalysis(n_blocks=len(blocks),
                                        table_column_counts=[b.n_cols for b in blocks if isinstance(b, TableBlock)]),
                blocks=blocks)


def run(pptx: str, to: str, out_dir: str, slides_spec: str, base_url: str, use_ai: bool) -> bool:
    from pptx import Presentation

    name = os.path.splitext(os.path.basename(pptx))[0]
    print("=" * 72)
    print(pptx)
    print("=" * 72)
    started = time.time()
    try:
        prs = Presentation(pptx)
        slides = [read_slide(s, i) for i, s in enumerate(prs.slides, 1)]
    except Exception as exc:
        print(f"  COULD NOT OPEN: {exc}")
        return False
    visible = [s for s in slides if not s["hidden"]]
    wanted = set(pc.parse_slides(slides_spec, len(slides)))
    targets = [s for s in visible if s["index"] in wanted]
    hidden = len(slides) - len(visible)
    if not targets:
        print(f"  SKIPPED: no slide to convert (slides: {len(slides)}, hidden: {hidden})")
        return False
    os.makedirs(out_dir, exist_ok=True)
    print(f"  {len(slides)} slide(s)" + (f" ({hidden} hidden, left out)" if hidden else "")
          + f", converting {len(targets)} as {to.upper()} -> {out_dir}/")

    with tempfile.TemporaryDirectory() as tmp:
        # pictures of charts and SmartArt are cut from the slides as Impress draws them
        drawn = None
        if to in ("docx", "xlsx"):
            if pc.have_impress():
                try:
                    doc = pymupdf.open(pc.to_pdf(os.path.abspath(pptx), tmp))
                    if doc.page_count == len(visible):
                        drawn = {s["index"]: doc[i] for i, s in enumerate(visible)}
                except Exception as exc:
                    print(f"  ~ could not draw the slides for chart pictures ({exc}); their data is still converted")
            else:
                print("  ~ LibreOffice Impress is not installed: charts, SmartArt and embedded objects are converted "
                      "as data only, and the text is checked against what was read, not against the drawn slides")

        pages, images, tables, problems, source, held_words = [], [], [], [], [], 0
        ai_slides, tb_names = 0, {}
        for s in targets:
            blocks, pics, notes_lines, held = [], [], [], []      # held: areas kept as pictures
            # each text block keeps the alignment it has on the slide: centred only if the slide
            # centres it (measured on the drawn slide, else as written on the text), else left
            n_centred = 0
            for b, box, written in s["aligns"]:
                b.align = measured_align(drawn[s["index"]] if drawn else None, b, box) or written or "left"
                n_centred += b.align == "center" and b.kind == "heading"
            if not s["title"]:
                blocks.append(TextBlock(kind="heading", text=f"Slide {s['index']}", align="left", size="large", bold=True))
            n_pic = n_tab = 0
            for kind, item in s["parts"]:
                if kind == "blocks":
                    blocks += item
                    n_tab += sum(1 for b in item if isinstance(b, TableBlock))
                    continue
                if kind == "drawing":
                    if drawn is None:
                        continue
                    x0, y0, x1, y1 = item["box"]
                    page = drawn[s["index"]]
                    rect = pymupdf.Rect(x0 - 2, y0 - 2, x1 + 2, y1 + 2) & page.rect
                    if rect.is_empty or rect.width < 10:
                        continue
                    path = os.path.join(tmp, f"s{s['index']:02d}_{item['what'].replace(' ', '_')}_{len(pics)}.png")
                    page.get_pixmap(dpi=200, clip=rect, alpha=False).save(path)
                    pics.append({"path": path, "width_in": rect.width / 72, "height_in": rect.height / 72,
                                 "at": len(blocks), "bbox": (rect.y0, rect.y1), "x": (rect.x0, rect.x1)})
                    if item["what"] == "embedded object":
                        held.append(rect)
                    continue
                # a picture: copied into the document exactly, byte for byte, as the deck holds it
                n_pic += 1
                x0, y0, x1, y1 = item["box"]
                held.append(pymupdf.Rect(x0, y0, x1, y1))
                if to == "csv":
                    continue
                ext = (item["ext"] or "").lower()
                if ext in WORD_IMAGE_TYPES:
                    path = os.path.join(tmp, f"s{s['index']:02d}_pic{n_pic}.{ext}")
                    with open(path, "wb") as fh:
                        fh.write(item["blob"])
                elif drawn is not None:
                    # Word cannot hold this format (EMF / WMF / SVG ...): take it as Impress draws it
                    page = drawn[s["index"]]
                    rect = pymupdf.Rect(x0, y0, x1, y1) & page.rect
                    path = os.path.join(tmp, f"s{s['index']:02d}_pic{n_pic}.png")
                    page.get_pixmap(dpi=200, clip=rect, alpha=False).save(path)
                    print(f"  slide {s['index']} picture {n_pic}: a .{ext} picture, which Word cannot hold; "
                          f"taken from the drawn slide instead")
                else:
                    print(f"  slide {s['index']} picture {n_pic}: a .{ext} picture, which Word cannot hold, and "
                          f"LibreOffice Impress is not installed to draw it; left out")
                    continue
                pics.append({"path": path, "width_in": max(0.5, (x1 - x0) / 72), "height_in": max(0.5, (y1 - y0) / 72),
                             "at": len(blocks), "bbox": (y0, y1), "x": (x0, x1)})
            # the model's structure plan for this slide, checked before it is used
            if use_ai and blocks:
                head = 0 if s["title"] else 1          # leave the "Slide N" placeholder out of the plan
                try:
                    plan, secs = ask_structure(base_url, {"docx": "Word document", "xlsx": "spreadsheet",
                                                          "csv": "set of CSV files"}[to], blocks[head:])
                    new, ai_title, names, snotes, applied = apply_structure(plan, blocks[head:], s["text"],
                                                                            bool(s["title"]))
                    blocks[head:] = new
                    if ai_title:
                        blocks[0] = TextBlock(kind="heading", text=ai_title, align="left", size="large", bold=True)
                        s["ai_title"] = ai_title
                    # a named table gets its name as a caption (Word / Excel) and in its CSV file name
                    for j in sorted(names, reverse=True):
                        tb = blocks[head + j]
                        tb_names[id(tb)] = names[j]
                        if to != "csv":
                            blocks.insert(head + j, TextBlock(kind="caption", text=f"Table: {names[j]}", align="left",
                                                              size="small", bold=True))
                            for p in pics:
                                if p["at"] > head + j:
                                    p["at"] += 1
                    ai_slides += 1
                    print(f"  slide {s['index']} AI structure ({secs:.1f}s): {applied} change(s) applied"
                          + (f", title {ai_title!r}" if ai_title else "")
                          + (f", table name(s): {', '.join(repr(v) for v in names.values())}" if names else "")
                          + (f"; rejected: {'; '.join(snotes[:3])}" if snotes else ""))
                except Exception as exc:
                    why = (f"the model server at {base_url} could not be reached" if "Max retries" in str(exc)
                           or "ConnectionError" in type(exc).__name__ else str(exc).splitlines()[0][:120])
                    print(f"  slide {s['index']} AI structure: not used ({why})")
            if s["notes"] and to == "docx":
                blocks.append(TextBlock(kind="caption", text=f"Speaker notes: {s['notes']}", align="left",
                                        size="small", bold=False))
            n = max(1, len(blocks))
            for p in pics:
                p["frac_above"] = p.pop("at") / n
            pages.append(page_of(blocks))
            images.append(pics)
            for b in blocks:
                if isinstance(b, TableBlock):
                    tables.append((s["index"], b))
            if drawn:
                ws = drawn[s["index"]].get_text("words")
                inside = [w for w in ws if any(r.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)) for r in held)]
                held_words += len(inside)
                # bullets drawn from symbol fonts come out as private-use characters glued to the
                # first word ("\uf06cFinancial"); they are decoration, not text
                seen = re.sub(r"[\ue000-\uf8ff\u2022\u25aa\u25cf\u2023\u2043]", " ",
                              " ".join(w[4] for w in ws if w not in inside))
            else:
                seen = s["text"]
            source.append(seen + ("\n" + s["notes"] if s["notes"] and to == "docx" else ""))
            print(f"  slide {s['index']}: {s['title'] or '(no title)'!r}: {len(blocks)} block(s), {n_tab} table(s)"
                  + (f", {n_pic} picture(s)" if n_pic else "")
                  + (f", {sum(1 for k, _ in s['parts'] if k == 'drawing')} chart / SmartArt / object picture(s)" if any(
                      k == "drawing" for k, _ in s["parts"]) else "")
                  + (", speaker notes" if s["notes"] and to == "docx" else "")
                  + (f", {n_centred} heading(s) centred as on the slide" if n_centred else ""))

        # write and read back
        files = []
        src = "\n".join(source)
        try:
            if to == "docx":
                from to_docx import build_docx, verify_docx
                path = os.path.join(out_dir, f"{name}.docx")
                problems += build_docx(pages, path, image_sets=images) or []
                cov, missing = verify_docx(path, src)
                files.append(path)
            elif to == "xlsx":
                from to_xlsx import build_xlsx, verify_xlsx
                path = os.path.join(out_dir, f"{name}.xlsx")
                problems += build_xlsx(pages, path, image_sets=images, sheet_name="Slides") or []
                cov, missing = verify_xlsx(path, src)
                files.append(path)
            else:
                folder = os.path.join(out_dir, name)
                os.makedirs(folder, exist_ok=True)
                jobs, counter = [], {}
                for idx, tb in tables:
                    counter[idx] = counter.get(idx, 0) + 1
                    label = pc.safe_name(tb_names[id(tb)]) if id(tb) in tb_names else f"table_{counter[idx]}"
                    jobs.append((f"slide_{idx:02d}_{label}.csv", ([tb.header] if tb.has_header else []) + tb.rows))
                if not jobs:
                    rows = [["slide", "title", "kind", "text"]]
                    for s, pg in zip(targets, pages):
                        for b in pg.blocks:
                            if isinstance(b, TextBlock):
                                rows.append([str(s["index"]), s["title"], b.kind, b.text])
                    jobs.append(("deck_text.csv", rows))
                    print("  no table in the deck: deck_text.csv lists every text block, one per row")
                bad = 0
                for fn, rows in jobs:
                    p = os.path.join(folder, fn)
                    with open(p, "w", newline="", encoding="utf-8-sig") as fh:
                        csv.writer(fh).writerows(rows)
                    with open(p, newline="", encoding="utf-8-sig") as fh:
                        if list(csv.reader(fh)) != [[str(v) for v in r] for r in rows]:
                            problems.append(f"{fn}: read back differs")
                            bad += 1
                    files.append(p)
                print(f"  CSV files: {len(jobs)}, {len(jobs) - bad}/{len(jobs)} read back identical: "
                      + ", ".join(f"{fn} ({len(r)} rows)" for fn, r in jobs[:8]) + (" ..." if len(jobs) > 8 else ""))
                in_csv = " ".join(v for _, r in jobs for row in r for v in row)
                table_words = pc.words(" ".join(v for _, tb in tables for row in [tb.header] + tb.rows for v in row))
                have = set(pc.words(in_csv))
                cov = (sum(w in have for w in table_words) / len(table_words)) if table_words else 1.0
                missing = sorted({w for w in table_words if w not in have})
        except Exception as exc:
            print(f"  COULD NOT WRITE: {exc}")
            return False

    if to != "csv" or tables:
        what = ("the words drawn on the slides" if drawn else "the deck's words") if to != "csv" else "the tables' words"
        print(f"  word coverage:     {cov:.1%} of {what} are in the file"
              + (f" (missing e.g. {', '.join(sorted(missing)[:6])})" if missing else ""))
        if held_words:
            print(f"  ~ {held_words} word(s) drawn inside pictures / embedded objects are kept as part of those "
                  f"pictures (copied exactly as pictures), not counted above")
        if cov < MIN_COVERAGE:
            problems.append(f"word coverage {cov:.1%} is below {MIN_COVERAGE:.0%}")
    for p in problems:
        print(f"  ! {p}")
    ok = not problems and all(os.path.exists(f) for f in files)
    print()
    print("  --- verification (read back from the written file) ---")
    print(f"  slides converted:  {len(targets)}/{len(targets)}")
    print(f"  files written:     {len(files)}  ({', '.join(os.path.basename(f) for f in files[:5])}"
          + (" ..." if len(files) > 5 else "") + ")")
    print(f"  AI structure:      {ai_slides}/{len(targets)} slide(s) planned by the model")
    print(f"  {time.time() - started:.1f}s")
    print(f"  VERDICT: {'PASS -- converted and verified' if ok else 'FAIL -- see the problems above'}")
    print()
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", help="PowerPoint file(s) (.pptx), globs allowed")
    parser.add_argument("--to", choices=("docx", "xlsx", "csv"), required=True)
    parser.add_argument("--outdir", default=None, help="default output/ppt_to_word_output / output/ppt_to_excel_output / output/ppt_to_csv_output")
    parser.add_argument("--slides", default="all", help="e.g. 1, 1-3, 2,4 (default: all visible slides)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"the model server (default {DEFAULT_BASE_URL})")
    parser.add_argument("--no-ai", action="store_true", help="skip the model (structure plan and pictures); for a quick check only")
    args = parser.parse_args()
    paths = []
    for pattern in args.files:
        paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern])
    ok = fail = 0
    for path in paths:
        if not os.path.exists(path):
            print(f"skipping {path}: not found")
            continue
        if run(path, args.to, args.outdir or OUT_DIRS[args.to], args.slides, args.base_url, not args.no_ai):
            ok += 1
        else:
            fail += 1
    if len(paths) > 1:
        print("=" * 72)
        print(f"{ok} succeeded, {fail} failed, {len(paths)} total")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
