"""
word_to_pdf.py -- convert .docx to PDF, then prove nothing was lost.

DELIBERATELY SEPARATE from the PDF-to-Word pipeline. Nothing here imports
test_pdf.py, blocks.py or to_docx.py, and nothing there imports this. The two
directions share no state and cannot break each other. normalize() and squash()
are copied rather than imported for the same reason -- they are eight lines,
and the isolation is worth more than the duplication.

--------------------------------------------------------------------------
WHY THERE IS NO MODEL IN THIS FILE

PDF to Word needs a model because a PDF has no structure left. It stores
"draw the word Field at x=120 y=340" and nothing anywhere says those glyphs
form a table header -- that has to be worked out by looking at the page.

A .docx is the opposite. It states its own structure outright:

    <w:tbl>                        this is a table
    <w:tr><w:tc>                   this is a row, this is a cell
    <w:pStyle w:val="Heading1"/>   this paragraph is a heading
    <w:drawing>                    there is a picture here

Nothing needs recognising, so a model could only lose information by
re-reading what the file already declares. Conversion is a rendering job, and
LibreOffice is the reference implementation -- the same engine that opens the
file visually, so fonts, tables, images and page breaks all survive.

That leaves the real question: did everything actually make it across? That is
what the verification below is for, and it is where the work in this file is.

Usage:
    .venv/bin/python word_to_pdf.py report.docx
    .venv/bin/python word_to_pdf.py report.docx --out out/report.pdf
    .venv/bin/python word_to_pdf.py *.docx --outdir converted/
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pymupdf
from docx import Document

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# --- copied from blocks.py, on purpose: see the note at the top ------------

_PUNCT = str.maketrans({
    "‘": "'", "’": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
    "…": "...",
})


def normalize(s: str) -> str:
    """Collapse whitespace, fold typographic punctuation, lowercase."""
    return re.sub(r"\s+", " ", s.translate(_PUNCT)).strip().lower()


def squash(s: str) -> str:
    """normalize() with spaces removed, for strings differing only in spacing."""
    return normalize(s).replace(" ", "")


# ---------------------------------------------------------------------------
# CONVERSION
# ---------------------------------------------------------------------------

def find_soffice() -> str:
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    raise SystemExit(
        "LibreOffice not found. Install it with:\n"
        "    sudo apt-get install -y libreoffice-writer"
    )


def convert(docx_path: str, out_dir: str, timeout: int = 180) -> str:
    """Render a .docx to PDF with LibreOffice. Returns the output path.

    Runs against a throwaway profile directory. Without that, a LibreOffice
    window already open on the desktop makes the headless call silently attach
    to the running instance and do nothing.
    """
    soffice = find_soffice()
    os.makedirs(out_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as profile:
        result = subprocess.run(
            [soffice,
             f"-env:UserInstallation=file://{profile}",
             "--headless", "--norestore",
             "--convert-to", "pdf",
             "--outdir", out_dir,
             docx_path],
            capture_output=True, text=True, timeout=timeout,
        )

    expected = os.path.join(
        out_dir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf"
    )
    if not os.path.exists(expected):
        raise RuntimeError(
            f"conversion produced no file.\n"
            f"  stdout: {result.stdout.strip()}\n"
            f"  stderr: {result.stderr.strip()}"
        )
    return expected


# ---------------------------------------------------------------------------
# READING BOTH SIDES
# ---------------------------------------------------------------------------

def read_docx(path: str) -> dict:
    """Everything the Word file contains, as the thing to check against.

    Paragraphs and table cells are kept apart because they are checked
    differently: a paragraph must appear somewhere in the PDF, while a table's
    cells must appear AND its shape must survive.
    """
    doc = Document(path)

    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]

    tables = []
    for table in doc.tables:
        rows = [[c.text.strip() for c in row.cells] for row in table.rows]
        tables.append({
            "rows": len(table.rows),
            "cols": len(table.columns),
            "cells": [c for row in rows for c in row if c],
        })

    return {
        "paragraphs": paragraphs,
        "tables": tables,
        "images": len(doc.inline_shapes),
        "text": "\n".join(paragraphs
                          + [c for t in tables for c in t["cells"]]),
    }


def read_pdf(path: str) -> dict:
    """What actually came out the other side."""
    doc = pymupdf.open(path)
    text = "\n".join(page.get_text() for page in doc)

    images = 0
    tables = []
    for page in doc:
        images += len(page.get_images())
        try:
            for t in page.find_tables().tables:
                tables.append((t.row_count, t.col_count))
        except Exception:
            pass

    return {"text": text, "pages": doc.page_count,
            "images": images, "tables": tables}


# ---------------------------------------------------------------------------
# VERIFICATION
# ---------------------------------------------------------------------------

def verify(src: dict, out: dict):
    """Compare the Word file against the PDF it produced.

    Returns (problems, notes, coverage). The split matters: a problem means
    content is missing, a note means something differed in a way that does not
    lose anything -- LibreOffice's table detector disagreeing about row counts
    is a note, a paragraph that vanished is a problem.
    """
    problems, notes = [], []

    produced = normalize(out["text"])
    produced_squashed = squash(out["text"])

    # --- 1. word coverage -------------------------------------------------
    words = {w for w in normalize(src["text"]).split() if len(w) > 3}
    missing_words = sorted(
        w for w in words
        if w not in produced and squash(w) not in produced_squashed
    )
    coverage = 1 - len(missing_words) / len(words) if words else 1.0

    if coverage < 0.99:
        shown = ", ".join(missing_words[:8])
        more = (f" (+{len(missing_words) - 8} more)"
                if len(missing_words) > 8 else "")
        problems.append(
            f"WORDS: {coverage:.0%} coverage -- {len(missing_words)} "
            f"missing: {shown}{more}"
        )

    # --- 2. paragraph-level coverage -------------------------------------
    # Word coverage measures vocabulary, which hides a dropped line whose
    # words appear elsewhere. Whole paragraphs are either present or not.
    missing_paras = []
    for para in src["paragraphs"]:
        key = normalize(para)
        if len(key) < 8:
            continue
        if key not in produced and squash(para) not in produced_squashed:
            missing_paras.append(para)

    if missing_paras:
        shown = "; ".join(repr(p[:45]) for p in missing_paras[:4])
        more = (f" (+{len(missing_paras) - 4} more)"
                if len(missing_paras) > 4 else "")
        problems.append(
            f"PARAGRAPHS: {len(missing_paras)} missing entirely: {shown}{more}"
        )

    # --- 3. table cells ---------------------------------------------------
    total_cells = missing_cells = 0
    for i, table in enumerate(src["tables"]):
        gone = [c for c in table["cells"]
                if normalize(c) not in produced
                and squash(c) not in produced_squashed]
        total_cells += len(table["cells"])
        missing_cells += len(gone)
        if gone:
            shown = ", ".join(repr(c[:22]) for c in gone[:4])
            more = f" (+{len(gone) - 4} more)" if len(gone) > 4 else ""
            problems.append(
                f"TABLE {i} ({table['rows']}x{table['cols']}): "
                f"{len(gone)} cell(s) missing: {shown}{more}"
            )

    # Shape is advisory: find_tables() is a detector, not ground truth. It
    # routinely reports a phantom trailing row or misses a borderless table,
    # so a count mismatch means "look", not "content was lost".
    if len(out["tables"]) != len(src["tables"]):
        notes.append(
            f"table COUNT differs: {len(src['tables'])} in the .docx, "
            f"{len(out['tables'])} detected in the PDF (detector is "
            f"unreliable -- cell coverage above is what decides)"
        )

    # --- 4. images --------------------------------------------------------
    if src["images"] != out["images"]:
        if out["images"] < src["images"]:
            problems.append(
                f"IMAGES: {src['images']} in the .docx but "
                f"{out['images']} in the PDF"
            )
        else:
            notes.append(
                f"PDF has {out['images']} images against {src['images']} in "
                f"the .docx -- LibreOffice may split or re-encode graphics"
            )

    return problems, notes, coverage, {
        "cells": (total_cells - missing_cells, total_cells),
        "paragraphs": (len(src["paragraphs"]) - len(missing_paras),
                       len(src["paragraphs"])),
    }


# ---------------------------------------------------------------------------

def run(docx_path: str, out_dir: str, keep_going: bool) -> bool:
    name = os.path.basename(docx_path)
    print("=" * 72)
    print(name)
    print("=" * 72)

    src = read_docx(docx_path)
    print(f"  .docx: {len(src['paragraphs'])} paragraphs, "
          f"{len(src['tables'])} table(s), {src['images']} image(s)")

    try:
        pdf_path = convert(docx_path, out_dir)
    except Exception as exc:
        print(f"  CONVERSION FAILED: {exc}")
        return False

    out = read_pdf(pdf_path)
    size = os.path.getsize(pdf_path)
    print(f"  PDF  : {out['pages']} page(s), {out['images']} image(s), "
          f"{size / 1024:.0f} KB -> {pdf_path}")

    problems, notes, coverage, stats = verify(src, out)

    print()
    print(f"  word coverage      {coverage:.1%}")
    print(f"  paragraphs kept    {stats['paragraphs'][0]}/{stats['paragraphs'][1]}")
    print(f"  table cells kept   {stats['cells'][0]}/{stats['cells'][1]}")
    print(f"  verdict            {'PASS' if not problems else 'FAIL'}")

    for p in problems:
        print(f"    - {p}")
    for n in notes:
        print(f"    ~ {n}")
    print()
    return not problems


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("files", nargs="+", help=".docx file(s), globs allowed")
parser.add_argument("--outdir", default="pdf_out", help="where PDFs go")
parser.add_argument("--out", help="exact output path (single input only)")
args = parser.parse_args()

paths = []
for pattern in args.files:
    paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[")
                 else [pattern])

if args.out and len(paths) > 1:
    raise SystemExit("--out takes a single input file; use --outdir for many")

ok = fail = 0
for path in paths:
    if not os.path.exists(path):
        print(f"skipping {path}: not found")
        continue
    target = os.path.dirname(args.out) or "." if args.out else args.outdir
    if run(path, target, keep_going=True):
        ok += 1
    else:
        fail += 1
    if args.out:
        produced = os.path.join(
            target, os.path.splitext(os.path.basename(path))[0] + ".pdf")
        if os.path.exists(produced) and produced != args.out:
            shutil.move(produced, args.out)
            print(f"  moved to {args.out}\n")

if len(paths) > 1:
    print("=" * 72)
    print(f"{ok} passed, {fail} failed, {len(paths)} total")

sys.exit(1 if fail else 0)
