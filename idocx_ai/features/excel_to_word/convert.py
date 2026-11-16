"""Excel to Word: every sheet of a workbook becomes a heading and a real Word table.

The cells are copied by code, never retyped by the model: a workbook already holds the
exact data, so reading it again would only add mistakes. Values come out as Excel last
calculated them (a formula gives its result), numbers keep their thousands separators,
decimals and percent signs, dates read DD-MM-YYYY, merged cells stay merged and bold
cells stay bold. Charts and pictures are not copied.
"""

import io
import zipfile
from datetime import date, datetime, time
from pathlib import Path

import docx
import openpyxl
from docx.enum.section import WD_ORIENT
from docx.shared import Pt

from core import store
from core.pdfutil import ToolError

MAX_SHEETS = 20
MAX_ROWS = 1000           # per sheet; more would make a Word table nobody can read
MAX_COLS = 30
WIDE = 8                  # tables wider than this get landscape pages and a smaller font
MAX_UNZIPPED_MB = 200     # an .xlsx is a zip: refuse ones that unpack to something huge
MAX_ZIP_ENTRIES = 5000


def inspect_xlsx(data: bytes) -> int:
    """Check an upload really is a safe .xlsx workbook; return its sheet count.

    Raises ValueError with a message fit for the user."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            entries = zf.infolist()
            names = {e.filename for e in entries}
    except zipfile.BadZipFile:
        raise ValueError("not a readable Excel (.xlsx) workbook") from None
    if "xl/workbook.xml" not in names:
        raise ValueError("not an Excel (.xlsx) workbook")
    if "xl/vbaProject.bin" in names:
        raise ValueError("macro-enabled workbooks are not accepted; save it as a plain .xlsx")
    if len(entries) > MAX_ZIP_ENTRIES or sum(e.file_size for e in entries) > MAX_UNZIPPED_MB * 1024 * 1024:
        raise ValueError(f"the workbook unpacks to more than {MAX_UNZIPPED_MB} MB")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
    except Exception:
        raise ValueError("not a readable Excel (.xlsx) workbook") from None
    count = len(wb.sheetnames)
    wb.close()
    return count


def excel_to_word(file_id: str, sheets: list[str] | None = None) -> dict:
    """Write a NEW .docx with each visible sheet (or the sheets named) as a heading and a table."""
    try:
        meta, path = store.get_file(file_id)
    except store.NotFound as e:
        raise ToolError(str(e))
    if meta.get("ext") != "xlsx":
        raise ToolError(f"'{meta['name']}' is not an Excel (.xlsx) workbook")

    wb = openpyxl.load_workbook(path, data_only=True)   # not read-only: merged cells need the full sheet
    chosen, hidden = _pick_sheets(wb, sheets)

    word = docx.Document()
    word.styles["Normal"].font.size = Pt(10)
    word.add_heading(Path(meta["name"]).stem, level=1)
    report = []
    for ws in chosen:
        rows, cols, truncated = _used_size(ws)
        word.add_heading(ws.title, level=2)
        if not rows:
            word.add_paragraph("(empty sheet)")
            report.append({"sheet": ws.title, "rows": 0, "columns": 0})
            continue
        _add_table(word, ws, rows, cols)
        if truncated:
            word.add_paragraph(f"Only the first {rows} rows and {cols} columns of this sheet are included.")
        report.append({"sheet": ws.title, "rows": rows, "columns": cols, "truncated": truncated})
    wb.close()

    if any(r["columns"] > WIDE for r in report):
        _landscape(word)
    out = io.BytesIO()
    word.save(out)
    new = store.save_file(out.getvalue(), f"{Path(meta['name']).stem}.docx", pages=0,
                          source="excel_to_word", parents=[file_id], ext="docx")
    result = {"file_id": new["id"], "sheets": report, "files_created": [new],
              "note": "Cell values were copied exactly by code. Formulas show their last calculated result; "
                      "charts and pictures are not copied."}
    if hidden:
        result["hidden_sheets_skipped"] = hidden
    return result


def _pick_sheets(wb, wanted: list[str] | None):
    hidden = [ws.title for ws in wb.worksheets if ws.sheet_state != "visible"]
    if wanted:
        unknown = [n for n in wanted if n not in wb.sheetnames]
        if unknown:
            raise ToolError(f"no sheet named {unknown}; the sheets are {wb.sheetnames}")
        chosen = [wb[n] for n in wanted]
    else:
        chosen = [ws for ws in wb.worksheets if ws.sheet_state == "visible"]
    if len(chosen) > MAX_SHEETS:
        raise ToolError(f"{len(chosen)} sheets is more than {MAX_SHEETS}; name the sheets you want")
    return chosen, [h for h in hidden if not wanted or h not in wanted]


def _used_size(ws) -> tuple[int, int, bool]:
    """Rows and columns that actually hold values (formatting alone doesn't count), capped."""
    last_row = last_col = 0
    for row in ws.iter_rows(max_row=min(ws.max_row, MAX_ROWS), max_col=min(ws.max_column, MAX_COLS)):
        for cell in row:
            if cell.value is not None and str(cell.value).strip():
                last_row, last_col = max(last_row, cell.row), max(last_col, cell.column)
    truncated = ws.max_row > MAX_ROWS or ws.max_column > MAX_COLS
    return last_row, last_col, truncated and last_row > 0


def _add_table(word, ws, rows: int, cols: int) -> None:
    table = word.add_table(rows=rows, cols=cols)
    table.style = "Table Grid"
    size = Pt(8) if cols > WIDE else Pt(9.5)
    cells = [row.cells for row in table.rows]
    for r, row in enumerate(ws.iter_rows(max_row=rows, max_col=cols)):
        for c, cell in enumerate(row):
            text = _display(cell)
            if not text:
                continue
            run = cells[r][c].paragraphs[0].add_run(text)
            run.font.size = size
            run.bold = bool(cell.font and cell.font.b)
    for merged in ws.merged_cells.ranges:
        top, left, bottom, right = merged.min_row, merged.min_col, merged.max_row, merged.max_col
        if top <= rows and left <= cols:
            cells[top - 1][left - 1].merge(cells[min(bottom, rows) - 1][min(right, cols) - 1])


def _display(cell) -> str:
    """The value roughly as Excel shows it: separators, decimals, percent, DD-MM-YYYY dates."""
    v = cell.value
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, datetime):
        return v.strftime("%d-%m-%Y") if v.time() == time(0) else v.strftime("%d-%m-%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%d-%m-%Y")
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, int | float):
        fmt = (cell.number_format or "General").split(";")[0]
        decimals = len(fmt.split(".")[1].split("%")[0].rstrip("_)* ").replace("#", "")) if "." in fmt else 0
        if "%" in fmt:
            return f"{v * 100:.{decimals}f}%"
        if fmt == "General":
            return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
        return f"{v:,.{decimals}f}" if "," in fmt else f"{v:.{decimals}f}"
    return str(v).strip()


def _landscape(word) -> None:
    section = word.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
