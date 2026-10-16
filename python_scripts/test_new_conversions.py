"""
test_new_conversions.py -- run the ten newer conversions on the local 2B model and say PASS / FAIL for each.

    .venv/Scripts/python python_scripts/test_new_conversions.py                  all ten, one sample file each
    .venv/Scripts/python python_scripts/test_new_conversions.py --list           what exists and what each case checks
    .venv/Scripts/python python_scripts/test_new_conversions.py --only image_to_csv,pdf_to_xml
    .venv/Scripts/python python_scripts/test_new_conversions.py --all-samples    every file in each input folder (slow)

For every case the stale output is deleted first (an old file can never make a case pass), the conversion script
is run, and the file it wrote is OPENED AGAIN and checked: it parses, it has the right shape, and the words that
must be in it are in it. For PDF and Word inputs "the words that must be in it" are the source document's own
words (the project's 4-letter-or-longer coverage rule); for pictures a fixed list. A CSV holds text as one
line per row and tables as rows and columns, so a picture with no table (image_to_csv_text) still gives a CSV.

A case reports SKIP, not PASS, when a tool it needs is missing (LibreOffice for the Word cases). The exit code
is 1 if anything FAILED.

The model server must be up first:
    ~\\.local\\opt\\llama.cpp\\llama-b11247\\llama-server.exe --model models_manual\\Qwen3VL-2B-Instruct-Q4_K_M.gguf
        --mmproj models_manual\\mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf --port 8090 --host 127.0.0.1 --no-webui
        --ctx-size 8192 --image-min-tokens 512 --image-max-tokens 512 --parallel 1
(see docs/new_conversions.md).
"""

import argparse
import csv
import glob
import os
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent                     # the project root
sys.path.insert(0, str(SCRIPTS))
from blocks import LOCAL_BASE_URL, normalize, squash  # noqa: E402

# id, script, sample, output name pattern, kind of file, source of the must-be-present words, extra
CASES = [
    dict(id="image_to_ppt", script="image_to_ppt.py", sample="hi.png", out="hi.pptx", kind="pptx",
         words=["hello", "demo"], note="picture of two lines -> PowerPoint"),
    dict(id="image_to_xml", script="image_to_xml.py", sample="hi.png", out="hi.xml", kind="xml",
         words=["hello", "demo"], note="picture of two lines -> XML"),
    dict(id="image_to_csv", script="image_to_csv.py", sample="demo2.png", out="demo2.csv", kind="csv",
         words=["repair", "restores", "placement"], cols=4, rows=3,
         note="picture with text and a table -> CSV: text lines plus a 4-column table"),
    dict(id="image_to_csv_text", script="image_to_csv.py", sample="hi.png", out="hi.csv", kind="csv",
         dir="image_to_csv", src="input/image_to_word_input/hi.png", words=["hello", "demo"], textonly=True,
         note="picture with NO table -> CSV of its text lines, one cell per row"),
    dict(id="pdf_to_xml", script="pdf_to_xml.py", sample="sample.pdf", out="sample.xml", kind="xml", ground="pdf",
         note="digital PDF -> XML, checked against the PDF's own text"),
    dict(id="pdf_to_csv", script="pdf_to_csv.py", sample="sample.pdf", out="sample.csv", kind="csv", ground="pdf",
         note="PDF with text and a table -> CSV, checked against the PDF's own text"),
    dict(id="word_to_xlsx", script="word_to_xlsx.py", sample="price_list.docx", out="price_list.xlsx", kind="xlsx",
         words=["notebook", "nb-01", "4.50", "stapler"], needs="soffice", note="Word table -> Excel"),
    dict(id="word_to_xml", script="word_to_xml.py", sample="sample_report.docx", out="sample_report.xml", kind="xml",
         ground="docx", needs="soffice", note="Word report -> XML, checked against the .docx's own text"),
    dict(id="word_to_ppt", script="word_to_ppt.py", sample="sample_report.docx", out="sample_report.pptx", kind="pptx",
         ground="docx", needs="soffice", note="Word report -> PowerPoint, checked against the .docx's own text"),
    dict(id="word_to_csv", script="word_to_csv.py", sample="price_list.docx", out="price_list.csv", kind="csv",
         words=["price", "notebook", "nb-01", "4.50", "stapler"], cols=3, rows=5, needs="soffice",
         note="Word heading and table -> CSV"),
    dict(id="word_to_image", script="word_to_image.py", sample="sample_report.docx", out="sample_report_page1.png",
         kind="png", needs="soffice", nomodel=True, note="Word -> one PNG per page (rendering only, no model)"),
]


def tool_ok(name):
    if name != "soffice":
        return True
    import doc_pipeline
    try:
        doc_pipeline.find_soffice()
        return True
    except SystemExit:
        return False


def server_up():
    try:
        return urllib.request.urlopen(LOCAL_BASE_URL + "/health", timeout=3).status == 200
    except Exception:
        return False


# --------------------------------------------------------------------------- reading the files back
def read_text(path, kind):
    """All the text in a written file, read back from disk."""
    if kind == "csv":
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return " ".join(c for row in csv.reader(fh) for c in row)
    if kind == "xml":
        return " ".join(e.text for e in ET.parse(path).getroot().iter() if e.text and e.text.strip())
    if kind == "xlsx":
        from openpyxl import load_workbook
        from to_xlsx import cell_display      # a number is stored as a number but SHOWN as printed (4.50)
        wb = load_workbook(path, data_only=True)
        out = [cell_display(c.value, c.number_format) for ws in wb.worksheets for row in ws.iter_rows() for c in row
               if c.value is not None]
        wb.close()
        return " ".join(out)
    if kind == "pptx":
        from pptx import Presentation
        parts = []
        for slide in Presentation(path).slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    parts.append(shape.text_frame.text)
                if shape.has_table:
                    parts.extend(cell.text for row in shape.table.rows for cell in row.cells)
        return " ".join(parts)
    raise ValueError(kind)


def ground_text(case, sample_path):
    if case.get("ground") == "pdf":
        import pymupdf
        with pymupdf.open(sample_path) as doc:
            return "\n".join(p.get_text() for p in doc)
    from docx import Document
    d = Document(sample_path)
    parts = [p.text for p in d.paragraphs]
    parts += [c.text for t in d.tables for row in t.rows for c in row.cells]
    return "\n".join(parts)


def recall(written, truth):
    words = {w for w in normalize(truth).split() if len(w) > 3}
    if not words:
        return 1.0, []
    w, ws = normalize(written), squash(written)
    missing = sorted(x for x in words if x not in w and squash(x) not in ws)
    return 1 - len(missing) / len(words), missing


# --------------------------------------------------------------------------- one case
def check(case, out_path, sample_path):
    """Returns (ok, [lines])."""
    lines = []
    kind = case["kind"]
    if kind == "png":
        from PIL import Image
        stem = os.path.splitext(os.path.basename(sample_path))[0]
        pngs = sorted(glob.glob(os.path.join(os.path.dirname(out_path), f"{stem}_page*.png")))
        if not pngs:
            return False, ["no page PNG written"]
        for p in pngs:
            with Image.open(p) as im:
                im.verify()
        return True, [f"{len(pngs)} page image(s), all valid PNGs"]

    if not os.path.exists(out_path):
        return False, [f"{os.path.basename(out_path)} was not written"]
    try:
        written = read_text(out_path, kind)
    except Exception as exc:
        return False, [f"the file does not open: {exc}"]
    ok = True
    if not written.strip():
        return False, ["the file opens but holds no text"]
    lines.append(f"opens, {len(written.split())} words")

    if kind == "csv":
        with open(out_path, newline="", encoding="utf-8-sig") as fh:
            rows = [r for r in csv.reader(fh) if any(c.strip() for c in r)]
        lines.append(f"{len(rows)} rows (a text line is 1 cell, a table row is as wide as the table)")
        if "cols" in case:
            table_rows = [r for r in rows if len(r) == case["cols"]]
            lines.append(f"{len(table_rows)} rows of {case['cols']} columns")
            if "rows" in case and len(table_rows) != case["rows"]:
                ok = False
                lines.append(f"expected {case['rows']} rows of {case['cols']} columns")
        if case.get("textonly") and any(len(r) != 1 for r in rows):
            ok = False
            lines.append("a picture with no table should give one cell per row")
    if kind == "xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(out_path)
        lines.append(f"{sum(ws.max_row for ws in wb.worksheets)} rows in {len(wb.worksheets)} sheet(s)")
        wb.close()
    if kind == "pptx":
        from pptx import Presentation
        n = len(Presentation(out_path).slides)
        lines.append(f"{n} slide(s)")
        ok = ok and n >= 1
    if kind == "xml":
        root = ET.parse(out_path).getroot()
        pages = root.findall("page")
        lines.append(f"well-formed, {len(pages)} <page>, {len(list(root.iter('table')))} <table>")
        ok = ok and len(pages) >= 1

    if "words" in case:
        missing = [w for w in case["words"] if squash(w) not in squash(written)]
        lines.append("expected words: " + ("all present" if not missing else f"MISSING {missing}"))
        ok = ok and not missing
    if case.get("ground"):
        truth = ground_text(case, sample_path)
        share, missing = recall(written, truth)
        lines.append(f"{share:.0%} of the source's words are in the file" + (f"; missing: {missing[:6]}" if missing else ""))
        ok = ok and share >= 0.9
    return ok, lines


def run_case(case, sample_path, verbose):
    folder = case.get("dir", case["id"])
    out_dir = REPO / "output" / f"{folder}_output"
    out_path = str(out_dir / case["out"]) if case["kind"] != "png" else str(out_dir / case["out"])
    # a stale output must never make a case pass
    stem = os.path.splitext(os.path.basename(sample_path))[0]
    for stale in glob.glob(str(out_dir / f"{stem}*")):
        if os.path.isfile(stale) and not stale.endswith(".gitkeep"):
            os.remove(stale)
    cmd = [sys.executable, "-u", str(SCRIPTS / case["script"]), str(sample_path)]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    started = time.time()
    proc = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    secs = time.time() - started
    if verbose:
        print(proc.stdout[-3000:], proc.stderr[-1500:])
    if proc.returncode != 0:
        tail = [l for l in (proc.stdout + proc.stderr).strip().splitlines() if l.strip()][-4:]
        return False, secs, [f"script exited {proc.returncode}"] + [f"  {l[:140]}" for l in tail]
    ok, lines = check(case, out_path, str(sample_path))
    return ok, secs, lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all-samples", action="store_true", help="every file in each input folder, not just one")
    ap.add_argument("--verbose", action="store_true", help="show each script's own output")
    a = ap.parse_args()

    if a.list:
        for c in CASES:
            print(f"{c['id']:<22} {c['script']:<18} {c['sample']:<20} {c['note']}")
        return 0

    wanted = set(a.only.split(",")) if a.only else None
    cases = [c for c in CASES if not wanted or c["id"] in wanted]
    if wanted and len(cases) != len(wanted):
        print("unknown case id(s):", ", ".join(sorted(wanted - {c['id'] for c in CASES})))
        return 2

    subprocess.run([sys.executable, str(SCRIPTS / "make_word_samples.py")], cwd=REPO, capture_output=True)
    model_needed = any(not c.get("nomodel") for c in cases)
    if model_needed and not server_up():
        print(f"The model server is not answering at {LOCAL_BASE_URL}.")
        print("Start it first (command in the header of this file and in docs/new_conversions.md).")
        return 2

    results = []
    for c in cases:
        if not tool_ok(c.get("needs", "")):
            results.append((c["id"], "SKIP", 0, [f"needs {c['needs']} (LibreOffice), which is not installed"]))
            print(f"[SKIP] {c['id']}")
            continue
        folder = c.get("dir", c["id"])
        samples = [c["sample"]]
        if a.all_samples:
            ext = os.path.splitext(c["sample"])[1]
            kinds = (".png", ".jpg", ".jpeg") if ext in (".png", ".jpg") else (ext,)
            samples = sorted(os.path.basename(p) for p in glob.glob(str(REPO / "input" / f"{folder}_input" / "*"))
                             if p.lower().endswith(kinds))
        for s in samples:
            case = dict(c, out=os.path.splitext(s)[0] + os.path.splitext(c["out"])[1]) if s != c["sample"] else c
            if case["kind"] == "png":
                case = dict(case, out=os.path.splitext(s)[0] + "_page1.png")
            path = REPO / c["src"] if c.get("src") else REPO / "input" / f"{folder}_input" / s
            print(f"[run ] {c['id']}  ({s})", flush=True)
            ok, secs, lines = run_case(case, path, a.verbose)
            results.append((f"{c['id']}:{s}" if len(samples) > 1 else c["id"], "PASS" if ok else "FAIL", secs, lines))
            print(f"[{'PASS' if ok else 'FAIL'}] {c['id']}  ({s})  {secs:.0f}s")

    print("\n" + "=" * 78)
    print(f"{'conversion':<30}{'result':<8}{'time':>7}   details")
    print("-" * 78)
    for name, res, secs, lines in results:
        print(f"{name:<30}{res:<8}{secs:>6.0f}s   {lines[0] if lines else ''}")
        for extra in lines[1:]:
            print(f"{'':<46}{extra}")
    n = lambda r: sum(1 for x in results if x[1] == r)
    print("=" * 78)
    print(f"{n('PASS')} passed, {n('FAIL')} failed, {n('SKIP')} skipped, {len(results)} total")
    return 1 if n("FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
