"""
excel_to_image.py -- convert an Excel workbook (.xlsx) into PNG / JPG / JPEG pictures,
one picture per sheet (a sheet too large for one picture is split into parts).

THE PICTURE IS DRAWN FROM THE REAL FILE, NEVER FROM THE MODEL. LibreOffice Calc
opens the workbook and draws it (cell values, number formats, fonts, fills, borders,
merged cells, conditional formatting, charts) into a PDF with one page per sheet
(its "SinglePageSheets" export). PyMuPDF then turns each page into the picture.
Nothing the model writes ever reaches a cell.

WHAT THE MODEL (the Qwen 2B) DOES. It PLANS and CHECKS, it does not draw:

  1. The workbook is read with openpyxl: values, merged ranges, hidden rows and
     columns, bold and filled rows, charts, conditional formats. A compact outline
     of each sheet (a few words per row, flagged bold / filled / merged) goes to
     the model together with TEMPLATE below.
  2. The model answers with a small JSON plan (a pydantic schema, so it cannot
     answer in any other shape): the REGIONS of the sheet (title, header, table,
     totals, notes, chart), where a large sheet may safely be CUT between images,
     and layout WARNINGS (a very wide sheet, a table that continues, ...).
  3. The plan is VALIDATED against the file. Rows that do not exist are dropped,
     rows no region covers are added as an "other" region, a cut is refused inside
     a merged range or inside a table. The model cannot make the picture lose a
     row, because the picture is drawn from the whole sheet either way.
  4. A sheet that fits one picture is one picture. A larger one is split (default)
     or scaled down (--large scale). Cuts go where the plan says a region ends, and
     only between lines of text, so no line is ever cut in half.

If the model server cannot be reached or answers badly, the conversion still
runs: the plan falls back to plain rules (--no-ai does the same on purpose) and the
report says "AI plan: not used".

HOW IT PROVES NOTHING WAS LOST. Each picture is opened again from disk and checked:

  1. FORMAT       the file is really a PNG / JPEG of the size that was drawn.
  2. NOT BLANK    a sheet with content did not come out white.
  3. EVERY CELL   every word of every visible text cell is in the PDF page the
                  picture was cut from (the PDF carries the real text of the page).
  4. NO CUT LINE  no line of text lies across the edge between two parts.
  5. COUNT        one sheet, one picture (or its parts); none missing.

Advisory only: cells holding formulas that had no stored result (a file written by a
script) are calculated by LibreOffice and cannot be compared with a stored value;
the number of charts found in the file is reported, and the chart is drawn by
LibreOffice, not independently checked.

RESOLUTION, per format. PNG is lossless, so it defaults to 200 dpi (sharp text; the
file is bigger). JPG / JPEG default to 150 dpi at quality 90 (smaller; text edges
soften a little). --dpi and --quality override those. A picture wider than
MAX_SIDE_PX is drawn at a lower dpi instead.

Output layout, the same as pdf_to_png.py: every workbook gets its own subfolder:

    input/excel_to_image_input/report.xlsx  ->  output/excel_to_image_output/report/sheet_01_Sales.png
                                                                       sheet_02_Costs_part_01.png
                                                                       sheet_02_Costs_part_02.png

Needs LibreOffice Calc:  sudo apt-get install -y libreoffice-calc
Usage:
    .venv/bin/python scripts/excel_to_image.py input/excel_to_image_input/report.xlsx
    .venv/bin/python scripts/excel_to_image.py report.xlsx --format jpg --quality 85
    .venv/bin/python scripts/excel_to_image.py report.xlsx --sheets 1,3 --large scale --no-ai
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import List, Literal
from xml.etree import ElementTree as ET

import pymupdf
from openpyxl import load_workbook
from PIL import Image
from pydantic import BaseModel, Field

EXCEL_DIR = "input/excel_to_image_input"
IMG_OUT_DIR = "output/excel_to_image_output"

# The 2B served by llama-server on this machine. The model reads text only here (an
# outline of the sheet), so no mmproj is needed. IDOX_BASE_URL or --base-url override.
DEFAULT_BASE_URL = os.environ.get("IDOX_BASE_URL", "http://127.0.0.1:8090")

FORMATS = {"png": {"dpi": 200}, "jpg": {"dpi": 150, "quality": 90}, "jpeg": {"dpi": 150, "quality": 90}}
MAX_SIDE_PX = 5000        # a picture wider than this is drawn at a lower dpi
MAX_PART_PX = 4000        # the tallest single picture; a taller sheet is split or scaled
MIN_DPI = 40              # --large scale never goes below this; it splits instead
INK_CONTRAST = 40         # brightest minus darkest grey level that counts as "has content"
CELL_COVERAGE = 0.97      # share of text cells whose words must all be on the PDF page
MAX_AI_SHEETS = 12        # sheets beyond this are planned by the plain rules
OUTLINE_ROWS = (50, 15)   # rows shown to the model: the first 50 and the last 15
OUTLINE_COLS = 10
CELL_CHARS = 30


# ---------------------------------------------------------------------------
# the plan the model must answer with
# ---------------------------------------------------------------------------
class Region(BaseModel):
    kind: Literal["title", "header", "table", "totals", "notes", "chart", "other"] = Field(
        description="What this block of rows is.")
    first_row: int = Field(description="First sheet row of the region, from 1.")
    last_row: int = Field(description="Last sheet row of the region.")
    summary: str = Field(description="At most 12 words on what it holds. No cell values copied in bulk.")


class SheetPlan(BaseModel):
    regions: List[Region] = Field(max_length=14, description="Every block of rows, top to bottom, no gaps.")
    split_after_rows: List[int] = Field(
        max_length=10,
        description="Rows after which a NEW picture may start. Only rows where a region ends. "
                    "[] when the sheet fits one picture.")
    warnings: List[str] = Field(max_length=5, description="Layout risks, one short sentence each; [] if none.")


# The template the model is given. Two parts: the rules that keep the structure, and
# the formatting configuration of THIS run (filled in by build_prompt()).
TEMPLATE = """You plan how ONE spreadsheet sheet is drawn as a picture. A program draws the
picture from the real file; you never draw it and you never write cell values. You
read an outline of the sheet and answer with a JSON plan.

STRUCTURE-PRESERVATION RULES
1. Every row that has content belongs to exactly one region. Regions are in
   top-to-bottom order and leave no gap that holds content.
2. Region kinds: title (a heading above the data), header (the column labels), table
   (the data rows), totals (a total / subtotal row), notes (text below the data),
   chart (rows a chart sits over), other.
3. A table stays whole: its header and its data rows are never separated, and a
   total row stays with the table it totals.
4. A merged range (listed in the outline) is one block. Never split it.
5. Rows marked [bold] or [fill] are usually a title, header or totals row.
6. Use only row numbers that appear in the outline or in the used range.
7. Never invent rows, never copy cell values into the summary beyond a few words.

SPLITTING
- If the whole sheet fits one picture, split_after_rows is [].
- Otherwise list the rows where a picture may END: the last row of a region, or a
  blank row between regions. Never a row inside a table or inside a merged range.
- A picture can hold about {rows_per_image} rows of this sheet.

FORMATTING CONFIGURATION (for your information; you do not change it)
- output: {fmt}, {dpi} dpi, drawn by LibreOffice Calc with the file's own fonts,
  fills, borders, number formats, conditional formatting and charts
- the facts of the sheet (name, used range, charts, merged ranges, hidden rows) come
  with the outline; trust them, they were read from the file

WARNINGS: add one only for a real risk, for example "very wide: 40 columns",
"chart overlaps the data", "table continues past the last listed row"."""


def build_prompt(fmt: str, dpi: int, rows_per_image: int) -> str:
    """The system prompt. Only run settings are in it, so it is the same for every sheet and
    the server keeps it cached instead of reading it again."""
    return TEMPLATE.format(rows_per_image=rows_per_image, fmt=fmt.upper(), dpi=dpi)


# ---------------------------------------------------------------------------
# reading the workbook
# ---------------------------------------------------------------------------
def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def charts_per_sheet(path: str):
    """({sheet name: number of charts}, {sheet name: its XML part}), read from the package
    itself. openpyxl drops charts when it loads a file, so it cannot tell us."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
          "p": "http://schemas.openxmlformats.org/package/2006/relationships"}
    out, parts = {}, {}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())

            def rels(part):
                folder, _, base = part.rpartition("/")
                rp = f"{folder}/_rels/{base}.rels"
                if rp not in names:
                    return {}
                root = ET.fromstring(z.read(rp))
                return {e.get("Id"): e.get("Target") for e in root.findall("p:Relationship", ns)}

            def resolve(base_part, target):
                if target.startswith("/"):
                    return target.lstrip("/")
                parts = base_part.rpartition("/")[0].split("/")
                for seg in target.split("/"):
                    if seg == "..":
                        parts.pop()
                    elif seg != ".":
                        parts.append(seg)
                return "/".join(parts)

            wb = ET.fromstring(z.read("xl/workbook.xml"))
            wb_rels = rels("xl/workbook.xml")
            for sh in wb.findall("m:sheets/m:sheet", ns):
                part = resolve("xl/workbook.xml", wb_rels.get(sh.get(f"{{{ns['r']}}}id"), ""))
                n = 0
                if part in names:
                    for target in rels(part).values():
                        if "drawing" in target and "vml" not in target:
                            dpart = resolve(part, target)
                            n += sum(1 for t in rels(dpart).values() if "/charts/" in t or "chart" in t.split("/")[-1])
                out[sh.get("name")] = n
                parts[sh.get("name")] = part
    except Exception:
        pass
    return out, parts


def read_sheets(path: str) -> list:
    """One dict per sheet, with the cells that are drawn (visible, non-empty)."""
    wb = load_workbook(path, data_only=True)         # the values Excel stored
    wf = load_workbook(path)                         # the formulas
    charts, parts = charts_per_sheet(path)
    sheets = []
    for idx, ws in enumerate(wb.worksheets, 1):
        hidden_rows = {r for r, d in ws.row_dimensions.items() if d.hidden}
        hidden_cols = set()
        for d in ws.column_dimensions.values():
            if d.hidden and d.min and d.max:
                hidden_cols.update(range(d.min, d.max + 1))
        cells, unevaluated = [], 0
        fws = wf[ws.title]
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if v is None or (isinstance(v, str) and not v.strip()):
                    if v is None and isinstance(fws[c.coordinate].value, str) \
                            and str(fws[c.coordinate].value).startswith("="):
                        unevaluated += 1
                    continue
                if c.row in hidden_rows or c.column in hidden_cols:
                    continue
                cells.append(c)
        rows = sorted({c.row for c in cells})
        firsts = {}
        for c in sorted(cells, key=lambda c: (c.row, c.column)):
            firsts.setdefault(c.row, c)
        styled_rows = {r for r, c in firsts.items()
                       if (c.font and c.font.b) or (c.fill and c.fill.fill_type == "solid")}
        by_row = {}
        for c in cells:
            by_row.setdefault(c.row, []).append(c)
        # what a row looks like: how many cells, bold / filled first cell, text or number first
        sig = {r: (len(cs), r in styled_rows, isinstance(firsts[r].value, str)) for r, cs in by_row.items()}
        try:
            cond = sum(len(r.rules) for r in ws.conditional_formatting)
        except Exception:
            cond = 0
        merged = [str(m) for m in ws.merged_cells.ranges]
        n_charts = charts.get(ws.title, 0)
        sheets.append({
            "index": idx, "name": ws.title, "hidden": ws.sheet_state != "visible", "ws": ws,
            "cells": cells, "rows": rows, "styled_rows": styled_rows, "by_row": by_row, "sig": sig, "unevaluated": unevaluated, "merged": merged,
            "merged_rows": [(m.min_row, m.max_row) for m in ws.merged_cells.ranges if m.max_row > m.min_row],
            "hidden_rows": hidden_rows, "hidden_cols": hidden_cols, "charts": n_charts, "cond": cond,
            "used": (f"{_col_letter(min(c.column for c in cells))}{rows[0]}:"
                     f"{_col_letter(max(c.column for c in cells))}{rows[-1]}") if cells else "empty",
            # a sheet with only a chart has no cells but still draws a picture
            "empty": not cells and n_charts == 0,
            "part": parts.get(ws.title),
            "images": len(getattr(ws, "_images", [])),
            # a sheet with no borders anywhere prints as bare text; it gets the cell grid instead
            "no_borders": not any(c.border and any(getattr(c.border, side).style
                                                   for side in ("left", "right", "top", "bottom"))
                                  for c in cells),
        })
    return sheets


def outline(sheet: dict) -> str:
    """The WHOLE sheet in a few dozen lines for the model. Rows that look alike (the same
    number of cells, the same bold / fill flags, no blank row between) are one run: the
    first two and the last are shown, the rest are counted. Every row that differs
    (a title, a header, a total, a blank gap) is shown, wherever it is on the sheet."""
    by_row, sig = sheet["by_row"], sheet["sig"]
    rows = sorted(by_row)

    def first_of(r):
        return sorted(by_row[r], key=lambda c: c.column)[0]

    def flags_of(r):
        f = first_of(r)
        return [n for n, on in (("bold", f.font and f.font.b), ("fill", f.fill and f.fill.fill_type == "solid")) if on]

    def line(r):
        cs = sorted(by_row[r], key=lambda c: c.column)
        fl = flags_of(r)
        text = " | ".join(str(c.value).replace("\n", " ")[:CELL_CHARS] for c in cs[:OUTLINE_COLS])
        more = f" (+{len(cs) - OUTLINE_COLS} more)" if len(cs) > OUTLINE_COLS else ""
        return f"R{r}: {len(cs)} cell(s) {'[' + ','.join(fl) + '] ' if fl else ''}{text}{more}"

    lines, i = [], 0
    while i < len(rows):
        r = rows[i]
        if i and r != rows[i - 1] + 1:
            lines.append(f"(blank rows R{rows[i - 1] + 1}-R{r - 1})")
        j = i
        while j + 1 < len(rows) and rows[j + 1] == rows[j] + 1 and sig[rows[j + 1]] == sig[r]:
            j += 1
        run = rows[i:j + 1]
        if len(run) <= 3:
            lines += [line(x) for x in run]
        else:
            lines += [line(run[0]), line(run[1]),
                      f"... {len(run) - 3} more rows like these, R{run[2]}-R{run[-2]} ...", line(run[-1])]
        i = j + 1
    cap = OUTLINE_ROWS[0] + OUTLINE_ROWS[1]
    if len(lines) > cap:
        lines = lines[:OUTLINE_ROWS[0]] + [f"... ({len(lines) - cap} outline lines not shown) ..."] + lines[-OUTLINE_ROWS[1]:]
    return "\n".join(lines)


def sheet_facts(sheet: dict) -> str:
    """What the program knows about the sheet for certain, sent with the outline."""
    return (f'SHEET "{sheet["name"]}": used range {sheet["used"]}, {len(sheet["rows"])} row(s) with content, '
            f'{sheet["charts"]} chart(s), {sheet["cond"]} conditional-format rule(s), '
            f'{len(sheet["merged"])} merged range(s)'
            + (": " + ", ".join(sheet["merged"][:20]) if sheet["merged"] else "")
            + f', {len(sheet["hidden_rows"])} hidden row(s), {len(sheet["hidden_cols"])} hidden column(s).\n'
              f"OUTLINE (R<row>: cells, [flags] first cells):\n")


# ---------------------------------------------------------------------------
# the plan: ask the model, validate, repair
# ---------------------------------------------------------------------------
def ask_model(base_url: str, system: str, user: str):
    """The plan from the model, plus how long it took. Raises if it cannot be had."""
    import requests

    api_key = os.environ.get("IDOX_API_KEY", "").strip()
    thinking = os.environ.get("IDOX_THINKING", "").strip().lower()
    timeout = int(os.environ.get("IDOX_TIMEOUT", "600") or 600)
    body = {
        "model": os.environ.get("IDOX_MODEL", "manual") or "manual",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "sheet_plan", "schema": SheetPlan.model_json_schema()}},
        "temperature": 0, "repeat_penalty": 1.1, "n_predict": 900,
        **({"chat_template_kwargs": {"enable_thinking": thinking == "on"}} if thinking in ("on", "off") else {}),
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    t0 = time.time()
    resp = requests.post(f"{base_url.rstrip('/')}/v1/chat/completions", headers=headers, json=body, timeout=timeout)
    if resp.status_code == 401:
        raise RuntimeError("the server rejected the API key (401)")
    if not resp.ok:
        raise RuntimeError(f"the server answered {resp.status_code}: {resp.text[:160].strip()}")
    d = resp.json()
    if d["choices"][0].get("finish_reason") == "length":
        raise ValueError("the plan was cut off at the token limit")
    content = d["choices"][0]["message"].get("content") or ""
    if not content.strip():
        raise RuntimeError("the model returned no text")
    return SheetPlan.model_validate_json(content), time.time() - t0, (d.get("usage") or {}).get("completion_tokens", 0)


def blank_runs(rows: list) -> list:
    """Contiguous runs of content rows, split where a blank row (or more) lies between."""
    runs, start, prev = [], None, None
    for r in rows:
        if start is None:
            start = prev = r
        elif r == prev + 1:
            prev = r
        else:
            runs.append((start, prev))
            start = prev = r
    if start is not None:
        runs.append((start, prev))
    return runs


def default_plan(sheet: dict) -> SheetPlan:
    """No model: one region per run of rows, cuts allowed wherever a run ends."""
    runs = blank_runs(sheet["rows"])
    return SheetPlan(regions=[Region(kind="other", first_row=a, last_row=b, summary="rows with content")
                              for a, b in runs[:14]], split_after_rows=[b for _, b in runs[:-1]][:10], warnings=[])


def checked_warning(text: str, sheet: dict) -> bool:
    """Keep a model warning unless the file contradicts it. A 2B invents warnings (a chart on a
    sheet with none, "40 columns" on a 5-column sheet); those it claims as numbers are compared."""
    low = text.lower()
    if "chart" in low and not sheet["charts"]:
        return False
    cols = len({c.column for c in sheet["cells"]})
    for n in re.findall(r"(\d+)\s+columns?", low):
        if abs(int(n) - cols) > max(1, cols // 5):
            return False
    return True


def repair_plan(plan: SheetPlan, sheet: dict):
    """The plan made consistent with the file. Returns (regions, split rows, notes).
    Whatever the model claims is checked against what was read from the file."""
    notes, rows = [], sheet["rows"]
    lo, hi = (rows[0], rows[-1]) if rows else (1, 1)
    regions = []
    for rg in plan.regions:
        a, b = max(rg.first_row, lo), min(rg.last_row, hi)
        if a > b:
            notes.append(f"dropped region rows {rg.first_row}-{rg.last_row} (outside the sheet's content)")
            continue
        kind = rg.kind
        if kind == "chart" and not sheet["charts"]:
            kind = "other"
            notes.append(f"region rows {a}-{b} was called a chart, but the sheet has no chart")
        regions.append((kind, a, b, rg.summary))
    # one region per row: an overlap is cut back to where the next region starts
    regions.sort(key=lambda x: (x[1], x[2]))
    fixed = []
    for rg in regions:
        if fixed and rg[1] <= fixed[-1][2]:
            prev = fixed[-1]
            if prev[1] == rg[1]:
                notes.append(f"dropped region rows {rg[1]}-{rg[2]} (it overlapped the one at rows {prev[1]}-{prev[2]})")
                continue
            fixed[-1] = (prev[0], prev[1], rg[1] - 1, prev[3])
            notes.append(f"region rows {prev[1]}-{prev[2]} overlapped the next; cut back to row {rg[1] - 1}")
        fixed.append(rg)
    regions = fixed
    covered = {r for _, a, b, _ in regions for r in range(a, b + 1)}
    missing = [r for r in rows if r not in covered]
    if missing:
        for a, b in blank_runs(missing):
            regions.append(("other", a, b, "rows the plan did not cover"))
        notes.append(f"added {len(blank_runs(missing))} region(s) for {len(missing)} row(s) the plan missed")
    regions.sort(key=lambda x: x[1])

    inside_table = set()
    for kind, a, b, _ in regions:
        if kind in ("table", "header", "totals") and b > a:
            inside_table.update(range(a, b))             # a cut after row r would split it
    in_merge = {r for a, b in sheet["merged_rows"] for r in range(a, b)}
    rowset = set(rows)
    # never separate a bold / filled row (a title or header) from the row right under it
    after_header = {r for r in sheet["styled_rows"] if r + 1 in rowset}
    # the model counts rows badly inside a long run; it may only pick a place where the file's
    # own structure changes: a blank row follows, or the next row is a different kind of row
    inside_run = {r for r in rows if r + 1 in rowset and sheet["sig"][r] == sheet["sig"][r + 1]}
    bad = inside_table | in_merge | after_header | inside_run
    ok, refused = [], 0
    for r in plan.split_after_rows:
        if r < lo or r >= hi or r in bad:
            refused += 1
        else:
            ok.append(r)
    if refused:
        notes.append(f"refused {refused} cut(s): outside the sheet, inside a table, run of rows or merged range, or right after a header")
    # the end of every region is a safe place to cut too, even if the plan did not list it
    ends = {b for _, _, b, _ in regions if b < hi and b not in bad}
    return regions, sorted(set(ok) | ends), notes


# ---------------------------------------------------------------------------
# LibreOffice and the PDF
# ---------------------------------------------------------------------------
def find_soffice() -> str:
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    raise SystemExit("LibreOffice not found. Install it with:\n    sudo apt-get install -y libreoffice-calc")


def have_calc() -> bool:
    try:
        real = os.path.dirname(os.path.realpath(find_soffice()))
    except SystemExit:
        return False
    return any(os.path.exists(os.path.join(real, n)) for n in ("libsclo.so", "libsc.so"))


def to_pdf(xlsx: str, out_dir: str, timeout: int = 300) -> str:
    """The workbook as a PDF with ONE PAGE PER SHEET, sized to the sheet's content.
    Runs against a throwaway profile, so an open LibreOffice window cannot swallow it."""
    soffice = find_soffice()
    os.makedirs(out_dir, exist_ok=True)
    flt = 'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}'
    with tempfile.TemporaryDirectory() as profile:
        proc = subprocess.run(
            [soffice, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
             "--convert-to", flt, "--outdir", out_dir, xlsx],
            capture_output=True, text=True, timeout=timeout)
    pdf = os.path.join(out_dir, os.path.splitext(os.path.basename(xlsx))[0] + ".pdf")
    if not os.path.exists(pdf):
        why = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError("LibreOffice wrote no PDF: " + (why[-1] if why else f"exit {proc.returncode}"))
    return pdf


def pdf_lines(page) -> list:
    """[(text, (x0, y0, x1, y1))] for every line of text on the page, top to bottom."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if t:
                out.append((t, tuple(ln["bbox"])))
    out.sort(key=lambda x: (round(x[1][1], 1), x[1][0]))
    return out


def _key(s: str) -> str:
    return re.sub(r"\W+", "", s.lower())


def row_positions(sheet: dict, lines: list) -> dict:
    """{row: (y0, y1)} for the rows whose first text cell can be found, in order, on the
    page. The PDF does not say which row a line belongs to; the cell text does."""
    firsts = []
    seen = set()
    for c in sorted(sheet["cells"], key=lambda c: (c.row, c.column)):
        if c.row not in seen and isinstance(c.value, str) and _key(c.value):
            seen.add(c.row)
            firsts.append((c.row, _key(c.value)[:24]))
    pos, i = {}, 0
    for row, key in firsts:
        for j in range(i, len(lines)):
            if key in _key(lines[j][0]):
                pos[row] = (lines[j][1][1], lines[j][1][3])
                i = j + 1
                break
    return pos


def ink(b) -> tuple:
    """The part of a line's box that holds its letters. LibreOffice's line boxes include the
    font's full ascent and descent, so in a tightly packed table neighbouring rows' boxes
    overlap and no y touches none of them; the letters themselves sit in the middle."""
    h = b[3] - b[1]
    return b[1] + 0.2 * h, b[3] - 0.2 * h


def safe_cut(y: float, lines: list) -> bool:
    """A cut at y crosses no letters of any line of text."""
    return not any(ink(b)[0] < y < ink(b)[1] for _, b in lines)


def choose_cuts(page_h: float, max_part: float, split_rows: list, pos: dict, lines: list) -> list:
    """y values (pt) at which to cut a tall page: at the end of a planned region when one
    falls in range, else at the nearest gap between lines, else (no gap) just at the limit."""
    cands = []
    anchored = sorted(pos)
    for r in split_rows:
        above = [a for a in anchored if a <= r]
        if above:
            y = pos[above[-1]][1] + 1
            if safe_cut(y, lines):
                cands.append(y)
    gaps = sorted({round(y, 2) for _, b in lines for y in (ink(b)[1] + 0.3, ink(b)[0] - 0.3) if safe_cut(y, lines)})
    cuts, start = [], 0.0
    while page_h - start > max_part:
        limit = start + max_part
        window = [y for y in cands if start + 0.3 * max_part < y <= limit]
        if not window:
            window = [y for y in gaps if start + 0.3 * max_part < y <= limit]
        cut = max(window) if window else limit
        cuts.append(cut)
        start = cut
    return cuts


def table_blocks(sheet: dict) -> list:
    """(first_row, last_row, first_col, last_col) of each table on a sheet: two or more
    consecutive rows that each hold at least two cells. A one-cell row (a title, a
    sentence) is never part of one."""
    rows = [r for r in sheet["rows"] if len(sheet["by_row"][r]) >= 2]
    blocks, run = [], []
    for r in rows + [None]:
        if run and (r is None or r != run[-1] + 1):
            if len(run) >= 2:
                cols = [c.column for x in run for c in sheet["by_row"][x]]
                blocks.append((run[0], run[-1], min(cols), max(cols)))
            run = []
        if r is not None:
            run.append(r)
    return blocks


def bordered_copy(xlsx: str, blocks: dict, out_dir: str) -> str:
    """A copy of the workbook where the cells of each table block get a thin border, so a
    table saved without borders (e.g. by image -> Excel) is drawn as a table. blocks maps a
    sheet's XML part to its table blocks. Only the border of those cells changes: a copy of
    each cell's own style is made with the border added. Everything else, charts included,
    is copied byte for byte."""
    dst = os.path.join(out_dir, "bordered_" + os.path.basename(xlsx))
    with zipfile.ZipFile(xlsx) as zin:
        styles = zin.read("xl/styles.xml").decode("utf-8")
        # 1. one new border: thin on all four sides
        thin = ('<border><left style="thin"><color rgb="FF808080"/></left><right style="thin"><color rgb="FF808080"/>'
                '</right><top style="thin"><color rgb="FF808080"/></top><bottom style="thin"><color rgb="FF808080"/>'
                '</bottom><diagonal/></border>')
        n_borders = len(re.findall(r"<border\b(?![s])", styles))
        if re.search(r"<borders\b[^>]*/>", styles):
            styles = re.sub(r"<borders\b[^>]*/>", f'<borders count="1">{thin}</borders>', styles, 1)
            n_borders = 0
        else:
            styles = styles.replace("</borders>", thin + "</borders>", 1)
        styles = re.sub(r'(<borders\b[^>]*count=")\d+(")', rf"\g<1>{n_borders + 1}\2", styles, 1)
        border_id = n_borders
        # 2. the cell formats, so each style used in a table gets a bordered twin
        m = re.search(r"<cellXfs\b[^>]*>(.*?)</cellXfs>", styles, re.S)
        xfs = re.findall(r"<xf\b[^>]*/>|<xf\b[^>]*>.*?</xf>", m.group(1), re.S)
        twins, added = {}, []

        def twin(sid: int) -> int:
            if sid not in twins:
                xf = xfs[sid] if sid < len(xfs) else '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                xf = re.sub(r'\s(borderId|applyBorder)="[^"]*"', "", xf)
                xf = xf.replace("<xf", f'<xf borderId="{border_id}" applyBorder="1"', 1)
                twins[sid] = len(xfs) + len(added)
                added.append(xf)
            return twins[sid]

        sheets_xml = {}
        for part, blks in blocks.items():
            xml = zin.read(part).decode("utf-8")

            def restyle(mc):
                tag, ref = mc.group(0), mc.group(1)
                col_letters, row = re.match(r"([A-Z]+)(\d+)", ref).groups()
                col = 0
                for ch in col_letters:
                    col = col * 26 + ord(ch) - 64
                if not any(r1 <= int(row) <= r2 and c1 <= col <= c2 for r1, r2, c1, c2 in blks):
                    return tag
                sm = re.search(r'\ss="(\d+)"', tag)
                new_s = twin(int(sm.group(1)) if sm else 0)
                return re.sub(r'\ss="\d+"', f' s="{new_s}"', tag) if sm else tag.replace("<c ", f'<c s="{new_s}" ', 1)

            sheets_xml[part] = re.sub(r'<c\b[^>]*?\br="([A-Z]+\d+)"[^>]*?/?>', restyle, xml)
        body = m.group(1) + "".join(added)
        cell_xfs = re.sub(r'count="\d+"', f'count="{len(xfs) + len(added)}"', m.group(0)[:m.group(0).index(">") + 1], 1)
        styles = styles[:m.start()] + cell_xfs + body + "</cellXfs>" + styles[m.end():]

        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == "xl/styles.xml":
                    zout.writestr(item, styles.encode("utf-8"))
                elif item.filename in sheets_xml:
                    zout.writestr(item, sheets_xml[item.filename].encode("utf-8"))
                else:
                    zout.writestr(item, zin.read(item.filename))
    return dst


# ---------------------------------------------------------------------------
# drawing and checking the pictures
# ---------------------------------------------------------------------------
def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")[:40] or "sheet"


def save_image(pix, path: str, fmt: str, dpi: int, quality: int) -> None:
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if fmt == "png":
        img.save(path, "PNG", dpi=(dpi, dpi))
    else:
        img.save(path, "JPEG", quality=quality, dpi=(dpi, dpi), subsampling=0, optimize=True)


def check_image(path: str, fmt: str, size: tuple, has_content: bool):
    problems = []
    try:
        with Image.open(path) as im:
            want = "PNG" if fmt == "png" else "JPEG"
            if im.format != want:
                problems.append(f"saved as {im.format}, expected {want}")
            if im.size != size:
                problems.append(f"size {im.size[0]}x{im.size[1]}px, expected {size[0]}x{size[1]}px")
            lo, hi = im.convert("L").getextrema()
            if has_content and hi - lo <= INK_CONTRAST:
                problems.append("the picture is blank but the sheet has content")
    except Exception as exc:
        problems.append(f"could not read the picture back: {exc}")
    return problems


def words_of(s: str) -> list:
    return re.findall(r"\w+", s.lower())


def cell_coverage(sheet: dict, page) -> tuple:
    """(cells whose words are all on the page, text cells, [first missing cells])."""
    page_words = set(words_of(page.get_text()))
    total = ok = 0
    missing = []
    for c in sheet["cells"]:
        if not isinstance(c.value, str):
            continue
        ws = words_of(c.value)
        if not ws:
            continue
        total += 1
        if all(w in page_words for w in ws):
            ok += 1
        elif len(missing) < 5:
            missing.append(f"{c.coordinate} {c.value[:30]!r}")
    return ok, total, missing


def parse_sheets_arg(spec: str, n: int) -> list:
    if spec in ("all", ""):
        return list(range(1, n + 1))
    out = []
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        if not a.isdigit() or (b and not b.isdigit()):
            raise SystemExit(f"--sheets should be all, or like 1, 1-3 or 2,4 (got {spec!r})")
        out.extend(range(int(a), int(b or a) + 1))
    return sorted({i for i in out if 1 <= i <= n})


def run(xlsx: str, out_dir: str, fmt: str, dpi: int, quality: int, sheets_spec: str,
        large: str, base_url: str, use_ai: bool) -> bool:
    name = os.path.splitext(os.path.basename(xlsx))[0]
    print("=" * 72)
    print(xlsx)
    print("=" * 72)
    started = time.time()

    try:
        sheets = read_sheets(xlsx)
    except Exception as exc:
        print(f"  COULD NOT OPEN: {exc}")
        return False
    visible = [s for s in sheets if not s["hidden"]]
    wanted = set(parse_sheets_arg(sheets_spec, len(sheets)))
    targets = [s for s in visible if s["index"] in wanted and not s["empty"]]
    for s in visible:
        if s["index"] in wanted and s["empty"]:
            print(f"  sheet {s['index']} '{s['name']}': empty, no picture")
    if not targets:
        print(f"  SKIPPED: no sheet to draw (sheets: {len(sheets)}, hidden: {len(sheets) - len(visible)}, "
              f"selected by --sheets {sheets_spec!r})")
        return False

    if not have_calc():
        print("  CONVERSION FAILED: LibreOffice Calc is not installed. Install it with:\n"
              "      sudo apt-get install -y libreoffice-calc")
        return False

    img_out = os.path.join(out_dir, name)
    os.makedirs(img_out, exist_ok=True)
    print(f"  {len(sheets)} sheet(s), converting {len(targets)} as {fmt.upper()} at {dpi} dpi -> {img_out}/")

    # 1. the PLAN, per sheet
    plans, ai_used = {}, 0
    for s in targets:
        plan, why, secs, toks = None, "", 0.0, 0
        if use_ai and ai_used < MAX_AI_SHEETS and s["cells"]:
            ai_used += 1
            avg_h = max(10.0, float(s["ws"].sheet_format.defaultRowHeight or 15))
            rows_per_image = max(5, int(MAX_PART_PX * 72 / dpi / avg_h))
            try:
                plan, secs, toks = ask_model(base_url, build_prompt(fmt, dpi, rows_per_image),
                                             sheet_facts(s) + outline(s))
            except Exception as exc:
                why = (f"the model server at {base_url} could not be reached"
                       if "ConnectionError" in type(exc).__name__ or "Max retries" in str(exc)
                       else str(exc).splitlines()[0][:160])
        elif use_ai and s["cells"]:
            why = f"only the first {MAX_AI_SHEETS} sheets are planned by the model"
        else:
            why = (("already planned by " + os.environ["IDOX_PLANNED_BY"]) if os.environ.get("IDOX_PLANNED_BY")
                   else "turned off (--no-ai)") if not use_ai else "no cells"
        used_ai = plan is not None
        regions, cuts, notes = repair_plan(plan or default_plan(s), s) if s["cells"] else ([], [], [])
        plans[s["index"]] = {"regions": regions, "cuts": cuts, "notes": notes, "ai": used_ai,
                             "warnings": [w for w in (plan.warnings if plan else [])
                                      if checked_warning(w, s)]}
        if used_ai:
            print(f"  sheet {s['index']} '{s['name']}': AI plan, {len(regions)} region(s) "
                  f"({secs:.1f}s, {toks} tokens): "
                  + ", ".join(f"{k} R{a}-{b}" for k, a, b, _ in regions[:8]) + (" ..." if len(regions) > 8 else ""))
        else:
            print(f"  sheet {s['index']} '{s['name']}': AI plan not used ({why})")
        for n in notes:
            print(f"       plan repaired: {n}")
        for w in plans[s["index"]]["warnings"]:
            print(f"       ~ model note (not checked against the file): {w}")

    # 2. draw with LibreOffice
    with tempfile.TemporaryDirectory() as tmp:
        grid = {s["part"]: table_blocks(s) for s in targets if s["no_borders"] and s["cells"] and s["part"]}
        grid = {k: v for k, v in grid.items() if v}
        try:
            src = bordered_copy(os.path.abspath(xlsx), grid, tmp) if grid else os.path.abspath(xlsx)
            pdf_path = to_pdf(src, tmp)
            doc = pymupdf.open(pdf_path)
        except Exception as exc:
            print(f"  CONVERSION FAILED: {exc}")
            return False
        # LibreOffice writes one page per sheet, hidden and empty sheets too (an empty one as a
        # 1 pt page). Match on that; fall back to the visible / non-empty lists if the count says so.
        nonempty = [s for s in visible if not s["empty"]]
        for group in (sheets, visible, nonempty):
            if doc.page_count == len(group):
                page_of = {s["index"]: doc[i] for i, s in enumerate(group)}
                break
        else:
            print(f"  CONVERSION FAILED: the PDF has {doc.page_count} page(s) for {len(sheets)} sheet(s) "
                  f"({len(visible)} visible); the sheets cannot be matched to pages")
            return False


        # 3. one picture per sheet, or its parts
        results, written, total_bytes = [], 0, 0
        for s in targets:
            page = page_of[s["index"]]
            W, H = page.rect.width, page.rect.height
            lines = pdf_lines(page)
            sdpi = min(dpi, int(MAX_SIDE_PX * 72 / W)) if W * dpi / 72 > MAX_SIDE_PX else dpi
            infos = []
            if sdpi != dpi:
                infos.append(f"drawn at {sdpi} dpi: {W * dpi / 72:.0f}px wide is over the {MAX_SIDE_PX}px limit")
            cuts = []
            if H * sdpi / 72 > MAX_PART_PX:
                if large == "scale" and MAX_PART_PX * 72 / H >= MIN_DPI:
                    sdpi = min(sdpi, int(MAX_PART_PX * 72 / H))
                    infos.append(f"scaled down to {sdpi} dpi to fit one picture")
                else:
                    cuts = choose_cuts(H, MAX_PART_PX * 72 / sdpi, plans[s["index"]]["cuts"],
                                       row_positions(s, lines), lines)
            bounds = [0.0] + cuts + [H]
            parts, problems, expect_files = [], [], []
            for p in range(len(bounds) - 1):
                clip = pymupdf.Rect(0, bounds[p], W, bounds[p + 1])
                fname = f"sheet_{s['index']:02d}_{safe_name(s['name'])}" + (f"_part_{p + 1:02d}" if cuts else "") + f".{fmt}"
                path = os.path.join(img_out, fname)
                try:
                    pix = page.get_pixmap(dpi=sdpi, clip=clip, alpha=False)
                    save_image(pix, path, fmt, sdpi, quality)
                except Exception as exc:
                    problems.append(f"{fname}: could not be written ({exc})")
                    continue
                written += 1
                total_bytes += os.path.getsize(path)
                problems += [f"{fname}: {m}" for m in check_image(path, fmt, (pix.width, pix.height), bool(s["cells"]))]
                parts.append((fname, pix.width, pix.height, os.path.getsize(path)))
            if cuts:
                cut_through = [t[:30] for t, b in lines if any(ink(b)[0] < y < ink(b)[1] for y in cuts)]
                if cut_through:
                    problems.append(f"{len(cut_through)} line(s) of text lie across a cut, e.g. {cut_through[0]!r}")
                infos.append(f"{len(parts)} parts, cut at pt " + ", ".join(f"{y:.0f}" for y in cuts))
            ok_cells, text_cells, missing = cell_coverage(s, page)
            cov = ok_cells / text_cells if text_cells else 1.0
            if cov < CELL_COVERAGE:
                problems.append(f"only {ok_cells}/{text_cells} text cells are on the page; missing e.g. {'; '.join(missing)}")
            results.append((s, parts, problems, ok_cells, text_cells, expect_files))
            sizes = ", ".join(f"{w}x{h}px {sz / 1024:.0f} KB" for _, w, h, sz in parts)
            extra = []
            if s["part"] in grid:
                extra.append(f"no borders in the file, {len(grid[s['part']])} table(s) drawn with thin borders")
            if s["charts"]:
                extra.append(f"{s['charts']} chart(s) drawn by LibreOffice")
            if s["cond"]:
                extra.append(f"{s['cond']} conditional-format rule(s)")
            print(f"  sheet {s['index']} '{s['name']}': {len(parts)} picture(s): {sizes or 'none written'}"
                  f"; cells {ok_cells}/{text_cells} text cells on the page"
                  + (f"; {'; '.join(extra)}" if extra else "") + f"  [{'PASS' if not problems else 'FAIL'}]")
            for i in infos:
                print(f"       - {i}")
            for pr in problems:
                print(f"       ! {pr}")
            if s["unevaluated"]:
                print(f"       ~ {s['unevaluated']} formula cell(s) have no stored result; LibreOffice calculated "
                      f"them and they are not compared with a stored value")

    n_bad = sum(1 for r in results if r[2])
    n_img = sum(len(r[1]) for r in results)
    ok = n_bad == 0 and len(results) == len(targets) and n_img > 0
    print()
    print("  --- verification (read back from the saved pictures) ---")
    print(f"  sheets converted:  {len(results)}/{len(targets)}")
    print(f"  pictures written:  {written}")
    print(f"  sheets verified:   {len(results) - n_bad}/{len(results)}")
    print(f"  AI plan:           {sum(1 for t in targets if plans[t['index']]['ai'])}/{len(targets)} sheet(s) planned by the model")
    print(f"  {total_bytes / 1024:.0f} KB total, {time.time() - started:.1f}s")
    print(f"  VERDICT: {'PASS -- every sheet drawn and verified' if ok else 'FAIL -- see the problems above'}")
    print()
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", help="Excel file(s) (.xlsx / .xlsm), globs allowed")
    parser.add_argument("--outdir", default=IMG_OUT_DIR, help=f"where the per-workbook folders go (default {IMG_OUT_DIR})")
    parser.add_argument("--format", choices=sorted(FORMATS), default="png", help="picture format (default png)")
    parser.add_argument("--dpi", type=int, default=None,
                        help="resolution; default 200 for png, 150 for jpg / jpeg")
    parser.add_argument("--quality", type=int, default=None, help="jpg / jpeg quality 1-100 (default 90)")
    parser.add_argument("--sheets", default="all", help="e.g. 1, 1-3, 2,4 by position (default: all)")
    parser.add_argument("--large", choices=("split", "scale"), default="split",
                        help="a sheet too tall for one picture: split it into parts (default) or scale it down")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="the model server for the plan "
                        f"(default {DEFAULT_BASE_URL}, the 2B)")
    parser.add_argument("--no-ai", action="store_true", help="skip the model; plan with plain rules")
    args = parser.parse_args()

    dpi = args.dpi if args.dpi is not None else FORMATS[args.format]["dpi"]
    quality = args.quality if args.quality is not None else FORMATS[args.format].get("quality", 90)
    if not 30 <= dpi <= 600:
        raise SystemExit("--dpi must be between 30 and 600")
    if not 1 <= quality <= 100:
        raise SystemExit("--quality must be between 1 and 100")

    paths = []
    for pattern in args.files:
        paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern])

    ok = fail = 0
    for path in paths:
        if not os.path.exists(path):
            print(f"skipping {path}: not found")
            continue
        if run(path, args.outdir, args.format, dpi, quality, args.sheets, args.large,
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
