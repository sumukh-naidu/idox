"""
csv_convert.py -- convert a CSV file into Excel (.xlsx), Word (.docx), PowerPoint (.pptx),
PDF or pictures (PNG / JPG / JPEG).

    --to xlsx            a formatted workbook: title, bold header, borders, numbers stored as
                         numbers but shown exactly as written in the CSV, frozen header row
    --to pdf             that workbook drawn by LibreOffice Calc (a long table cut into pages)
    --to png|jpg|jpeg    that workbook drawn as pictures (a long table split into parts)
    --to docx | pptx     a Word document / a slide deck with the table as a real table

HOW IT WORKS
  1. READ (no model). The encoding (UTF-8, UTF-16, Windows-1252) and the delimiter (, ; tab |)
     are detected; quoted fields, padded spacing and ragged rows are handled.
  2. PLAN (the Qwen 2B, every file). The model reads the first and last rows and answers in a
     fixed JSON shape: is the first row a header, a title for the table, and the TYPE of each
     column -- text, number, currency, percent, date, or id (codes, phone numbers, anything
     with leading zeros that must stay text).
  3. CHECK (no model). Every answer is checked against the data before it is used: a column
     becomes numbers only if at least 95% of its values really parse as numbers (otherwise it
     stays text, so nothing is changed); "id" keeps a column as text; the header answer is
     overruled when the data clearly says otherwise; the title is used only if its words come
     from the file's name or its column names.
  4. BUILD (no model). The workbook is written with openpyxl. A number keeps the look it had in
     the CSV ($1,234.50 stays $1,234.50; 12.5% stays 12.5%) through its number format.
  5. CONVERT (no model). PDF and pictures: excel_to_image.py / excel_to_office.py draw the
     workbook with LibreOffice Calc. Word and PowerPoint: excel_to_office.py writes the table.
  6. VERIFY (no model). LibreOffice opens the workbook and every cell must DISPLAY exactly as
     the CSV wrote it; then the converter's own read-back checks run on the output.

If the model server is down (or --no-ai), the plan comes from the same checks alone.

Needs LibreOffice Calc:  sudo apt-get install -y libreoffice-calc
Usage:
    .venv/bin/python scripts/csv_convert.py input/csv_to_excel_input/sales.csv --to xlsx
    .venv/bin/python scripts/csv_convert.py input/csv_to_pdf_input/*.csv --to pdf
    .venv/bin/python scripts/csv_convert.py data.csv --to png --no-ai
"""

import argparse
import csv
import glob
import io
import os
import re
import sys
import tempfile
import time
from typing import List, Literal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, Field

import excel_to_image as x2i

TARGETS = ("xlsx", "pdf", "docx", "pptx", "png", "jpg", "jpeg")
OUT_DIRS = {"xlsx": "output/csv_to_excel_output", "pdf": "output/csv_to_pdf_output", "docx": "output/csv_to_word_output",
            "pptx": "output/csv_to_ppt_output", "png": "output/csv_to_image_output", "jpg": "output/csv_to_image_output",
            "jpeg": "output/csv_to_image_output"}
DEFAULT_BASE_URL = x2i.DEFAULT_BASE_URL
NUMERIC_SHARE = 0.95            # share of a column's values that must parse before it becomes numbers
SAMPLE_ROWS = 15
TITLE_FILLER = {"data", "table", "report", "list", "summary", "details", "overview", "records", "sheet", "the", "and", "for", "with", "by", "of"}
MAX_DOC_ROWS = 2000             # Word / PowerPoint: a longer table is written, with a warning


# ---------------------------------------------------------------------------
# 1. reading
# ---------------------------------------------------------------------------
def read_csv(path: str):
    """(rows, encoding, delimiter name). Rows padded to the widest row, empty rows dropped."""
    raw = open(path, "rb").read()
    if b"\x00" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("this is not a text file")
    text = enc = None
    for e in (("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ()) + ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text, enc = raw.decode(e), e
            break
        except UnicodeDecodeError:
            continue
    sample = text[:65536]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in ",;\t|"}
        delim = max(counts, key=counts.get) if any(counts.values()) else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim, quotechar='"', skipinitialspace=True))
    rows = [[c.strip() for c in r] for r in rows]
    rows = [r for r in rows if any(c for c in r)]
    width = max((len(r) for r in rows), default=0)
    rows = [r + [""] * (width - len(r)) for r in rows]
    names = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
    return rows, enc, names.get(delim, repr(delim))


# ---------------------------------------------------------------------------
# numbers as they are written
# ---------------------------------------------------------------------------
CURRENCY = "$€£₹¥"
NUM_RE = re.compile(r"^(?P<neg>[-−(])?\s*(?P<cur1>[$€£₹¥])?\s*(?P<num>\d[\d,.' ]*)\s*(?P<cur2>[$€£₹¥])?\s*(?P<pct>%)?\)?$")


def parse_number(s: str, decimal: str):
    """(value, look) for a number written as text, or None. look = (decimals, thousands, symbol,
    symbol after, percent). decimal is "." or "," -- which mark is the decimal point."""
    s = s.strip()
    m = NUM_RE.match(s)
    if not m or (s.startswith("(") != s.endswith(")")):
        return None
    body = m.group("num").replace(" ", "").replace("'", "")
    thou = "," if decimal == "." else "."
    if body.count(decimal) > 1:
        return None
    whole, _, frac = body.partition(decimal)
    if thou in whole:
        groups = whole.split(thou)
        if not (1 <= len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:])):
            return None
    whole_digits = whole.replace(thou, "")
    if not whole_digits.isdigit() or (frac and not frac.isdigit()) or thou in frac:
        return None
    if len(whole_digits) > 1 and whole_digits.startswith("0") and not frac:
        return None                                         # leading zero: an id, not a number
    value = float(f"{whole_digits}.{frac}" if frac else whole_digits)
    if m.group("neg"):
        value = -value
    pct = bool(m.group("pct"))
    sym = m.group("cur1") or m.group("cur2") or ""
    return (value / 100 if pct else value), (len(frac), thou in whole, sym, bool(m.group("cur2")), pct)


def column_numbers(values: list):
    """For a column's non-empty values: (share that parse, decimal mark, [parsed or None])."""
    best = (0.0, ".", [None] * len(values))
    for dec in (".", ","):
        parsed = [parse_number(v, dec) if v else None for v in values]
        filled = [v for v in values if v]
        share = (sum(1 for v, p in zip(values, parsed) if v and p) / len(filled)) if filled else 0.0
        if share > best[0]:
            best = (share, dec, parsed)
    return best


def number_format(looks: list) -> str:
    """The Excel number format that shows the column's numbers as they were written. Mixed
    decimals with nothing else special -> General (8.2 stays 8.2, 10 stays 10)."""
    decs = {l[0] for l in looks}
    thou = any(l[1] for l in looks)
    syms = {l[2] for l in looks if l[2]}
    after = any(l[3] for l in looks)
    pct = all(l[4] for l in looks)
    if len(decs) > 1 and not thou and not syms and not pct:
        return "General"
    d = max(decs)
    core = ("#,##0" if thou else "0") + ("." + "0" * d if d else "")
    if pct:
        return core + "%"
    if syms:
        sym = syms.pop()
        return f'{core}"{sym}"' if after else f'"{sym}"{core}'
    return core


# ---------------------------------------------------------------------------
# 2-3. the model's plan, and its checks
# ---------------------------------------------------------------------------
class ColumnPlan(BaseModel):
    name: str = Field(description="The column's name (its header, or a short name if there is none).")
    kind: Literal["text", "number", "currency", "percent", "date", "id"]


class CsvPlan(BaseModel):
    has_header: bool = Field(description="True if the first row holds column names, not data.")
    title: str = Field(description="A short title for the table, at most 8 words, from the file name and column names.")
    columns: List[ColumnPlan] = Field(max_length=60, description="One entry per column, in order.")


CSV_TEMPLATE = """You plan how ONE CSV file is turned into a formatted table. A program copies every
value itself; you never write or change values. You read the file's first and last rows and
answer with a JSON plan.

RULES
1. has_header: true if the first row holds column names (words like Name, Date, Amount),
   false if it is already data.
2. title: a short title (at most 8 words) built from the file name and the column names.
   It is the MAIN HEADING of the document, and the main heading is always placed in the
   centre, across the full width of the table.
3. columns: one entry per column, in order, with its kind:
   - number    measurements, counts, quantities
   - currency  money amounts (with or without a currency sign)
   - percent   values written with %
   - date      dates or times
   - id        codes, phone numbers, postal codes, account numbers, anything with leading
               zeros or that is never added up -- these must stay exactly as written
   - text      everything else
4. Never invent columns: exactly as many entries as the file has columns."""


def ask_plan(base_url: str, name: str, rows: list, enc: str, delim: str):
    import requests

    shown = rows[:SAMPLE_ROWS] + ([["..."]] + rows[-2:] if len(rows) > SAMPLE_ROWS + 2 else rows[SAMPLE_ROWS:])
    sample = "\n".join(" | ".join(c[:40] for c in r) for r in shown)
    user = (f"FILE: {name}\n{len(rows)} rows x {len(rows[0])} columns, {delim}-separated, {enc}\n"
            f"ROWS (cells separated by ' | '):\n{sample}")
    body = {"model": os.environ.get("IDOX_MODEL", "manual") or "manual",
            "messages": [{"role": "system", "content": CSV_TEMPLATE}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "csv_plan", "schema": CsvPlan.model_json_schema()}},
            "temperature": 0, "repeat_penalty": 1.05, "n_predict": 1200}
    key = os.environ.get("IDOX_API_KEY", "").strip()
    t0 = time.time()
    resp = requests.post(f"{base_url.rstrip('/')}/v1/chat/completions", json=body,
                         headers={"Authorization": f"Bearer {key}"} if key else {},
                         timeout=int(os.environ.get("IDOX_TIMEOUT", "300") or 300))
    if not resp.ok:
        raise RuntimeError(f"the server answered {resp.status_code}: {resp.text[:120].strip()}")
    return CsvPlan.model_validate_json(resp.json()["choices"][0]["message"].get("content") or ""), time.time() - t0


def header_by_data(rows: list):
    """True when the data proves the first row is a header (it holds words where the rest of the
    column is numbers), else None -- a first row of numbers may still be a header (years such
    as 1958, 1959), so the data alone cannot say "no header"."""
    if len(rows) < 2:
        return None
    first, rest = rows[0], rows[1:]
    for j in range(len(first)):
        if first[j] and column_numbers([r[j] for r in rest])[0] >= NUMERIC_SHARE \
                and not (parse_number(first[j], ".") or parse_number(first[j], ",")):
            return True
    return None


def decide(plan, rows: list, stem: str):
    """The plan checked against the data. Returns (has_header, title, kinds, notes)."""
    notes, ncol = [], len(rows[0])
    by_data = header_by_data(rows)
    has_header = plan.has_header if plan else bool(by_data)
    if by_data and plan and not plan.has_header:
        notes.append(f"header: the model said {plan.has_header}, the data says {by_data} -- data used")
        has_header = by_data
    if plan is None and by_data is None:
        # no model: a first row of distinct, non-empty cells with at least one word is a header
        has_header = (all(rows[0]) and len(set(rows[0])) == len(rows[0])
                      and any(not parse_number(c, ".") for c in rows[0]))
    body = rows[1:] if has_header else rows
    kinds = []
    for j in range(ncol):
        share, _, _ = column_numbers([r[j] for r in body])
        said = plan.columns[j].kind if plan and j < len(plan.columns) else None
        if plan and len(plan.columns) != ncol and j == 0:
            notes.append(f"the model listed {len(plan.columns)} column(s) for {ncol}; missing ones decided by the data")
        if said in ("number", "currency", "percent"):
            if share >= NUMERIC_SHARE:
                kinds.append("number")
            else:
                kinds.append("text")
                notes.append(f"column {j + 1}: the model said {said}, but only {share:.0%} of its values are numbers -- kept as text")
        elif said == "text" and share >= NUMERIC_SHARE:
            notes.append(f"column {j + 1}: the model said text, but {share:.0%} of its values are numbers -- numbers used")
            kinds.append("number")
        elif said in ("id", "date", "text"):
            kinds.append("text")                    # id / date: kept exactly as written
        else:
            kinds.append("number" if share >= NUMERIC_SHARE else "text")
    source = (stem + " " + " ".join(rows[0])).lower()
    words = set(re.findall(r"[a-z]{3,}", source.replace("_", " ")))
    squashed = re.sub(r"[^a-z]", "", source)

    def from_file(w: str) -> bool:
        # inside the file name or a column name ("travel" in "airtravel"), or sharing a stem
        # with one of their words ("monthly" / "month")
        return w in squashed or any(len(x) >= 4 and (w.startswith(x) or x.startswith(w)) for x in words)

    title = None
    if plan and plan.title.strip() and len(plan.title.split()) <= 10:
        tw = [w for w in re.findall(r"[a-z]{3,}", plan.title.lower()) if w not in TITLE_FILLER]
        if tw and sum(from_file(w) for w in tw) / len(tw) >= 0.6:
            title = plan.title.strip()
        else:
            notes.append(f"title {plan.title!r} is not from the file's name or columns -- not used")
    if not title:
        title = re.sub(r"[_\-]+", " ", stem).strip().title()
    return has_header, title, kinds, notes


# ---------------------------------------------------------------------------
# 4. the workbook
# ---------------------------------------------------------------------------
def build_workbook(rows: list, has_header: bool, title: str, kinds: list, path: str) -> dict:
    """Write the formatted workbook. Returns {(row, col): text as written} for every data cell."""
    ncol = len(rows[0])
    header = rows[0] if has_header else [f"Column {j + 1}" for j in range(ncol)]
    body = rows[1:] if has_header else rows
    wb = Workbook()
    ws = wb.active
    ws.title = re.sub(r"[\[\]:*?/\\]", " ", title)[:31] or "Table"
    thin = Side(style="thin", color="999999")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    # the title is the document's main heading: always centred across the table's full width
    # (Word and PowerPoint read this alignment from the cell, so they centre it too)
    ws.cell(1, 1, title).font = Font(bold=True, size=14)
    ws.cell(1, 1).alignment = Alignment(horizontal="center", vertical="center")
    if ncol > 1:
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    for j, h in enumerate(header, 1):
        c = ws.cell(3, j, h)
        c.font, c.fill, c.border = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F3D6D"), box
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    expect, comma_cols = {}, []
    # a file that writes decimals with a comma uses "." for thousands ("1.200" is twelve hundred):
    # its number columns are all kept exactly as written
    comma_file = any(kinds[j] == "number" and column_numbers([r[j] for r in body])[1] == ","
                     and any("," in r[j] for r in body) for j in range(ncol))
    for j in range(ncol):
        col = [r[j] for r in body]
        dec, parsed, thou = None, None, False
        if kinds[j] == "number":
            _, dec, parsed = column_numbers(col)
            if dec == "," or comma_file:
                # a decimal comma cannot be shown by a number format (the machine's decimal point
                # decides that): the values stay exactly as written, as right-aligned text
                parsed = None
                comma_cols.append(j + 1)
            else:
                thou = any(p[1][1] for p in parsed if p)
        width = max([len(h) for h in [header[j]]] + [len(v) for v in col[:2000]]) + 2
        ws.column_dimensions[get_column_letter(j + 1)].width = min(max(width, 6), 60)
        for i, v in enumerate(col):
            r = 4 + i
            if parsed and parsed[i]:
                c = ws.cell(r, j + 1, parsed[i][0])
                look = parsed[i][1]
                # each value keeps its own decimals ("1.0" stays 1.0, "10%" stays 10%); the
                # thousands separator follows the column, so 950 and 1,200 sit side by side
                c.number_format = number_format([(look[0], look[1] or (thou and abs(parsed[i][0]) >= 1000),
                                                  look[2], look[3], look[4])])
                c.alignment = Alignment(horizontal="right")
            elif kinds[j] == "number" and v:
                c = ws.cell(r, j + 1, v)
                c.number_format = "@"
                c.alignment = Alignment(horizontal="right")
            else:
                c = ws.cell(r, j + 1, v if v else None)
                c.number_format = "@"                       # text stays text: no leading zeros lost
            c.border = box
            if v:
                expect[(r, j + 1)] = v
    # a title wider than the table wraps onto more lines instead of being cut at the table's edge
    # (a 14 pt bold character is about 1.5 column-width units)
    total = sum(ws.column_dimensions[get_column_letter(j + 1)].width or 8.43 for j in range(ncol))
    lines = max(1, -(-int(len(title) * 1.5) // max(1, int(total))))
    if lines > 1:
        ws.cell(1, 1).alignment = Alignment(horizontal="center", wrap_text=True, vertical="top")
        ws.row_dimensions[1].height = 20 * lines
    ws.freeze_panes = "A4"
    ws.auto_filter.ref = f"A3:{get_column_letter(ncol)}{3 + len(body)}"
    wb.save(path)
    for j, h in enumerate(header, 1):
        expect[(3, j)] = h
    return expect, comma_cols


def display_check(xlsx: str, expect: dict, tmp: str):
    """(cells shown exactly as the CSV wrote them, cells checked, [first mismatches])."""
    import excel_to_office as x2o

    grids = x2o.shown_values(xlsx, tmp)
    grid = next(iter(grids.values()), {}) if grids else {}
    bad = [(k, v, grid.get(k, "")) for k, v in expect.items() if grid.get(k, "").strip() != v.strip()]
    return len(expect) - len(bad), len(expect), bad[:5]


# ---------------------------------------------------------------------------
def run(path: str, to: str, out_dir: str, base_url: str, use_ai: bool, dpi, quality) -> bool:
    stem = os.path.splitext(os.path.basename(path))[0]
    print("=" * 72)
    print(path)
    print("=" * 72)
    started = time.time()
    try:
        rows, enc, delim = read_csv(path)
    except Exception as exc:
        print(f"  COULD NOT OPEN: {exc}")
        return False
    if not rows or not rows[0]:
        print("  SKIPPED: the file holds no data")
        return False
    print(f"  {len(rows)} row(s) x {len(rows[0])} column(s), {delim}-separated, {enc}, converting as {to.upper()} "
          f"-> {out_dir}/")
    if not x2i.have_calc():
        print("  CONVERSION FAILED: LibreOffice Calc is not installed. Install it with:\n"
              "      sudo apt-get install -y libreoffice-calc")
        return False

    plan, why = None, "turned off (--no-ai)"
    if use_ai:
        try:
            plan, secs = ask_plan(base_url, os.path.basename(path), rows, enc, delim)
            why = f"{secs:.1f}s"
        except Exception as exc:
            why = (f"the model server at {base_url} could not be reached" if "Max retries" in str(exc)
                   or "ConnectionError" in type(exc).__name__ else str(exc).splitlines()[0][:120])
    has_header, title, kinds, notes = decide(plan, rows, stem)
    if plan:
        print(f"  AI plan ({why}): header {plan.has_header}, title {plan.title!r}, columns: "
              + ", ".join(f"{c.name[:14]}={c.kind}" for c in plan.columns[:10]) + (" ..." if len(plan.columns) > 10 else ""))
    else:
        print(f"  AI plan: not used ({why})")
    for n in notes:
        print(f"       plan checked: {n}")
    print(f"  used: header {has_header}, title {title!r}, {kinds.count('number')} number column(s), "
          f"{kinds.count('text')} text column(s)")
    body_rows = len(rows) - (1 if has_header else 0)
    if to in ("docx", "pptx") and body_rows > MAX_DOC_ROWS:
        print(f"  ~ {body_rows} rows is a very long table for {'Word' if to == 'docx' else 'slides'}; it is all written")

    problems, ok_out = [], True
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        xlsx = os.path.join(out_dir if to == "xlsx" else tmp, f"{stem}.xlsx")
        try:
            expect, comma_cols = build_workbook(rows, has_header, title, kinds, xlsx)
            if comma_cols:
                print(f"  ~ column(s) {', '.join(map(str, comma_cols))} use a decimal comma: kept exactly as written "
                      f"(as text, right-aligned), since a number format would show the machine's decimal point")
        except Exception as exc:
            print(f"  COULD NOT WRITE: {exc}")
            return False
        good, total, bad = display_check(xlsx, expect, tmp)
        print(f"  display check:     {good}/{total} cells show exactly as written in the CSV"
              + (" -- e.g. " + "; ".join(f"R{k[0]}C{k[1]} {v!r} shown as {g!r}" for k, v, g in bad) if bad else ""))
        if good < total:
            problems.append(f"{total - good} cell(s) do not show as the CSV wrote them")
        if to != "xlsx":
            import excel_to_office as x2o
            os.environ["IDOX_PLANNED_BY"] = "the CSV plan above" if plan else "the CSV's own data"
            print("  --- converting the formatted table ---")
            if to in ("pdf", "docx", "pptx"):
                ok_out = x2o.run(xlsx, to, out_dir, "all", "tables", base_url, False)
            else:
                d = dpi if dpi is not None else x2i.FORMATS[to]["dpi"]
                q = quality if quality is not None else x2i.FORMATS[to].get("quality", 90)
                ok_out = x2i.run(xlsx, out_dir, to, d, q, "all", "split", base_url, False)
            if not ok_out:
                problems.append("the converter's own verification failed (see above)")
        else:
            print(f"  {stem}.xlsx written")
    for p in problems:
        print(f"  ! {p}")
    ok = not problems
    print()
    print("  --- CSV summary ---")
    print(f"  AI plan:           {'used' if plan else 'not used'}")
    print(f"  {time.time() - started:.1f}s")
    print(f"  VERDICT: {'PASS -- converted and verified' if ok else 'FAIL -- see the problems above'}")
    print()
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", help="CSV file(s), globs allowed")
    parser.add_argument("--to", choices=TARGETS, required=True)
    parser.add_argument("--format", choices=("png", "jpg", "jpeg"), default=None,
                        help="the picture format; overrides --to when given (used by the API and web app)")
    parser.add_argument("--outdir", default=None, help="default csv_to_<kind>_output")
    parser.add_argument("--dpi", type=int, default=None, help="pictures: default 200 for png, 150 for jpg / jpeg")
    parser.add_argument("--quality", type=int, default=None, help="jpg / jpeg quality 1-100 (default 90)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"the model server (default {DEFAULT_BASE_URL})")
    parser.add_argument("--no-ai", action="store_true", help="skip the model; the plan comes from the data alone")
    args = parser.parse_args()
    if args.format and args.to in ("png", "jpg", "jpeg"):
        args.to = args.format
    paths = []
    for pattern in args.files:
        paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern])
    ok = fail = 0
    for path in paths:
        if not os.path.exists(path):
            print(f"skipping {path}: not found")
            continue
        if run(path, args.to, args.outdir or OUT_DIRS[args.to], args.base_url, not args.no_ai, args.dpi, args.quality):
            ok += 1
        else:
            fail += 1
    if len(paths) > 1:
        print("=" * 72)
        print(f"{ok} succeeded, {fail} failed, {len(paths)} total")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
