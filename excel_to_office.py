"""
excel_to_office.py -- convert an Excel workbook (.xlsx) into PDF, Word (.docx),
PowerPoint (.pptx) or CSV.

    --to pdf    one PDF; each sheet drawn by LibreOffice Calc exactly as in Excel (a sheet
                too tall for one page is cut into pages between rows)
    --to docx   one Word document; each sheet becomes headings, paragraphs and REAL Word
                tables, one sheet per page, charts as pictures
    --to pptx   one deck; a slide per section of each sheet, long tables continued on more
                slides with the header repeated, charts on their own slides
    --to csv    CSV files; by default one per TABLE (clean data, no titles or notes), or one
                per sheet with --csv-scope sheet

WHERE THE VALUES COME FROM -- NEVER FROM THE MODEL. Every cell's text is taken from the
workbook exactly as Excel shows it: LibreOffice Calc opens the file, calculates the
formulas and exports each sheet's displayed values ("$125,000.50", "14 Mar 2022",
"TRUE"). A number is never re-typed, so it cannot be misread.

WHAT THE MODEL (the Qwen 2B) DOES. It reads an outline of each sheet (excel_to_image.py's
outline: every row in a few words, runs of similar rows collapsed) and labels its STRUCTURE:
which rows are a title, a table's header, its data, its totals, notes. A document needs
that structure -- Word has headings, paragraphs and tables, a spreadsheet only has cells.
The labels are checked against the file before they are used:

  - a TABLE is decided by the file: two or more consecutive rows holding two or more
    cells each (the same rule that draws table borders in excel_to_image.py). The model
    cannot merge a title into a table or split a table in two.
  - the model's labels decide what the file cannot: whether the first row of a table is
    its header, whether a one-cell row under a table is its TOTAL (then it joins the
    table), and whether a one-cell row is a heading or a paragraph.
  - every row the model leaves out still goes in (as a paragraph), so nothing is lost.

If the model server is down (or --no-ai), the same rules run without its labels: a bold or
filled row is a heading or header, everything else a paragraph.

HOW IT PROVES NOTHING WAS LOST. The written file is read back:
  pdf    every text cell's words are on the PDF's pages
  docx   word coverage of the document against the sheet text (to_docx.verify_docx)
  pptx   word coverage of the deck against the sheet text (to_pptx.verify_pptx)
  csv    every CSV is read back and compared CELL BY CELL with the values it was made from

Needs LibreOffice Calc:  sudo apt-get install -y libreoffice-calc
Usage:
    .venv/bin/python excel_to_office.py excel_to_image_input/sample_sales_report.xlsx --to docx
    .venv/bin/python excel_to_office.py report.xlsx --to csv --csv-scope sheet
    .venv/bin/python excel_to_office.py report.xlsx --to pptx --sheets 1 --no-ai
"""

import argparse
import csv
import glob
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from xml.etree import ElementTree as ET

import pymupdf

import excel_to_image as x2i
from blocks import LayoutAnalysis, Page, TableBlock, TextBlock

TARGETS = ("pdf", "docx", "pptx", "csv")
OUT_DIRS = {"pdf": "excel_to_pdf_output", "docx": "excel_to_word_output",
            "pptx": "excel_to_ppt_output", "csv": "excel_to_csv_output"}
SLIDE_ROWS = 12           # data rows per slide; a longer table continues on the next slide
SLIDE_PARAGRAPHS = 6      # text blocks per slide
PDF_MAX_PAGE_PT = 1440    # a sheet taller than 20 inches is cut into pages
MIN_COVERAGE = 0.97

DOC_TEMPLATE = """You label the STRUCTURE of ONE spreadsheet sheet, so a program can rewrite it
as a {target}. The program copies every value from the file itself; you never write cell
values. You read an outline of the sheet and answer with a JSON plan.

STRUCTURE RULES
1. Every row that has content belongs to exactly one region. Regions are in
   top-to-bottom order and leave no gap that holds content.
2. Region kinds: title (a heading above the data), header (the column labels of a
   table), table (its data rows), totals (a total / subtotal row of the table above),
   notes (sentences: explanations, footnotes), chart (rows a chart sits over), other.
3. A table's header row is its own region of kind header, directly above the table.
4. A row with one cell is a title or notes, unless it is a total row right under a
   table (kind totals).
5. Rows marked [bold] or [fill] are usually a title, header or totals row.
6. Use only row numbers that appear in the outline. Never invent rows.

PAGE BREAKS: split_after_rows lists rows after which a new page or slide may start: the
end of a table, or a blank row between sections. [] if the sheet is short.
WARNINGS: [] unless there is a real risk."""


# ---------------------------------------------------------------------------
# values as Excel shows them
# ---------------------------------------------------------------------------
def shown_values(xlsx: str, tmp: str) -> dict:
    """{sheet name: {(row, col): text}} -- each cell's text as Excel displays it, formulas
    calculated, exported by LibreOffice Calc (one CSV per sheet, starting at A1)."""
    out_dir = os.path.join(tmp, "shown")
    os.makedirs(out_dir, exist_ok=True)
    flt = "csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,true,false,false,-1"
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([x2i.find_soffice(), f"-env:UserInstallation=file://{profile}", "--headless",
                        "--norestore", "--convert-to", flt, "--outdir", out_dir, xlsx],
                       capture_output=True, text=True, timeout=300)
    stem = os.path.splitext(os.path.basename(xlsx))[0]
    grids = {}
    for path in glob.glob(os.path.join(out_dir, f"{glob.escape(stem)}-*.csv")):
        name = os.path.basename(path)[len(stem) + 1:-4]
        with open(path, newline="", encoding="utf-8") as fh:
            grids[name] = {(r, c): v for r, row in enumerate(csv.reader(fh), 1)
                           for c, v in enumerate(row, 1) if v.strip()}
    return grids


def text_of(sheet: dict, grid: dict, c) -> str:
    """The cell as shown; the stored value if LibreOffice gave nothing for it."""
    v = grid.get((c.row, c.column))
    return v if v is not None else str(c.value)


def add_calculated(sheet: dict, grid: dict) -> int:
    """Cells that only have a value once calculated (a formula saved without its result)
    are invisible to openpyxl; LibreOffice calculated them, so they join the sheet's cells.
    Returns how many were added."""
    ws, have = sheet["ws"], {(c.row, c.column) for c in sheet["cells"]}
    added = 0
    for (r, c) in sorted(grid):
        if (r, c) in have or r in sheet["hidden_rows"] or c in sheet["hidden_cols"]:
            continue
        cell = ws.cell(row=r, column=c)
        sheet["cells"].append(cell)
        sheet["by_row"].setdefault(r, []).append(cell)
        added += 1
    if added:
        sheet["rows"] = sorted(sheet["by_row"])
        for r, cs in sheet["by_row"].items():
            first = min(cs, key=lambda c: c.column)
            sheet["sig"][r] = (len(cs), r in sheet["styled_rows"], isinstance(first.value, str) or first.value is None)
    return added


TOTAL_RE = re.compile(r"^\s*(sub)?\s*totals?\b|^\s*grand total\b|^\s*sum\b", re.I)


# ---------------------------------------------------------------------------
# the structure: model labels, checked against the file
# ---------------------------------------------------------------------------
def plan_sheet(sheet: dict, target: str, base_url: str, use_ai: bool):
    """(regions, cut rows, notes, line for the log)."""
    plan, why = None, "turned off (--no-ai)"
    if use_ai and sheet["cells"]:
        try:
            plan, secs, toks = x2i.ask_model(base_url, DOC_TEMPLATE.format(target=target),
                                             x2i.sheet_facts(sheet) + x2i.outline(sheet))
            why = f"{secs:.1f}s, {toks} tokens"
        except Exception as exc:
            why = (f"the model server at {base_url} could not be reached"
                   if "Max retries" in str(exc) or "ConnectionError" in type(exc).__name__
                   else str(exc).splitlines()[0][:160])
    regions, cuts, notes = x2i.repair_plan(plan or x2i.default_plan(sheet), sheet) if sheet["cells"] else ([], [], [])
    if plan:
        line = (f"AI plan, {len(regions)} region(s) ({why}): "
                + ", ".join(f"{k} R{a}-{b}" for k, a, b, _ in regions[:8]) + (" ..." if len(regions) > 8 else ""))
    else:
        line = f"AI plan not used ({why})"
    return regions, cuts, notes, line, plan is not None


def sections(sheet: dict, grid: dict, regions: list, used_ai: bool):
    """The sheet as an ordered list of items:
        ("table", first_row, last_row, first_col, last_col, header_row or None)
        ("text", row, text, "heading" | "paragraph", cell)"""
    kind_of = {r: k for k, a, b, _ in regions for r in range(a, b + 1)}
    by_row, styled = sheet["by_row"], sheet["styled_rows"]
    tables = []
    for r1, r2, c1, c2 in x2i.table_blocks(sheet):
        # a one-cell row right under a table that the model calls its total joins the table
        # (and, whatever the model said, one that starts with "Total" / "Subtotal")
        while (r2 + 1) in by_row and ((used_ai and kind_of.get(r2 + 1) == "totals") or
                                      bool(TOTAL_RE.match(text_of(sheet, grid, min(by_row[r2 + 1], key=lambda c: c.column))))):
            r2 += 1
        first = sorted(by_row[r1], key=lambda c: c.column)
        nxt = by_row.get(r1 + 1, [])
        header = (kind_of.get(r1) == "header" or r1 in styled
                  or (all(isinstance(c.value, str) for c in first)
                      and any(isinstance(c.value, (int, float)) and not isinstance(c.value, bool) for c in nxt)))
        tables.append((r1, r2, c1, c2, r1 if header else None))
    in_table = {r: t for t in tables for r in range(t[0], t[1] + 1)}
    items, placed = [], set()
    for r in sheet["rows"]:
        if r in in_table:
            t = in_table[r]
            if t not in placed:
                placed.add(t)
                items.append(("table",) + t)
            continue
        cells = sorted(by_row[r], key=lambda c: c.column)
        text = "   ".join(text_of(sheet, grid, c) for c in cells)
        k = kind_of.get(r, "other") if used_ai else "other"
        # a heading is short whatever the model says: a 900-character "title" is a paragraph
        heading = (k in ("title", "header") or r in styled) and k != "notes" and len(text) <= 120
        items.append(("text", r, text, "heading" if heading else "paragraph", cells[0]))
    return items


def table_block(sheet: dict, grid: dict, t) -> TableBlock:
    r1, r2, c1, c2, hr = t[1:] if t[0] == "table" else t
    rows = [[grid.get((r, c), "") for c in range(c1, c2 + 1)]
            for r in range(r1, r2 + 1) if r in sheet["by_row"]]
    header, body = (rows[0], rows[1:]) if hr else ([], rows)
    return TableBlock(kind="table", has_header=bool(hr), n_data_rows=len(body), n_cols=c2 - c1 + 1,
                      header=header, rows=body)


def text_block(kind: str, text: str, cell, title: bool) -> TextBlock:
    al = cell.alignment.horizontal if cell.alignment else None
    size = cell.font.sz if cell.font and cell.font.sz else 11
    return TextBlock(kind=kind, text=text, align="center" if al in ("center", "centerContinuous") else
                     "right" if al == "right" else "left",
                     size="large" if (title or size >= 14) else "small" if size <= 8 else "normal",
                     bold=bool(cell.font and cell.font.b) or kind == "heading")


def page_of(blocks: list) -> Page:
    return Page(analysis=LayoutAnalysis(n_blocks=len(blocks),
                                        table_column_counts=[b.n_cols for b in blocks if isinstance(b, TableBlock)]),
                blocks=blocks)


# ---------------------------------------------------------------------------
# charts: cut out of the sheet as LibreOffice drew it
# ---------------------------------------------------------------------------
def chart_anchors(xlsx: str, part: str) -> list:
    """Each chart on a sheet as {"from": (col, col_off_pt, row, row_off_pt), "to": (...) or None,
    "size": (w_pt, h_pt) or None}; columns and rows 0-based. Excel saves a two-cell anchor
    (two corners), openpyxl a one-cell anchor (a corner and a size)."""
    ns = {"p": "http://schemas.openxmlformats.org/package/2006/relationships",
          "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
          "c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}
    emu_pt = 12700.0
    out = []
    try:
        with zipfile.ZipFile(xlsx) as z:
            names = set(z.namelist())
            folder, _, base = part.rpartition("/")
            rp = f"{folder}/_rels/{base}.rels"
            if rp not in names:
                return []
            for rel in ET.fromstring(z.read(rp)).findall("p:Relationship", ns):
                t = rel.get("Target")
                if "drawing" not in t or "vml" in t:
                    continue
                dpart = t[1:] if t.startswith("/") else os.path.normpath(os.path.join(folder, t)).replace("\\", "/")
                if dpart not in names:
                    continue
                for anc in list(ET.fromstring(z.read(dpart))):
                    if anc.find(".//c:chart", ns) is None and anc.find(".//xdr:pic", ns) is None:
                        continue
                    f, to, ext = anc.find("xdr:from", ns), anc.find("xdr:to", ns), anc.find("xdr:ext", ns)
                    if f is None:
                        continue
                    corner = lambda e: (int(e.find("xdr:col", ns).text), int(e.find("xdr:colOff", ns).text) / emu_pt,
                                        int(e.find("xdr:row", ns).text), int(e.find("xdr:rowOff", ns).text) / emu_pt)
                    out.append({"from": corner(f), "to": corner(to) if to is not None else None,
                                "size": (int(ext.get("cx")) / emu_pt, int(ext.get("cy")) / emu_pt)
                                if ext is not None else None})
    except Exception:
        pass
    return out


def chart_pictures(xlsx: str, sheet: dict, page, n_items: int, items: list, tmp: str) -> list:
    """Each chart as a PNG cut from the page LibreOffice drew, placed in reading order."""
    ws = sheet["ws"]
    dw = float(ws.sheet_format.defaultColWidth or 8.43)
    dh = float(ws.sheet_format.defaultRowHeight or 15)

    def col_x(col: int) -> float:                      # left edge of a 0-based column, in pt, as Calc lays it out
        return sum(float(ws.column_dimensions[x2i._col_letter(i + 1)].width or dw) * 5.25 + 3.75 for i in range(col))

    def row_y(row: int) -> float:                      # top edge of a 0-based row, in pt
        return sum(float(ws.row_dimensions[i + 1].height or dh) for i in range(row))

    # rows drawn taller than their stored height (a large title) shift what lies below:
    # line up on the last row with text above the chart
    pos = x2i.row_positions(sheet, x2i.pdf_lines(page))
    pics = []
    for k, anc in enumerate(chart_anchors(xlsx, sheet["part"])):
        c0, cx0, r0, ry0 = anc["from"]
        x0, y0 = col_x(c0) + cx0, row_y(r0) + ry0
        if anc["to"]:
            c1, cx1, r1, ry1 = anc["to"]
            x1, y1 = col_x(c1) + cx1, row_y(r1) + ry1
        elif anc["size"]:
            x1, y1 = x0 + anc["size"][0], y0 + anc["size"][1]
        else:
            continue
        # rows drawn taller than their stored height (a large title) push everything below down
        above = [r for r in pos if r <= r0]
        dy = (pos[max(above)][0] - row_y(max(above) - 1) - 2) if above else 0.0
        rect = pymupdf.Rect(x0 - 4, y0 + dy - 4, x1 + 4, y1 + dy + 4) & page.rect
        if rect.is_empty or rect.width < 20 or rect.height < 20:
            continue
        path = os.path.join(tmp, f"chart_{sheet['index']:02d}_{k + 1}.png")
        page.get_pixmap(dpi=200, clip=rect, alpha=False).save(path)
        before = sum(1 for it in items if it[1] <= r0 + 1)
        pics.append({"path": path, "width_in": rect.width / 72, "height_in": rect.height / 72,
                     "frac_above": before / n_items if n_items else 1.0, "row": r0 + 1,
                     "x": (rect.x0, rect.x1), "bbox": (rect.y0, rect.y1)})
    return pics


# ---------------------------------------------------------------------------
# the four writers
# ---------------------------------------------------------------------------
def source_words(sheet: dict, grid: dict) -> str:
    return " ".join(text_of(sheet, grid, c) for c in sheet["cells"])


def write_pdf(xlsx_drawn: str, targets: list, page_for: dict, cuts_for: dict, path: str, tmp: str):
    """One PDF: each sheet's page as LibreOffice drew it, a tall one cut between rows."""
    out = pymupdf.open()
    parts_of = {}
    for s in targets:
        page = page_for[s["index"]]
        W, H = page.rect.width, page.rect.height
        lines = x2i.pdf_lines(page)
        cuts = x2i.choose_cuts(H, PDF_MAX_PAGE_PT, cuts_for[s["index"]], x2i.row_positions(s, lines), lines) \
            if H > PDF_MAX_PAGE_PT else []
        bounds = [0.0] + cuts + [H]
        for i in range(len(bounds) - 1):
            clip = pymupdf.Rect(0, bounds[i], W, bounds[i + 1])
            new = out.new_page(width=W, height=clip.height)
            new.show_pdf_page(new.rect, page.parent, page.number, clip=clip)
        parts_of[s["index"]] = len(bounds) - 1
    out.save(path, garbage=3, deflate=True)
    return parts_of


def slides_for(sheet: dict, grid: dict, items: list, pics: list, multi: bool) -> list:
    """[(Page, [pictures])] -- one per slide, in the sheet's own order:
      - a heading starts a slide, and becomes its title
      - a table goes on the heading's slide when only a short intro is there; a long table
        continues on more slides, header repeated, titled "... (continued)"
      - a short note under a small table stays on its slide
      - a chart / picture goes on the slide of the text it follows, or its own slide
      - every slide has a title: the last heading, else the sheet name"""
    slides = []
    state = {"cur": None, "heading": None}

    def heading(text: str) -> TextBlock:
        return TextBlock(kind="heading", text=text, align="left", size="large", bold=True)

    def title_text() -> str:
        return state["heading"].text if state["heading"] else sheet["name"]

    def new(title_block):
        state["cur"] = {"blocks": [title_block], "pics": [], "rows": 0, "table": False}
        slides.append(state["cur"])
        return state["cur"]

    def text_chars(cur) -> int:
        return sum(len(b.text) for b in cur["blocks"][1:] if isinstance(b, TextBlock))

    seq = sorted([(it[1], 0, it) for it in items] + [(p["row"], 1, ("pic", p["row"], p)) for p in pics],
                 key=lambda x: (x[0], x[1]))
    for _, _, it in seq:
        cur = state["cur"]
        if it[0] == "text":
            blk = text_block(it[3], it[2], it[4], it[3] == "heading")
            if it[3] == "heading":
                state["heading"] = blk
                new(blk)
                continue
            if cur is None:
                cur = new(heading(title_text()))
            elif cur["table"] and (cur["rows"] > 8 or len(blk.text) > 300 or text_chars(cur) > 300):
                cur = new(heading(f"{title_text()} (continued)"))
            elif not cur["table"] and len(cur["blocks"]) > SLIDE_PARAGRAPHS:
                cur = new(heading(f"{title_text()} (continued)"))
            cur["blocks"].append(blk)
        elif it[0] == "table":
            tb = table_block(sheet, grid, it)
            chunks = [tb.rows[i:i + SLIDE_ROWS] for i in range(0, len(tb.rows), SLIDE_ROWS)] or [[]]
            for n, rows in enumerate(chunks):
                cur = state["cur"]
                if not (n == 0 and cur and not cur["table"] and not cur["pics"] and text_chars(cur) <= 300):
                    cur = new(heading(title_text() + (" (continued)" if n or cur else "")))
                cur["blocks"].append(TableBlock(kind="table", has_header=tb.has_header, n_data_rows=len(rows),
                                                n_cols=tb.n_cols, header=tb.header, rows=rows))
                cur["table"], cur["rows"] = True, len(rows)
        else:
            if not (cur and not cur["table"] and not cur["pics"] and text_chars(cur) <= 300):
                cur = new(heading(title_text()))
            cur["pics"].append(dict(it[2], after=len(cur["blocks"])))
    out = []
    for c in slides:
        n = len(c["blocks"])
        # the picture keeps its place among the slide's blocks (text after it stays after it)
        out.append((page_of(c["blocks"]), [dict(p, frac_above=p.pop("after") / n) for p in c["pics"]]))
    return out


def write_csvs(sheet: dict, grid: dict, items: list, scope: str, folder: str) -> list:
    """[(file name, rows written)] -- one CSV per table, or one for the whole sheet."""
    os.makedirs(folder, exist_ok=True)
    base = f"sheet_{sheet['index']:02d}_{x2i.safe_name(sheet['name'])}"
    tables = [it for it in items if it[0] == "table"]
    jobs = []
    if scope == "tables" and tables:
        for n, t in enumerate(tables, 1):
            tb = table_block(sheet, grid, t)
            jobs.append((f"{base}_table_{n}.csv" if len(tables) > 1 else f"{base}.csv",
                         ([tb.header] if tb.has_header else []) + tb.rows))
    else:
        cols = sorted({c.column for c in sheet["cells"]})
        c1, c2 = cols[0], cols[-1]
        rows = [[grid.get((r, c), "") for c in range(c1, c2 + 1) if c not in sheet["hidden_cols"]]
                for r in sheet["rows"]]
        jobs.append((f"{base}.csv", rows))
    for name, rows in jobs:
        with open(os.path.join(folder, name), "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerows(rows)
    return jobs


def check_csvs(folder: str, jobs: list) -> list:
    """Read every CSV back and compare it cell by cell with what was meant to be written."""
    problems = []
    for name, rows in jobs:
        with open(os.path.join(folder, name), newline="", encoding="utf-8-sig") as fh:
            back = list(csv.reader(fh))
        if back != [[str(v) for v in r] for r in rows]:
            bad = sum(1 for a, b in zip(back, rows) if a != b) + abs(len(back) - len(rows))
            problems.append(f"{name}: {bad} row(s) read back differ from the sheet")
    return problems


# ---------------------------------------------------------------------------
def run(xlsx: str, to: str, out_dir: str, sheets_spec: str, csv_scope: str, base_url: str, use_ai: bool) -> bool:
    name = os.path.splitext(os.path.basename(xlsx))[0]
    print("=" * 72)
    print(xlsx)
    print("=" * 72)
    started = time.time()
    try:
        sheets = x2i.read_sheets(xlsx)
    except Exception as exc:
        print(f"  COULD NOT OPEN: {exc}")
        return False
    visible = [s for s in sheets if not s["hidden"]]
    wanted = set(x2i.parse_sheets_arg(sheets_spec, len(sheets)))
    targets = [s for s in visible if s["index"] in wanted and not s["empty"]]
    if not targets:
        print(f"  SKIPPED: no sheet to convert (sheets: {len(sheets)}, selected by --sheets {sheets_spec!r})")
        return False
    if not x2i.have_calc():
        print("  CONVERSION FAILED: LibreOffice Calc is not installed. Install it with:\n"
              "      sudo apt-get install -y libreoffice-calc")
        return False
    os.makedirs(out_dir, exist_ok=True)
    print(f"  {len(sheets)} sheet(s), converting {len(targets)} as {to.upper()} -> {out_dir}/")

    with tempfile.TemporaryDirectory() as tmp:
        # 1. values as shown, and the drawing (needed for the PDF, and for charts)
        try:
            grids = shown_values(os.path.abspath(xlsx), tmp)
            borders = {s["part"]: x2i.table_blocks(s) for s in targets if s["no_borders"] and s["cells"] and s["part"]}
            borders = {k: v for k, v in borders.items() if v}
            need_drawing = to == "pdf" or any(s["charts"] or s["images"] for s in targets)
            doc, page_for = None, {}
            if need_drawing:
                src = x2i.bordered_copy(os.path.abspath(xlsx), borders, tmp) if (borders and to == "pdf") \
                    else os.path.abspath(xlsx)
                doc = pymupdf.open(x2i.to_pdf(src, tmp))
                for group in (sheets, visible, [s for s in visible if not s["empty"]]):
                    if doc.page_count == len(group):
                        page_for = {s["index"]: doc[i] for i, s in enumerate(group)}
                        break
                else:
                    raise RuntimeError(f"the PDF has {doc.page_count} page(s) for {len(sheets)} sheet(s)")
        except Exception as exc:
            print(f"  CONVERSION FAILED: {exc}")
            return False

        # 2. per sheet: the model's labels, checked, then the structure
        built, problems, all_text, n_ai = [], [], [], 0
        for s in targets:
            grid = grids.get(s["name"], {})
            if grid:
                n = add_calculated(s, grid)
                if n:
                    print(f"  sheet {s['index']} '{s['name']}': {n} calculated cell(s) taken from LibreOffice "
                          f"(formulas saved without their result)")
            if s["cells"] and not grid:
                print(f"  ~ sheet {s['index']} '{s['name']}': no displayed values from LibreOffice; stored values used")
            regions, cuts, notes, line, used_ai = plan_sheet(s, {"pdf": "PDF", "docx": "Word document",
                                                                  "pptx": "slide deck", "csv": "CSV"}[to],
                                                             base_url, use_ai)
            print(f"  sheet {s['index']} '{s['name']}': {line}")
            for n in notes:
                print(f"       plan repaired: {n}")
            items = sections(s, grid, regions, used_ai)
            n_draw = s["charts"] + s["images"]
            pics = chart_pictures(xlsx, s, page_for[s["index"]], len(items), items, tmp) \
                if n_draw and s["index"] in page_for and to in ("docx", "pptx") else []
            n_tab = sum(1 for it in items if it[0] == "table")
            n_txt = len(items) - n_tab
            print(f"  sheet {s['index']} '{s['name']}': {n_tab} table(s), {n_txt} text block(s)"
                  + (f", {len(pics)}/{n_draw} chart(s) / picture(s) placed" if n_draw and to in ("docx", "pptx") else "")
                  + (f", {s['charts']} chart(s) drawn" if s["charts"] and to == "pdf" else ""))
            built.append((s, grid, items, pics, cuts))
            n_ai += used_ai
            all_text.append(source_words(s, grid))

        # 3. write and read back
        source = " ".join(all_text)
        multi = len(targets) > 1
        out_files = []
        try:
            if to == "pdf":
                path = os.path.join(out_dir, f"{name}.pdf")
                parts = write_pdf(xlsx, targets, page_for, {s["index"]: c for s, _, _, _, c in built}, path, tmp)
                back = pymupdf.open(path)
                words = set(x2i.words_of(" ".join(p.get_text() for p in back)))
                for s, grid, _, _, _ in built:
                    cells = [text_of(s, grid, c) for c in s["cells"] if isinstance(c.value, str) and x2i.words_of(str(c.value))]
                    ok = sum(1 for t in cells if all(w in words for w in x2i.words_of(t)))
                    if cells and ok / len(cells) < MIN_COVERAGE:
                        problems.append(f"sheet {s['index']}: only {ok}/{len(cells)} text cells are in the PDF")
                    print(f"       sheet {s['index']}: {parts[s['index']]} page(s), {ok}/{len(cells)} text cells in the PDF")
                if back.page_count != sum(parts.values()):
                    problems.append(f"the PDF has {back.page_count} pages, expected {sum(parts.values())}")
                out_files.append(path)
            elif to == "docx":
                from to_docx import build_docx, verify_docx
                pages, images = [], []
                for s, grid, items, pics, _ in built:
                    blocks = ([TextBlock(kind="heading", text=s["name"], align="left", size="large", bold=True)]
                              if multi else [])
                    for it in items:
                        blocks.append(table_block(s, grid, it) if it[0] == "table"
                                      else text_block(it[3], it[2], it[4], it is items[0] and it[3] == "heading"))
                    pages.append(page_of(blocks))
                    images.append(pics)
                path = os.path.join(out_dir, f"{name}.docx")
                problems += build_docx(pages, path, image_sets=images) or []
                cov, missing = verify_docx(path, source)
                print(f"  word coverage:     {cov:.1%} of the sheets' words are in the document"
                      + (f" (missing e.g. {', '.join(sorted(missing)[:6])})" if missing else ""))
                if cov < MIN_COVERAGE:
                    problems.append(f"word coverage {cov:.1%} is below {MIN_COVERAGE:.0%}")
                out_files.append(path)
            elif to == "pptx":
                from to_pptx import build_pptx, verify_pptx
                slides = []
                for s, grid, items, pics, _ in built:
                    slides += slides_for(s, grid, items, pics, multi)
                path = os.path.join(out_dir, f"{name}.pptx")
                problems += build_pptx([p for p, _ in slides], path, image_sets=[i for _, i in slides]) or []
                cov, missing = verify_pptx(path, source)
                print(f"  slides:            {len(slides)}")
                print(f"  word coverage:     {cov:.1%} of the sheets' words are in the deck"
                      + (f" (missing e.g. {', '.join(sorted(missing)[:6])})" if missing else ""))
                if cov < MIN_COVERAGE:
                    problems.append(f"word coverage {cov:.1%} is below {MIN_COVERAGE:.0%}")
                out_files.append(path)
            else:
                folder = os.path.join(out_dir, name)
                for s, grid, items, _, _ in built:
                    jobs = write_csvs(s, grid, items, csv_scope, folder)
                    bad = check_csvs(folder, jobs)
                    problems += bad
                    left_out = sum(1 for it in items if it[0] == "text") if csv_scope == "tables" and \
                        any(it[0] == "table" for it in items) else 0
                    print(f"       sheet {s['index']}: " + ", ".join(f"{n} ({len(r)} rows)" for n, r in jobs)
                          + ("  [read back: identical]" if not bad else "  [read back: DIFFERS]")
                          + (f"; {left_out} title/note row(s) outside the tables not in the CSV" if left_out else ""))
                    out_files += [os.path.join(folder, n) for n, _ in jobs]
        except Exception as exc:
            print(f"  COULD NOT WRITE: {exc}")
            return False

    for p in problems:
        print(f"  ! {p}")
    ok = not problems and all(os.path.exists(f) for f in out_files)
    print()
    print("  --- verification (read back from the written file) ---")
    print(f"  sheets converted:  {len(built)}/{len(targets)}")
    print(f"  files written:     {len(out_files)}  ({', '.join(os.path.basename(f) for f in out_files[:6])}"
          + (" ..." if len(out_files) > 6 else "") + ")")
    print(f"  AI plan:           {n_ai}/{len(built)} sheet(s) labelled by the model")
    print(f"  {time.time() - started:.1f}s")
    print(f"  VERDICT: {'PASS -- converted and verified' if ok else 'FAIL -- see the problems above'}")
    print()
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", help="Excel file(s) (.xlsx / .xlsm), globs allowed")
    parser.add_argument("--to", choices=TARGETS, required=True, help="pdf, docx, pptx or csv")
    parser.add_argument("--outdir", default=None, help="where the output goes (default excel_to_<kind>_output)")
    parser.add_argument("--sheets", default="all", help="e.g. 1, 1-3, 2,4 by position (default: all)")
    parser.add_argument("--csv-scope", choices=("tables", "sheet"), default="tables",
                        help="csv: one file per table (default) or one per whole sheet")
    parser.add_argument("--base-url", default=x2i.DEFAULT_BASE_URL, help="the model server (default the local 2B)")
    parser.add_argument("--no-ai", action="store_true", help="skip the model; label the structure with plain rules")
    args = parser.parse_args()
    paths = []
    for pattern in args.files:
        paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern])
    ok = fail = 0
    for path in paths:
        if not os.path.exists(path):
            print(f"skipping {path}: not found")
            continue
        if run(path, args.to, args.outdir or OUT_DIRS[args.to], args.sheets, args.csv_scope,
               args.base_url, not args.no_ai):
            ok += 1
        else:
            fail += 1
    if len(paths) > 1:
        print("=" * 72)
        print(f"{ok} succeeded, {fail} failed, {len(paths)} total")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
