"""
pdf_to_txt.py -- convert a PDF into a plain-text (.txt) file, page by page.

For every page that HAS A TEXT LAYER, the vision model reads the page image and
returns it as structured blocks (headings, paragraphs, lists, tables), exactly
as in test_pdf.py. Those blocks are then written as text: headings and
paragraphs as lines, lists as "- item" lines, and tables as columns padded with
spaces so they still line up in any editor.

WHY A MODEL, WHEN THE TEXT LAYER ALREADY HOLDS THE WORDS? The text layer is exact
but flat: it knows the characters, not which of them form a table, a heading or
a bullet list, and reading order across columns is a guess. The model supplies
that structure. The text layer stays the GROUND TRUTH: the model's output is
checked against it, and lines the model dropped are put back from it.

WHAT THE MODEL MUST NOT BE TRUSTED WITH, AND HOW THAT IS COVERED:

  dropped text    the model sometimes skips a line. check_coverage() finds it and
                  repair_missing_lines() restores it from the PDF's own characters.
  invented text   the model can misread or invent. The final file is checked word
                  by word against the text layer and any word that is not in the
                  PDF is reported.
  a failed call   the page is NOT silently skipped (test_pdf.py does skip it). The
                  page falls back to the PDF's own text, and the report says so.

SCANNED PAGES (no text layer) are read by the model too, but there is nothing to
check them against and nothing to restore from, so they are reported as
UNVERIFIED and never as verified. Tesseract OCR gives an independent reading and
its agreement with the model is printed as information only: OCR misses text
printed white on dark backgrounds and turns accented letters into plain ones, so
it can not fail a page. A page the model fails on is NOT quietly dropped: a line
saying so is written in its place and the run fails. A page with no text and
nothing else on it is simply blank and is written blank.

MISREADS ARE CORRECTED FROM THE TEXT LAYER (digital pages). blocks.
snap_to_text_layer() matches each block to the passage of the PDF's own text it was
read from and, when the match is close, replaces the block with the PDF's exact
words. This removes misreads, and it stops the repair step from inserting a second,
correct copy beside a misread paragraph (which made pages 1.4x too long).

Output:   pdf_to_txt_input/report.pdf  ->  pdf_to_txt_output/report.txt
Pages are separated by a line reading  ----- Page N -----

HOW IT PROVES EVERYTHING WAS EXTRACTED. The saved .txt is read back, split into
pages at the markers, and every page is compared with the PDF's text layer
(see verify_txt()):

  1. PAGES        every page of the PDF has its marker, in order.
  2. COVERAGE     each word (over 3 characters) in the PDF's page is in the file.
  3. NO EXTRAS    each word in the file's page is in the PDF's page.
  4. LINES        each line of the PDF's page is present in the file.
  5. SIZE         the file's word count is close to the PDF's (catches repeats).
  6. FIGURES      any number that differs, in either direction, fails the page.
  (1-6 apply to digital pages. Scanned pages get 1 plus an advisory OCR figure.)

Usage:
    .venv/bin/python pdf_to_txt.py pdf_to_txt_input/report.pdf --base-url http://127.0.0.1:8090
    .venv/bin/python pdf_to_txt.py pdf_to_txt_input/*.pdf --pages 1-3
"""

import argparse
import glob
import os
import re
import sys
import tempfile
import time

import pymupdf

import ocr
from blocks import (
    DEFAULT_BASE_URL,
    LOCAL_BASE_URL,
    Page,
    TableBlock,
    TextBlock,
    check_coverage,
    check_structure,
    drop_duplicate_blocks,
    extract_page,
    fix_trailing_heading_after_table,
    merge_nested_tables,
    normalize,
    snap_to_text_layer,
    squash,
    ungrounded_table_blocks,
    ungrounded_text_blocks,
)

PDF_DIR = "pdf_to_txt_input"
TXT_OUT_DIR = "pdf_to_txt_output"

MARKER = "----- Page {n} -----"
MARKER_RE = re.compile(r"^----- Page (\d+) -----$", re.M)

# Pass thresholds for the read-back check. The same 95% the project already uses
# for coverage (check_coverage's default); extras get the same tolerance.
COVERAGE_MIN = 0.95
EXTRAS_MAX = 0.05
SIZE_MAX = 1.15      # file words / PDF words; above this, content is duplicated
SIZE_MIN = 0.85


def parse_pages(spec: str, page_count: int):
    """'all', '2', '1-3' or '1,3-4' -> zero-based page indexes, in order."""
    if spec in ("all", ""):
        return list(range(page_count))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, int(b)))
        else:
            out.append(int(part) - 1)
    return [p for p in out if 0 <= p < page_count]


# --- copied from test_pdf.py, on purpose ------------------------------------
# test_pdf.py is a SCRIPT (it runs its whole job at import), so it cannot be
# imported. The project's convention, stated in word_to_pdf.py, is to copy the
# small reusable pieces rather than import across scripts. These two are the
# repairs that put back what the model dropped, taken from the PDF's own text.

def repair_missing_lines(page, pdf_page, missing_lines):
    """Put back lines the model dropped, taken from the PDF's own text.

    Same principle as the images: the content was never missing from the
    DOCUMENT, only from the model's answer. A dropped line is sitting in the
    text layer with its exact characters, its position and its font size, so
    restoring it needs no second model call and cannot produce a different
    wrong answer -- it is a copy, not another reading.

    Nothing here is specific to any document. A line's kind comes from its
    size relative to the page's dominant body size, and its position from how
    much text sits above it -- the same two generic measurements used
    elsewhere. Whatever PDF arrives, the rules are identical.

    Lines that fall inside a detected table are NOT restored as paragraphs:
    loose cell text dumped into the prose would be worse than the gap. Those
    are reported instead, so a dropped table row stays visible as a real fault.

    Returns (new_page, repairs, skipped).
    """
    if not missing_lines:
        return page, [], []

    wanted = {normalize(m): m for m in missing_lines}

    # Every line in the text layer, with where it sits and how big it is.
    lines = []
    sizes = {}
    for block in pdf_page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            text = "".join(s["text"] for s in line["spans"]).strip()
            if not text:
                continue
            size = max((s["size"] for s in line["spans"]), default=10)
            lines.append({"text": text, "y0": line["bbox"][1], "size": size,
                          "bbox": line["bbox"]})
            sizes[round(size)] = sizes.get(round(size), 0) + len(text)

    body = max(sizes, key=sizes.get) if sizes else 10

    try:
        table_boxes = [pymupdf.Rect(t.bbox) for t in pdf_page.find_tables().tables]
    except Exception:
        table_boxes = []

    total_chars = sum(len(l["text"]) for l in lines) or 1
    repairs, skipped, to_insert = [], [], []

    for line in lines:
        key = normalize(line["text"])
        if key not in wanted:
            continue
        del wanted[key]                      # restore each line once

        rect = pymupdf.Rect(line["bbox"])
        if any(abs(rect & tb) / max(abs(rect), 1) > 0.5 for tb in table_boxes):
            skipped.append(line["text"])
            continue

        size = line["size"]
        if size >= body * 1.25:
            kind = "heading"
        elif size <= body * 0.88:
            kind = "caption"
        else:
            kind = "paragraph"

        above = sum(len(l["text"]) for l in lines if l["y0"] < line["y0"])
        to_insert.append({
            "frac": above / total_chars,
            # A restored line's appearance is MEASURED rather than observed --
            # this path only runs on digital PDFs, where the text layer gives
            # the real font size and the real x-position. No guessing needed.
            "block": TextBlock(
                kind=kind,
                align=("center" if abs(
                    (line["bbox"][0] + line["bbox"][2]) / 2
                    - pdf_page.rect.width / 2) < 20 else "left"),
                size=("large" if size >= body * 1.25
                      else "small" if size <= body * 0.88 else "normal"),
                bold=False,
                text=line["text"],
            ),
            "note": f"{line['text'][:50]!r} ({size:.0f}pt vs {body}pt body "
                    f"-> {kind})",
        })

    if not to_insert:
        return page, [], skipped

    blocks = list(page.blocks)
    # Insert from the bottom up so earlier indices stay valid.
    for item in sorted(to_insert, key=lambda d: d["frac"], reverse=True):
        idx = min(len(blocks), round(item["frac"] * len(blocks)))
        blocks.insert(idx, item["block"])
        repairs.append(f"{item['note']} inserted at position {idx}")

    return Page(analysis=page.analysis, blocks=blocks), repairs, skipped


def repair_table_rows(page, pdf_page):
    """Put back table rows the model dropped, read from the PDF's own cells.

    The line-level coverage check cannot see this. A table's cells sit in the
    text layer as separate short lines -- "Sales", "9", "9", "0" -- and the
    line check skips anything under 8 characters to avoid page numbers, so a
    whole missing row scores as a couple of absent words and passes. Measured:
    dropping an entire row left coverage at 98% and missing_lines empty.

    So rows are compared as rows. Cell text is read by clipping the text layer
    to each cell rectangle, which is exact -- PyMuPDF's own table extractor
    garbles it ("Form fei lds", "confrimatio") while clipping does not.

    Generic for any PDF: nothing here knows what the table is about, only
    which rows the detector found and which of them the model returned.

    Returns (new_page, repairs).
    """
    try:
        detected = sorted(pdf_page.find_tables().tables, key=lambda t: t.bbox[1])
    except Exception:
        return page, []
    if not detected:
        return page, []

    model_tables = [(i, b) for i, b in enumerate(page.blocks)
                    if isinstance(b, TableBlock)]
    if not model_tables:
        return page, []

    def cell_text(rect):
        if rect is None:
            return ""
        return " ".join(
            pdf_page.get_text("text", clip=pymupdf.Rect(rect)).split()
        )

    repairs = []
    blocks = list(page.blocks)

    for (bidx, mt), dt in zip(model_tables, detected):
        grid = [[cell_text(c) for c in row.cells] for row in dt.rows]
        while grid and not any(c for c in grid[-1]):
            grid.pop()                      # detector's phantom trailing row
        if not grid:
            continue

        data_rows = grid[1:] if mt.has_header else grid
        have = {normalize(" ".join(r)) for r in mt.rows}

        rebuilt, added = [], 0
        for src_row in data_rows:
            key = normalize(" ".join(src_row))
            if not key:
                continue
            if key in have:
                continue
            # Missing. Rebuild it to the model's column count so the grid
            # stays rectangular -- a ragged row breaks the Word table.
            row = (list(src_row) + [""] * mt.n_cols)[:mt.n_cols]
            rebuilt.append((data_rows.index(src_row), row))
            added += 1

        if not rebuilt:
            continue

        rows = list(mt.rows)
        for pos, row in rebuilt:
            rows.insert(min(pos, len(rows)), row)
            repairs.append(
                f"table row {row[:3]}... restored at row {min(pos, len(rows))}"
            )

        blocks[bidx] = TableBlock(
            kind="table", has_header=mt.has_header, n_data_rows=len(rows),
            n_cols=mt.n_cols, header=mt.header, rows=rows,
        )

    if not repairs:
        return page, []
    return Page(analysis=page.analysis, blocks=blocks), repairs


# ---------------------------------------------------------------------------


def page_kind(pdf_page) -> str:
    """'digital' (has a text layer), 'blank' (nothing at all) or 'scanned'."""
    if pdf_page.get_text().strip():
        return "digital"
    has_content = bool(pdf_page.get_images(full=True)) or bool(pdf_page.get_drawings())
    return "scanned" if has_content else "blank"


def _table_lines(block: TableBlock):
    """A table as text: columns padded with spaces so they line up."""
    width = len(block.header) if block.has_header else (
        len(block.rows[0]) if block.rows else 0)
    if width == 0:
        return []
    def cell(v):
        return " ".join(str(v).split())          # a cell is one line of text
    # The model sometimes says "has a header" and then returns four empty
    # labels; that would print a blank line and a rule of dashes for nothing.
    has_header = block.has_header and any(str(c).strip() for c in block.header)
    grid = []
    if has_header:
        grid.append([cell(c) for c in block.header[:width]])
    for row in block.rows:
        grid.append([cell(c) for c in (list(row) + [""] * width)[:width]])
    widths = [max(len(r[i]) for r in grid) for i in range(width)]
    lines = ["  ".join(r[i].ljust(widths[i]) for i in range(width)).rstrip()
             for r in grid]
    if has_header:
        lines.insert(1, "  ".join("-" * widths[i] for i in range(width)))
    return lines


def render_block(block) -> str:
    """One block as plain text."""
    if isinstance(block, TableBlock):
        return "\n".join(_table_lines(block))
    if block.kind == "list":
        # Strip any bullet the model already wrote before adding one, so a list
        # never comes out as "- - item".
        return "\n".join(
            f"- {l.strip().lstrip('-*•●‣ \t').strip()}"
            for l in block.text.splitlines() if l.strip())
    return block.text.strip()


def render_page(page: Page) -> str:
    return "\n\n".join(t for t in (render_block(b) for b in page.blocks) if t.strip())


def extract_digital_page(pdf_page, img_path: str, base_url: str, model: str):
    """The model reading of one digital page, corrected and repaired.

    Returns (page, notes, timing). Raises if the model call fails; the caller
    decides what to do about that.
    """
    notes = []
    page, timing = extract_page(img_path, model=model, return_timing=True,
                                base_url=base_url, include_look=False)

    page, dupes = drop_duplicate_blocks(page, source_text=pdf_page.get_text())
    if dupes:
        notes.append(f"dropped {len(dupes)} block(s) the model repeated")
    page, fixed = fix_trailing_heading_after_table(page)
    if fixed:
        notes.append("moved a trailing heading back in front of its table")
    page, merges = merge_nested_tables(page)
    for m in merges:
        notes.append(m)

    source_text = pdf_page.get_text()

    # Text the model read OUT OF A PICTURE (a logo, say) is not in the PDF's text
    # layer; it would be a wrong duplicate of the picture. Only when the page
    # actually has pictures, exactly as test_pdf.py does.
    if pdf_page.get_images(full=True):
        bogus = set(ungrounded_text_blocks(page, source_text)) \
            | set(ungrounded_table_blocks(page, source_text))
        if bogus:
            notes.append(f"dropped {len(bogus)} block(s) read from a picture, "
                         f"not in the PDF's text")
            page = Page(analysis=page.analysis,
                        blocks=[b for i, b in enumerate(page.blocks)
                                if i not in bogus])

    # Correct the model's misreads from the PDF's own text BEFORE the coverage
    # check, so a misread paragraph is repaired in place instead of being left
    # beside a re-inserted exact copy.
    page, snaps = snap_to_text_layer(page, source_text)
    if snaps:
        b, a = snaps[0]
        notes.append(f"corrected {len(snaps)} misread passage(s)/cell(s) to the "
                     f"PDF's exact text (e.g. {b[:35]!r} -> {a[:35]!r})")

    problems, _ = check_structure(page)
    for p in problems:
        notes.append(f"model's own check: {p}")

    _, _, _, missing_lines = check_coverage(page, source_text)
    if missing_lines:
        page, repairs, skipped = repair_missing_lines(page, pdf_page, missing_lines)
        if repairs:
            notes.append(f"restored {len(repairs)} dropped line(s) from the PDF's text")
    page, row_repairs = repair_table_rows(page, pdf_page)
    if row_repairs:
        notes.append(f"restored {len(row_repairs)} dropped table row(s) from the PDF")
    return page, notes, timing


def extract_scanned_page(img_path: str, base_url: str, model: str):
    """The model reading of one SCANNED page. Nothing to check it against.

    Same model call and the same structure-only corrections as a digital page,
    but no snap, no coverage check and no repair: those all need the text layer.
    Returns (page, notes, timing); raises if the model call fails.
    """
    notes = []
    page, timing = extract_page(img_path, model=model, return_timing=True,
                                base_url=base_url, include_look=False)
    page, dupes = drop_duplicate_blocks(page)
    if dupes:
        notes.append(f"dropped {len(dupes)} block(s) the model repeated")
    page, fixed = fix_trailing_heading_after_table(page)
    if fixed:
        notes.append("moved a trailing heading back in front of its table")
    page, merges = merge_nested_tables(page)
    notes.extend(merges)
    problems, _ = check_structure(page)
    for p in problems:
        notes.append(f"model's own check: {p}")
    return page, notes, timing


def fallback_text(pdf_page) -> str:
    """The PDF's own text for a page, in reading order. Used only when the model
    call for that page fails, so the page is never silently missing."""
    return pdf_page.get_text("text", sort=True).strip()


def _tokens(text: str):
    """Every counted word, repeats included (unlike _words, which is a set)."""
    return [w for w in normalize(text).split()
            if len(w) > 3 and any(c.isalnum() for c in w)]


def _words(text: str):
    # Words of more than 3 characters that contain a letter or digit. A run of
    # dashes (the rule drawn under a table header) or other bare punctuation is
    # formatting, not text, and must not count as a word the PDF lacks.
    return {w for w in normalize(text).split()
            if len(w) > 3 and any(c.isalnum() for c in w)}


UNREADABLE = "[Page {n} could not be read:"


def verify_txt(txt_path: str, doc, wanted, ocr_texts=None):
    """Read the saved .txt back and check every converted page against the PDF.

    wanted is a list of (page_number, kind): 'digital' pages are checked against
    the PDF's text layer; 'blank' pages need only their marker; 'scanned' pages
    have no ground truth, so they are only checked for being present and not
    empty, plus an advisory comparison with ocr_texts[page_number] if given.
    Returns (results, problems) where results is one dict per page.
    """
    ocr_texts = ocr_texts or {}
    text = open(txt_path, encoding="utf-8").read()
    marks = list(MARKER_RE.finditer(text))
    sections = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        sections[int(m.group(1))] = text[m.end():end]

    found = [int(m.group(1)) for m in marks]
    expected = [n for n, _ in wanted]
    problems = []
    if found != expected:
        problems.append(f"page markers {found} but expected {expected}")

    results = []
    for n, kind in wanted:
        r = {"page": n, "kind": kind, "problems": []}
        if n not in sections:
            r["problems"].append("page is missing from the file")
            results.append(r)
            continue
        got_text = sections[n]
        r["chars"] = len(got_text.strip())
        if kind == "blank":
            results.append(r)
            continue

        if kind == "scanned":
            body = got_text.strip()
            if body.startswith(UNREADABLE.format(n=n)):
                r["problems"].append("the model could not read this page "
                                     "(a notice was written in its place)")
            read = ocr_texts.get(n, "")
            o_words = _words(read)
            got_words = _words(got_text)
            r["ocr_words"] = len(o_words)
            r["unverified"] = True
            if not body:
                r["problems"].append("the page came out empty")
            elif o_words:
                o_n, o_sq = normalize(read), squash(read)
                g_n, g_sq = normalize(got_text), squash(got_text)
                r["ocr_cov"] = 1 - len([w for w in o_words if w not in g_n
                                        and squash(w) not in g_sq]) / len(o_words)
                r["ocr_prec"] = (1 - len([w for w in got_words if w not in o_n
                                          and squash(w) not in o_sq])
                                 / len(got_words)) if got_words else 0.0
                if len(o_words) >= 15 and len(got_words) < 0.3 * len(o_words):
                    r["problems"].append(
                        f"the page has only {len(got_words)} words in the file but "
                        f"OCR reads {len(o_words)}: most of the page is missing")
            results.append(r)
            continue

        source = doc[n - 1].get_text()
        src_n, src_sq = normalize(source), squash(source)
        got_n, got_sq = normalize(got_text), squash(got_text)
        src_words, got_words = _words(source), _words(got_text)

        missing = sorted(w for w in src_words
                         if w not in got_n and squash(w) not in got_sq)
        extras = sorted(w for w in got_words
                        if w not in src_n and squash(w) not in src_sq)
        cov = 1 - len(missing) / len(src_words) if src_words else 1.0
        share = len(extras) / len(got_words) if got_words else 0.0

        gone = []
        seen = set()
        for line in source.splitlines():
            ln = normalize(line)
            if len(ln) < 8 or ln in seen:
                continue
            seen.add(ln)
            if ln not in got_n and squash(line) not in got_sq:
                gone.append(line.strip())

        # A page can contain every word and still be wrong if content is in it
        # twice. That happens when the model misreads a paragraph and the repair
        # step then adds the exact line beside it. Counting words WITH repeats
        # exposes it; the ratio of the file's words to the PDF's is 1.0 when
        # nothing is duplicated or lost.
        n_src, n_got = len(_tokens(source)), len(_tokens(got_text))
        ratio = n_got / n_src if n_src else 1.0

        r.update(coverage=cov, missing=missing, extras=extras, extra_share=share,
                 lines_missing=gone, words=len(src_words), ratio=ratio)
        if ratio > SIZE_MAX or ratio < SIZE_MIN:
            r["problems"].append(
                f"the file has {n_got} words for this page but the PDF has "
                f"{n_src} ({ratio:.2f}x) -- content is "
                f"{'repeated' if ratio > 1 else 'missing'}")
        # A percentage tolerance is fine for words, never for figures: a single
        # misread number (84,200 -> 84,700) is 2% of a page and would slip
        # under any threshold, yet it is exactly the error that matters most.
        # Any number the file has that the PDF lacks, or the reverse, fails.
        num_missing = [w for w in missing if any(c.isdigit() for c in w)]
        num_extra = [w for w in extras if any(c.isdigit() for c in w)]
        if num_missing or num_extra:
            r["problems"].append(
                "figures differ from the PDF -- in the PDF but not the file: "
                f"{', '.join(num_missing[:5]) or 'none'}; in the file but not "
                f"the PDF: {', '.join(num_extra[:5]) or 'none'}")
        if cov < COVERAGE_MIN:
            r["problems"].append(
                f"only {cov:.0%} of the PDF's words are in the file "
                f"({len(missing)} missing: {', '.join(missing[:6])})")
        if share > EXTRAS_MAX:
            r["problems"].append(
                f"{len(extras)} word(s) in the file are not in the PDF "
                f"({share:.0%}): {', '.join(extras[:6])}")
        if gone:
            r["problems"].append(
                f"{len(gone)} line(s) of the PDF are missing: "
                + "; ".join(repr(g[:40]) for g in gone[:3]))
        results.append(r)
    return results, problems


# The page image the model reads is capped in size. The server shrinks every image
# to a fixed token budget anyway, so a huge picture only costs transfer time. Some
# scanners declare the page box in pixels, which makes a Letter scan "23 inches".
MAX_SIDE_PX = 2600


def _render_dpi(pdf_page, dpi: int) -> int:
    longest_in = max(pdf_page.rect.width, pdf_page.rect.height) / 72.0
    if longest_in * dpi > MAX_SIDE_PX:
        return max(int(MAX_SIDE_PX / longest_in), 30)
    return dpi


RAW_MAX_SIDE_PX = 4000


def scan_image_for_model(pdf_page, doc, out_path: str) -> bool:
    """Save a scanned page's OWN embedded image, when that is all the page is.

    OFF BY DEFAULT (--scan-image turns it on), because the evidence points both
    ways. Rendering a scanned page resamples the scan and the server then shrinks
    it again to its fixed token budget; two resamplings blur thin marks. On the
    txt_test.pdf scan the model dropped the hyphen of "SCAN-TEST-001" on 9 of 9
    reads of renders (125, 200 and 300 dpi alike) and read it correctly on 4 of 4
    reads of the scan's own image. But on sample_scanned_document.pdf page 1 the
    same route lost a whole line on 4 of 4 reads (21 OCR words missing in total)
    against 1 of 4 for the render (3 words). A dropped line is the worse error,
    so the render stays the default until this is understood.

    ONLY WHEN THE PAGE IS EXACTLY THAT IMAGE: one image, upright (no rotation, no
    skew, no mirroring), covering at least 85% of the page, no vector drawings, no
    transparency mask, and the page itself unrotated. Otherwise a render is the
    faithful picture of the page and the caller uses it.

    Returns True if out_path was written.
    """
    try:
        images = pdf_page.get_images(full=True)
        if len(images) != 1 or pdf_page.rotation != 0 or pdf_page.get_drawings():
            return False
        xref = images[0][0]
        info = next((i for i in pdf_page.get_image_info(xrefs=True)
                     if i.get("xref") == xref), None)
        if info is None:
            return False
        a, b, c, d = info["transform"][:4]
        if abs(b) > 1e-3 or abs(c) > 1e-3 or a <= 0 or d <= 0:
            return False
        area = abs(pymupdf.Rect(info["bbox"]) & pdf_page.rect)
        if area < 0.85 * abs(pdf_page.rect):
            return False
        raw = doc.extract_image(xref)
        if not raw or raw.get("smask"):
            return False
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(raw["image"])).convert("RGB")
        if max(im.size) > RAW_MAX_SIDE_PX:
            im.thumbnail((RAW_MAX_SIDE_PX, RAW_MAX_SIDE_PX), Image.LANCZOS)
        im.save(out_path, "PNG")
        return True
    except Exception:
        return False


def run(pdf_path: str, out_dir: str, pages: str, dpi: int, base_url: str,
        model: str, use_scan_image: bool = False) -> bool:
    name = os.path.splitext(os.path.basename(pdf_path))[0]
    print("=" * 72)
    print(pdf_path)
    print("=" * 72)

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        print(f"  COULD NOT OPEN: {exc}")
        return False
    if doc.needs_pass:
        print("  SKIPPED: the PDF is password protected")
        return False

    targets = parse_pages(pages, doc.page_count)
    if not targets:
        print(f"  SKIPPED: no pages match --pages {pages!r} "
              f"(the PDF has {doc.page_count})")
        return False

    kinds = {p: page_kind(doc[p]) for p in targets}
    n_dig = sum(1 for k in kinds.values() if k == "digital")
    n_scan = sum(1 for k in kinds.values() if k == "scanned")
    n_blank = sum(1 for k in kinds.values() if k == "blank")
    print(f"  {doc.page_count} page(s), converting {len(targets)}: "
          f"{n_dig} digital, {n_scan} scanned, {n_blank} blank "
          f"(digital and scanned pages both go to the model)")
    if n_dig or n_scan:
        print(f"  model: {base_url or 'Ollama'}")
    have_ocr = ocr.have_tesseract()
    if n_scan and not have_ocr:
        print(f"  note: no OCR available ({ocr.install_hint()}) -- scanned pages "
              f"will have no OCR comparison")

    started = time.time()
    chunks = []              # the text of the file, page by page
    wanted = []              # (page number, kind) written with a marker
    fell_back = []           # digital pages where the model failed
    unreadable = []          # scanned pages where the model failed
    ocr_texts = {}           # page number -> OCR text of a scanned page
    model_time = 0.0

    with tempfile.TemporaryDirectory() as tmp:
        for p in targets:
            n = p + 1
            kind = kinds[p]
            head = MARKER.format(n=n)
            if kind == "blank":
                chunks.append(head + "\n")
                wanted.append((n, "blank"))
                print(f"  page {n}: blank page")
                continue

            img = os.path.join(tmp, f"page_{n:03d}.png")
            used_scan = (use_scan_image and kind == "scanned"
                         and scan_image_for_model(doc[p], doc, img))
            if not used_scan:
                doc[p].get_pixmap(dpi=_render_dpi(doc[p], dpi)).save(img)
            t0 = time.time()
            timing = {}
            try:
                if kind == "digital":
                    page, notes, timing = extract_digital_page(
                        doc[p], img, base_url, model)
                else:
                    page, notes, timing = extract_scanned_page(img, base_url, model)
                body = render_page(page)
                how = f"{len(page.blocks)} blocks"
            except Exception as exc:
                if kind == "digital":
                    # The PDF's own text is a faithful stand-in for this page.
                    body = fallback_text(doc[p])
                    notes = [f"MODEL FAILED ({exc}); wrote the PDF's own text instead"]
                    fell_back.append(n)
                    how = "text-layer fallback"
                else:
                    # A scan has no text layer to fall back on, and OCR is never
                    # written into the output, so the page is marked as unread.
                    body = f"{UNREADABLE.format(n=n)} {exc}]"
                    notes = [f"MODEL FAILED ({exc}); nothing could be written for this page"]
                    unreadable.append(n)
                    how = "NOT READ"
            elapsed = time.time() - t0
            if used_scan:
                notes.append("read from the scan's own image, not a render (a render blurs thin marks)")
            n_img = len(doc[p].get_images(full=True))
            if n_img and kind == "digital":
                notes.append(f"{n_img} picture(s) on this page are not in the text file")
            model_time += elapsed
            if kind == "scanned" and have_ocr:
                ocr_texts[n] = ocr.ocr_page(doc[p])
            chunks.append(f"{head}\n\n{body}\n")
            wanted.append((n, kind))
            extra = ""
            if timing:
                extra = (f" (prefill {timing['prefill_s']:.1f}s, generate "
                         f"{timing['generate_s']:.1f}s / {timing['generate_tokens']} tok)")
            print(f"  page {n}: {kind}, {how} in {elapsed:.1f}s{extra}")
            for note in notes:
                print(f"       ! {note}")

    if not chunks:
        print("  VERDICT: FAIL -- nothing to write")
        return False

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(chunks))
    print(f"  wrote {os.path.getsize(path) / 1024:.1f} KB -> {path}")

    # Everything below reads the SAVED file.
    results, file_problems = verify_txt(path, doc, wanted, ocr_texts)
    print()
    print("  --- verification (read back from the saved .txt) ---")
    for r in results:
        tag = "PASS" if not r["problems"] else "FAIL"
        if r["kind"] == "blank":
            print(f"  page {r['page']}: blank page  [{tag}]")
        elif r["kind"] == "scanned":
            if "ocr_cov" in r:
                info = (f"OCR agreement: {r['ocr_cov']:.0%} of the {r['ocr_words']} "
                        f"words OCR read are in the file, {r['ocr_prec']:.0%} of the "
                        f"file's words are in OCR")
            elif have_ocr:
                info = "OCR read nothing on this page"
            else:
                info = "no OCR available"
            print(f"  page {r['page']}: scanned, UNVERIFIED (no text layer to check "
                  f"against). {info}  [{tag}]")
            if r.get("ocr_cov") is not None and (r["ocr_cov"] < 0.7 or r["ocr_prec"] < 0.7):
                print("       ~ low OCR agreement. Advisory only: OCR misses white-on-dark "
                      "text and accents. Look at this page.")
        elif "coverage" in r:
            print(f"  page {r['page']}: {r['coverage']:.0%} of {r['words']} PDF words present, "
                  f"{len(r['extras'])} extra, {len(r['lines_missing'])} line(s) missing, "
                  f"size {r['ratio']:.2f}x the PDF  [{tag}]")
        else:
            print(f"  page {r['page']}: [FAIL]")
        for pr in r["problems"]:
            print(f"       - {pr}")
    for pr in file_problems:
        print(f"  - {pr}")

    n_bad = sum(1 for r in results if r["problems"])
    n_unv = sum(1 for r in results if r.get("unverified") and not r["problems"])
    ok = n_bad == 0 and not file_problems
    print()
    print(f"  pages written:    {len(results)}/{len(targets)}")
    print(f"  pages verified:   {len(results) - n_bad - n_unv}/{len(results)}"
          + (f"   ({n_unv} scanned page(s) UNVERIFIED)" if n_unv else ""))
    if fell_back:
        print(f"  note: page(s) {fell_back} used the PDF's own text because the model failed")
    if unreadable:
        print(f"  note: page(s) {unreadable} could not be read by the model")
    print(f"  {time.time() - started:.1f}s total ({model_time:.1f}s on the model)")
    if not ok:
        print("  VERDICT: FAIL -- see the problems above")
    else:
        extra = []
        if fell_back:
            extra.append(f"{len(fell_back)} via text-layer fallback")
        if n_unv:
            extra.append(f"{n_unv} scanned page(s) unverified")
        print("  VERDICT: PASS -- every page converted"
              + (", digital pages verified" if n_dig else "")
              + (f" ({'; '.join(extra)})" if extra else ""))
    print()
    return ok


parser = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("pdfs", nargs="+", help="PDF file(s), globs allowed")
parser.add_argument("--outdir", default=TXT_OUT_DIR,
                    help=f"where the .txt files go (default {TXT_OUT_DIR})")
parser.add_argument("--pages", default="all", help="e.g. 1, 1-3, 2,4 (default: all)")
parser.add_argument("--dpi", type=int, default=125,
                    help="resolution of the page image the model reads (default 125)")
parser.add_argument("--model", default="qwen3-vl:4b-instruct",
                    help="Ollama model name; ignored when --base-url is given")
parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                    help=f"the llama-server to use (default {DEFAULT_BASE_URL}). "
                         f"Pass --base-url {LOCAL_BASE_URL} for this machine's "
                         "local 2B model, or an empty string for Ollama.")
parser.add_argument("--scan-image", action="store_true",
                    help="EXPERIMENTAL: for a scanned page that is exactly one "
                         "upright full-page image, send that image to the model "
                         "instead of a render. Fixed a lost hyphen on one scan but "
                         "lost more whole lines on another; see scan_image_for_model().")
args = parser.parse_args()

paths = []
for pattern in args.pdfs:
    paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[")
                 else [pattern])

ok = fail = 0
for path in paths:
    if not os.path.exists(path):
        print(f"skipping {path}: not found")
        continue
    if run(path, args.outdir, args.pages, args.dpi, args.base_url, args.model,
           use_scan_image=args.scan_image):
        ok += 1
    else:
        fail += 1

if len(paths) > 1:
    print("=" * 72)
    print(f"{ok} succeeded, {fail} failed or partial, {len(paths)} total")

sys.exit(1 if fail else 0)
