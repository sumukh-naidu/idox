"""
test_pdf.py -- run the extraction pipeline against a REAL PDF and check the
result against the PDF's own contents.

This is the first script where the validator has something real to check
against. Up to now it could only ask the model "did you contradict yourself?".
A digital PDF carries its own embedded text layer, which is the actual text the
document was built from -- so we can finally ask "did you make this up?".

THE THREE CHECKS, AND HOW MUCH EACH IS WORTH
--------------------------------------------------------------------------
1. SELF-CONSISTENCY (check_structure, in blocks.py)
   Does the model's claimed shape match what it actually emitted?
   Reliable, but weak -- a confident wrong answer passes.

2. TEXT-LAYER GROUNDING          <-- the valuable one
   Does every string the model produced actually appear in the PDF's embedded
   text? This is real ground truth, not a heuristic: it catches hallucination
   and misread values, which is the "right shape, wrong number" gap that
   self-consistency can never see.
   Only works on digital PDFs. A scanned PDF has no text layer, and this check
   correctly reports that it cannot run rather than pretending to pass.

3. TABLE GEOMETRY CROSS-CHECK    <-- advisory only, deliberately
   Compares the model's row/col counts against PyMuPDF's find_tables().
   Treated as ADVISORY because the detector is itself unreliable on real
   layouts -- on the IRS 1040 it reports a "36 x 27 table" that is really just
   form fields. Disagreement here means "these two disagree, look at it",
   not "the model is wrong".

Usage:
    .venv/bin/python test_pdf.py <file.pdf>
    .venv/bin/python test_pdf.py <file.pdf> --pages 1-3
    .venv/bin/python test_pdf.py <file.pdf> --pages 2 --dpi 200
    .venv/bin/python test_pdf.py <file.pdf> --out result.md
"""

import argparse
import hashlib
import os
import time

import pymupdf

from blocks import (
    DEFAULT_BASE_URL,
    Page,
    TableBlock,
    TextBlock,
    check_coverage,
    check_grounding,
    check_structure,
    drop_duplicate_blocks,
    extract_page,
    normalize,
    ungrounded_table_blocks,
    ungrounded_text_blocks,
)
import ocr
from to_docx import build_docx, verify_docx, verify_images
from to_xlsx import build_xlsx, verify_xlsx

RENDER_DIR = "render"
IMAGE_DIR = os.path.join(RENDER_DIR, "images")


# normalize() and check_grounding() now live in blocks.py, so the ReAct loop
# can feed their output back to the model as reflection material.

def parse_pages(spec: str, page_count: int):
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


def _real_row_count(pdf_page, table) -> int:
    """PyMuPDF's row count, minus trailing rows that are entirely empty.

    find_tables() routinely reports a phantom last row -- on page 1 of the
    business doc it claims 8 rows where the table really has 7, the eighth
    being ['', '', '', '', '', '']. Counting that made the check report a
    disagreement that was the detector's fault, not the model's.
    """
    rows = table.rows
    count = len(rows)
    while count > 0:
        cells = rows[count - 1].cells
        text = "".join(
            pdf_page.get_text("text", clip=pymupdf.Rect(c)).strip()
            for c in cells if c is not None
        )
        if text:
            break
        count -= 1
    return count


def extract_images(pdf_page, out_dir: str, page_no: int):
    """Pull raster images out of a PDF page, exactly as stored.

    NO MODEL AND NO OCR. The picture is already a finished digital asset inside
    the PDF -- copying the bytes gives perfect fidelity, while OCR would throw
    the graphic away to produce a text approximation. That is precisely what
    went wrong before this existed: the model read a 1352x969 logo and emitted
    the string "FreeTestData" instead.

    Returns [] when the page has no usable images, which makes every step
    downstream a no-op -- nothing changes for a PDF without pictures.

    Each entry carries:
        path        the saved PNG
        width_in    displayed size on the page, not pixel size, so the Word
                    document reproduces the scale the author chose
        frac_above  fraction of the page's text that sits ABOVE this image.
                    This is how the picture is placed in reading order: the
                    model's blocks carry no coordinates, but the PDF's text
                    layer does, so "65% of the text is above the logo" becomes
                    "insert it 65% of the way down the blocks".

    Vector graphics are deliberately NOT handled. They live in get_drawings()
    as individual instructions -- one test page had 91, almost all of them
    table borders -- so telling a real diagram from ruling lines is a separate
    problem. Raster covers what real documents mostly carry.
    """
    try:
        raw = pdf_page.get_images(full=True)
    except Exception:
        return []
    if not raw:
        return []

    # Text positions, for working out what sits above each image.
    spans = [
        (b[1], b[3], len(b[4]))
        for b in pdf_page.get_text("blocks") if b[4].strip()
    ]
    total_chars = sum(s[2] for s in spans) or 1

    # Same blocks, keeping the text, sorted down the page -- used to record
    # what sits immediately either side of each image.
    text_lines = sorted(
        (b[1], b[3], b[0], " ".join(b[4].split()))
        for b in pdf_page.get_text("blocks") if b[4].strip()
    )

    os.makedirs(out_dir, exist_ok=True)
    found = []

    for n, img in enumerate(raw):
        xref = img[0]
        try:
            bbox = pdf_page.get_image_bbox(img)
            info = pdf_page.parent.extract_image(xref)
        except Exception:
            continue

        # extract_image() hands back the ORIGINAL stored bytes and format,
        # untouched. The earlier version decoded to a Pixmap and re-saved as
        # PNG, which for a JPEG photo meant re-encoding it into a different
        # format and a much larger file. A copy should be a copy: this way the
        # bytes in the Word document can be hashed all the way back to the PDF.
        data, ext = info["image"], info.get("ext", "png")
        w, h = info.get("width", 0), info.get("height", 0)

        # Skip spacers, rules and bullet glyphs -- a 1x1 tracking pixel is not
        # a picture anyone wants in their Word document.
        if w < 40 or h < 40 or bbox.width < 28 or bbox.height < 28:
            continue

        path = os.path.join(out_dir, f"p{page_no:03d}_img{n:02d}.{ext}")
        try:
            with open(path, "wb") as fh:
                fh.write(data)
        except Exception:
            continue

        above = sum(c for y0, y1, c in spans if y1 <= bbox.y0)

        # The text immediately either side of the image, by coordinate. These
        # are what check 5 uses to prove the picture landed in the right place
        # rather than merely somewhere -- frac_above is a heuristic, and its
        # result was previously never checked at all.
        before = [t for y0, y1, _, t in text_lines if y1 <= bbox.y0]
        after = [t for y0, y1, _, t in text_lines if y0 >= bbox.y1]

        found.append({
            "path": path,
            "sha1": hashlib.sha1(data).hexdigest(),
            "bytes": len(data),
            "ext": ext,
            "bbox": (bbox.y0, bbox.y1),
            "width_in": bbox.width / 72.0,
            "height_in": bbox.height / 72.0,
            "px": (w, h),
            "frac_above": above / total_chars,
            "text_before": before[-1] if before else None,
            "text_after": after[0] if after else None,
        })

    return found


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


def measure_block_look(page, pdf_page):
    """Replace the model's guesses about appearance with MEASUREMENTS.

    align/size/bold exist for scanned pages, where there is no text layer and a
    model looking at the picture is the only thing that can see that a
    letterhead is centred. On a DIGITAL page that is the wrong source: the text
    layer records the exact font size and x-position of every line.

    Asking anyway caused a real regression. On a 3-page document whose body
    text is 12pt, the model labelled most blocks 'small', which made 'small'
    the size normaliser's anchor and pushed everything else up -- 31 runs
    rendered at 24pt and three source pages became six.

    Three guards keep the scanned path untouched:
      - the caller only invokes this when the page has a text layer
      - no lines found returns 0 and changes nothing
      - a block whose text cannot be located keeps the model's values, so
        nothing ever ends up with no formatting at all

    Returns the number of blocks corrected.
    """
    lines = []
    sizes = {}
    for raw in pdf_page.get_text("dict")["blocks"]:
        if raw["type"] != 0:
            continue
        for line in raw["lines"]:
            text = "".join(s["text"] for s in line["spans"]).strip()
            if not text:
                continue
            size = max((s["size"] for s in line["spans"]), default=10)
            bold = any("bold" in s.get("font", "").lower()
                       for s in line["spans"])
            # Same technique as bold, one line down: an italic font is named
            # accordingly ("Helvetica-Oblique", "Arial-Italic"), so this is a
            # measured fact from the document, not a guess -- the same
            # standard the rest of this function holds itself to.
            italic = any(f in s.get("font", "").lower()
                        for s in line["spans"] for f in ("italic", "oblique"))
            lines.append((normalize(text), size, line["bbox"], bold, italic))
            sizes[round(size, 1)] = sizes.get(round(size, 1), 0) + len(text)

    if not lines:
        return 0

    body = max(sizes, key=sizes.get)          # the dominant size IS the body
    page_mid = pdf_page.rect.width / 2
    # The widest line on the page approximates the real text column width.
    # Needed below to tell a genuinely centred (narrow) line from an ordinary
    # full-width paragraph line that merely happens to sit symmetrically
    # between two equal page margins.
    col_width = max((bbox[2] - bbox[0] for _, _, bbox, _, _ in lines), default=0)
    fixed = 0

    for block in page.blocks:
        if isinstance(block, TableBlock) or not block.text.strip():
            continue

        # Match on the opening of the block: a paragraph spans several lines,
        # and its first line carries the size and position that matter.
        head = normalize(block.text)[:20]
        if len(head) < 6:
            continue
        match = next((l for l in lines if l[0].startswith(head)), None)
        if match is None:
            match = next((l for l in lines if head in l[0]), None)
        if match is None:
            continue

        _, size, bbox, bold, italic = match
        block.size = ("large" if size >= body * 1.15
                      else "small" if size <= body * 0.85 else "normal")
        block.bold = bold

        # kind, like align/size/bold, is only ever the model's guess on a
        # digital page -- but here too the text layer has a real, measurable
        # answer for one common mistake: a caption/footnote line that the
        # model called a plain "paragraph". Same thresholds already trusted
        # elsewhere in this file for exactly this decision (see
        # repair_missing_lines()). Deliberately one-directional: this only
        # upgrades an under-classified "paragraph", never overrides a
        # heading/list/caption the model already committed to, so a correct
        # call is never second-guessed, only a generic fallback is.
        if block.kind == "paragraph":
            if size >= body * 1.25:
                block.kind = "heading"
            elif italic or size <= body * 0.88:
                block.kind = "caption"

        centre = (bbox[0] + bbox[2]) / 2
        width = bbox[2] - bbox[0]
        left_gap, right_gap = bbox[0], pdf_page.rect.width - bbox[2]
        # A real regression, confirmed on a real document: an ordinary
        # full-width body paragraph line, wrapped near the right margin,
        # lands its own midpoint within a few points of the page's midpoint
        # purely because the page's left/right margins are symmetric -- NOT
        # because the text is centred. "This report covers user growth..."
        # (bbox centre 293.9 vs page mid 306.0, well inside the old 24pt
        # tolerance) came out CENTER on a page where it visibly is not.
        # A genuinely centred line (a title, a letterhead) is narrower than
        # the page's own text column, with roughly equal empty space either
        # side -- an ordinary paragraph line fills most of that column. This
        # extra width check is what tells the two apart.
        narrow_enough = col_width == 0 or width < col_width * 0.85
        if (narrow_enough and abs(centre - page_mid) < 24
                and left_gap > 40 and right_gap > 40):
            block.align = "center"
        elif left_gap > right_gap * 3 and left_gap > 100:
            block.align = "right"
        else:
            block.align = "left"
        fixed += 1

    return fixed


# Standard page sizes in inches, for recognising a mislabelled page box.
STANDARD_PAGES = {
    "Letter": (8.5, 11.0),
    "Legal": (8.5, 14.0),
    "A4": (8.27, 11.69),
    "A3": (11.69, 16.54),
    "A5": (5.83, 8.27),
    "Tabloid": (11.0, 17.0),
}


def _sane_page_rect(pdf_page):
    """The page's real size, correcting a box that was declared in pixels.

    Some scanners write the page box using the image's PIXEL dimensions as if
    they were points. A 200 dpi scan of a Letter page then declares itself
    1700 x 2200 pt, which is 23.6 x 30.6 inches -- and the Word document comes
    out nearly four times too big, with content stranded in the top third of an
    enormous page.

    The aspect ratio gives it away without guessing at the dpi: 1700/2200 is
    0.7727, and Letter is 8.5/11 = 0.7727 exactly. So an implausibly large page
    whose proportions match a standard size is reported at that standard size.

    A page under 17 inches is left alone, so correctly-declared documents --
    including genuinely large ones like A3 and Tabloid -- are untouched.
    """
    rect = pdf_page.rect
    w_in, h_in = rect.width / 72.0, rect.height / 72.0

    if max(w_in, h_in) <= 17.5:
        return rect

    ratio = w_in / h_in if h_in else 1
    best, best_err = None, 0.02          # within 2% counts as a match
    for name, (sw, sh) in STANDARD_PAGES.items():
        for cand_w, cand_h in ((sw, sh), (sh, sw)):      # portrait or landscape
            err = abs(ratio - cand_w / cand_h) / (cand_w / cand_h)
            if err < best_err:
                best, best_err = (name, cand_w, cand_h), err

    if best is None:
        return rect

    name, cw, ch = best
    print(f"           page box declared {w_in:.1f}x{h_in:.1f}in -- "
          f"proportions match {name}, using {cw}x{ch}in")
    return pymupdf.Rect(0, 0, cw * 72, ch * 72)


def measure_layout(pdf_page) -> dict:
    """Read the source page's geometry so Word can be made to match it.

    Margins are measured from where the text actually sits rather than assumed,
    because a PDF carries no margin setting -- only content at coordinates.
    The body font size is the one the most characters use, which ignores
    headings and footnotes.
    """
    rect = _sane_page_rect(pdf_page)

    # Text coordinates are in the page's ORIGINAL space. If the box was
    # corrected above, everything measured from those coordinates has to be
    # scaled by the same factor or the margins land in the wrong units.
    scale = rect.width / pdf_page.rect.width if pdf_page.rect.width else 1.0

    blocks = [b for b in pdf_page.get_text("blocks") if b[4].strip()]

    if blocks:
        left = min(b[0] for b in blocks) * scale
        right = (pdf_page.rect.width - max(b[2] for b in blocks)) * scale
        top = min(b[1] for b in blocks) * scale
    else:
        left = right = top = 72.0

    sizes = {}
    for block in pdf_page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                sizes[round(span["size"])] = (
                    sizes.get(round(span["size"]), 0) + len(span["text"])
                )
    body = max(sizes, key=sizes.get) if sizes else 11

    return {
        "width_pt": rect.width,
        "height_pt": rect.height,
        "left_pt": left,
        "right_pt": right,
        "top_pt": top,
        # The bottom margin is not measurable -- a page that ends mid-way
        # through its text tells you nothing about where the margin was -- so
        # it mirrors the top, which is the usual convention.
        "bottom_pt": top,
        "body_pt": body,
    }


def check_geometry(page, pdf_page):
    """Advisory comparison against PyMuPDF's own table detector.

    Two things this deliberately does NOT do:
      - compare the model's CLAIMED row count. It compares what the model
        actually emitted, because a claim that disagrees with its own output is
        check 1's job, not this one's. Mixing them reported the same fault twice.
      - pair tables by list order. The model sometimes finds an extra table
        first, which used to make this compare a grey metadata strip against
        the main table and call them different shapes -- true, but meaningless.
        Tables are matched by vertical position on the page instead.
    """
    notes = []
    try:
        detected = pdf_page.find_tables().tables
    except Exception as exc:
        return [f"find_tables() failed: {exc}"]

    model_tables = [b for b in page.blocks if isinstance(b, TableBlock)]

    if len(detected) != len(model_tables):
        notes.append(
            f"table COUNT differs: model found {len(model_tables)}, "
            f"PyMuPDF found {len(detected)}"
        )

    # Match by page order on both sides: the model emits blocks top to bottom,
    # and detected tables are sorted by their vertical position.
    detected = sorted(detected, key=lambda t: t.bbox[1])

    for i, (mt, dt) in enumerate(zip(model_tables, detected)):
        # A headerless table contributes no extra row, and takes its width
        # from the data rather than from an empty header.
        model_rows = len(mt.rows) + (1 if mt.has_header else 0)
        model_cols = len(mt.header) if mt.has_header else (
            len(mt.rows[0]) if mt.rows else 0
        )
        det_rows = _real_row_count(pdf_page, dt)

        if model_rows != det_rows or model_cols != dt.col_count:
            notes.append(
                f"table[{i}] SHAPE differs: model {model_rows}x{model_cols}, "
                f"PyMuPDF {det_rows}x{dt.col_count}"
            )

    return notes


def render_block(block) -> str:
    """Render one block as clean Markdown."""
    if isinstance(block, TableBlock):
        # Width comes from the header actually emitted, not from the model's
        # claimed n_cols -- when those disagree (they did on the first real
        # page) trusting n_cols writes malformed Markdown.
        # Markdown has no headerless table, so an empty header row is emitted
        # to keep the grid valid rather than misrepresenting data as labels.
        header = block.header if block.has_header else (
            [""] * (len(block.rows[0]) if block.rows else 1)
        )
        width = max(len(header), 1)
        lines = ["| " + " | ".join(header) + " |"]
        lines.append("|" + "|".join(["---"] * width) + "|")
        for row in block.rows:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    if block.kind == "heading":
        return f"## {block.text}"
    if block.kind == "caption":
        return f"*{block.text}*"
    if block.kind == "list":
        return "\n".join(f"- {l.strip()}" for l in block.text.splitlines() if l.strip())
    return block.text


# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("pdf", help="path to the PDF to test")
parser.add_argument("--pages", default="all", help="e.g. 1, 1-3, 2,4 (default: all)")
parser.add_argument("--dpi", type=int, default=125,
                    help="render resolution (default 125). Measured on page 1: "
                         "100dpi loses every text block, 125 keeps all content "
                         "at ~16%% less time than 150.")
parser.add_argument("--model", default="qwen3-vl:4b-instruct",
                    help="Ollama model to use for extraction (default "
                         "qwen3-vl:4b-instruct). E.g. qwen3-vl:2b-instruct -- "
                         "measured ~2x faster on one real document, same "
                         "block count, no accuracy loss observed. Ignored "
                         "when --base-url is given.")
parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                    help="the raw llama-server instance to use (default: "
                         "the manually-downloaded HF model on "
                         f"{DEFAULT_BASE_URL}). Pass an empty string to use "
                         "Ollama's own bundled model instead (e.g. for "
                         "--model qwen3-vl:4b-instruct, not present in the "
                         "manually-downloaded set).")
parser.add_argument("--out", default="extracted.md", help="clean Markdown output file")
parser.add_argument("--docx", metavar="FILE.docx",
                    help="also write a Word document (Tier 3, no model involved)")
parser.add_argument("--xlsx", metavar="FILE.xlsx",
                    help="also write an Excel file (Tier 3, no model involved). "
                         "Every page goes into one sheet, tables as grids and "
                         "prose in column A, all values kept as text.")
parser.add_argument("--no-ocr", action="store_true",
                    help="skip OCR verification on scanned pages, leaving "
                         "checks 2 and 4 reporting N/A as before")
parser.add_argument("--scan-mode", choices=("text", "image", "both"),
                    default="text",
                    help="what to do with a page that has NO text layer. "
                         "'text' (default) reads it with the model and writes "
                         "an editable transcript, embedding no image. 'image' "
                         "embeds the page picture and does NOT run the model "
                         "at all -- fast, but not editable. 'both' does the "
                         "text extraction AND writes the page images to a "
                         "SECOND file named <docx>_scan.docx -- never mixed "
                         "into one document. Ignored for digital pages.")
parser.add_argument("--show-reasoning", action="store_true",
                    help="print the model's layout analysis and self-review")
args = parser.parse_args()

doc = pymupdf.open(args.pdf)
targets = parse_pages(args.pages, doc.page_count)
os.makedirs(RENDER_DIR, exist_ok=True)

print(f"{args.pdf} -- {doc.page_count} pages, testing {len(targets)}: "
      f"{[p + 1 for p in targets]}")
print(f"rendering at {args.dpi} dpi into {RENDER_DIR}/\n")

markdown_out = [f"# Extracted from {os.path.basename(args.pdf)}\n"]
extracted_pages = []          # kept for the Tier 3 .docx conversion
page_image_sets = []          # one list of extracted images per page (often [])
scan_image_sets = []          # scanned-page images destined for a SEPARATE file
source_texts = []             # the PDF's own text, for verifying that .docx
source_layout = None          # page size/margins/font, so Word matches the PDF

# Latency breakdown: these accumulate across every page in the loop, then get
# printed as one total at the very end. Each is the standard
# time.time()-before / time.time()-after / subtract pattern, just added up.
total_extraction_time = 0.0     # PHASE 1: PDF -> data (extract_page calls)
total_validation_time = 0.0     # PHASE 2: the 4 checks only (not repairs)
total_conversion_time = 0.0     # PHASE 3: data -> .docx (build_docx, once)

# TEMPORARY -- sub-breakdown of PHASE 1 only, straight from Ollama's own
# internal timers. Remove alongside the other TEMPORARY block once the
# bottleneck within extraction has been identified.
total_model_load_time = 0.0
total_prefill_time = 0.0
total_generate_time = 0.0
total_prefill_tokens = 0
total_generate_tokens = 0

for pno in targets:
    pdf_page = doc[pno]

    # --- Tier 0: what the PDF itself says -------------------------------
    source_text = pdf_page.get_text()
    try:
        n_detected = len(pdf_page.find_tables().tables)
    except Exception:
        n_detected = -1

    print("=" * 72)
    print(f"PAGE {pno + 1}")
    print("=" * 72)
    print(f"Tier 0 -- text layer: {len(source_text)} chars, "
          f"tables detected: {n_detected}")

    # --- render the page for the model ----------------------------------
    img_path = os.path.join(RENDER_DIR, f"page_{pno + 1:03d}.png")
    pdf_page.get_pixmap(dpi=args.dpi).save(img_path)

    # --- the scanned-page gate ------------------------------------------
    # A page with no text layer is a scan, and for a scan the two extraction
    # paths are alternatives rather than partners. Running both is what put a
    # picture of the page AND a transcript of that same picture into one
    # document -- the same content twice, which also pushed a one-page letter
    # onto two pages.
    #
    # A digital page is unaffected: is_scanned is false, so both paths run as
    # before and a logo still sits inline with the text where it belongs.
    is_scanned = not source_text.strip()
    image_only = is_scanned and args.scan_mode == "image"
    scan_images_this_page = []      # only filled under --scan-mode both

    # --- what the checks get to compare against ---------------------------
    # Three levels, and the difference decides what is allowed to happen:
    #
    #   "pdf"  the text layer -- the author's actual characters. Exact, so a
    #          dropped line can be restored from it byte-for-byte.
    #   "ocr"  an independent READING of the pixels. Good evidence, but a
    #          reading: disagreements are reported, never silently applied.
    #   "none" nothing. Checks 2 and 4 report N/A and mean it.
    verify_text, verify_trust = source_text, "pdf"

    if is_scanned and not image_only and not args.no_ocr:
        if ocr.have_tesseract():
            ocr_started = time.time()
            verify_text = ocr.ocr_page(pdf_page)
            if verify_text.strip():
                verify_trust = "ocr"
                print(f"           OCR: {len(verify_text)} chars read in "
                      f"{time.time() - ocr_started:.1f}s "
                      f"-- used to CHECK the model, not to replace it")
            else:
                verify_trust = "none"
                print("           OCR returned nothing -- checks stay N/A")
        else:
            verify_trust = "none"
            print(f"           no OCR available ({ocr.install_hint()})")
    elif is_scanned:
        verify_trust = "none"

    if is_scanned:
        print(f"           scanned page -> --scan-mode {args.scan_mode}: "
              + ("embedding the page image, model NOT run"
                 if image_only else
                 "extracting text, page image NOT embedded"))

    if image_only:
        # No model call at all. This is the whole point of the mode: the page
        # is copied, not read, so it costs a fraction of a second instead of
        # several minutes.
        page = Page(analysis={"n_blocks": 0, "table_column_counts": []},
                    blocks=[])
        page_images = extract_images(pdf_page, IMAGE_DIR, pno + 1)

        if not page_images:
            # A scan with no embedded raster -- rare, but then the rendered
            # page is the only thing to fall back on.
            fallback = os.path.join(IMAGE_DIR, f"p{pno + 1:03d}_page.png")
            os.makedirs(IMAGE_DIR, exist_ok=True)
            pix = pdf_page.get_pixmap(dpi=args.dpi)
            pix.save(fallback)
            with open(fallback, "rb") as fh:
                data = fh.read()
            page_images = [{
                "path": fallback, "sha1": hashlib.sha1(data).hexdigest(),
                "bytes": len(data), "ext": "png",
                "bbox": (0, pdf_page.rect.height),
                "width_in": pdf_page.rect.width / 72.0,
                "height_in": pdf_page.rect.height / 72.0,
                "px": (pix.width, pix.height), "frac_above": 0.0,
                "text_before": None, "text_after": None,
            }]
            print("           no embedded raster -- using the rendered page")

        for im in page_images:
            print(f"  image: {im['px'][0]}x{im['px'][1]}px, "
                  f"{im['width_in']:.2f}x{im['height_in']:.2f}in -> {im['path']}")
        print("\n  --- checks ---")
        print("  skipped: nothing was extracted, so there is nothing to verify")
        print()

        extracted_pages.append(page)
        page_image_sets.append(page_images)
        scan_image_sets.append([])
        source_texts.append(source_text)
        if source_layout is None:
            source_layout = measure_layout(pdf_page)
        continue

    # --- Tier 1: the model ----------------------------------------------
    # PHASE 1: PDF -> data extraction.
    started = time.time()
    try:
        page, model_timing = extract_page(
            img_path, model=args.model, return_timing=True,
            base_url=args.base_url, include_look=is_scanned,
        )
    except Exception as exc:
        print(f"Tier 1 FAILED to produce valid output: {exc}")
        continue
    elapsed = time.time() - started
    total_extraction_time += elapsed

    # A real, confirmed model failure mode: under greedy decoding, the model
    # has re-emitted already-written blocks verbatim instead of stopping the
    # array. repeat_penalty (in extract_page()) fixes this at the source; this
    # is the deterministic backstop in case that ever isn't enough.
    page, dupes = drop_duplicate_blocks(page)
    for d in dupes:
        print(f"       ! dropped duplicate block (model repeated itself): "
              f"{d!r}")

    print(f"Tier 1 -- {len(page.blocks)} blocks in {elapsed:.1f}s\n")

    # TEMPORARY -- PHASE 1 sub-breakdown, accumulated here and printed ONCE at
    # the very end in the LATENCY BREAKDOWN summary (not per-page, to avoid
    # showing the same numbers twice). Straight from Ollama's own internal
    # timers, not re-measured here. Remove alongside the other TEMPORARY
    # block once the bottleneck within extraction has been identified.
    total_model_load_time += model_timing["load_s"]
    total_prefill_time += model_timing["prefill_s"]
    total_generate_time += model_timing["generate_s"]
    total_prefill_tokens += model_timing["prefill_tokens"]
    total_generate_tokens += model_timing["generate_tokens"]

    if args.show_reasoning:
        def show(title, obj):
            print(f"  ----- {title} -----")
            for field, value in obj.model_dump().items():
                print(f"  {field}:")
                for line in str(value).strip().splitlines():
                    print(f"      {line}")
            print()

        # Written BEFORE the blocks -- this is what the extraction was based on.
        show("ANALYSIS (stage 1, written before extracting)", page.analysis)

    for i, block in enumerate(page.blocks):
        if isinstance(block, TableBlock):
            kind = "with header" if block.has_header else "headerless"
            print(f"  [{i}] table  {block.n_data_rows} data rows "
                  f"x {block.n_cols} cols ({kind})")
            if block.has_header:
                print("        H | " + " | ".join(block.header))
            for row in block.rows:
                print("          | " + " | ".join(row))
        else:
            preview = " ".join(block.text.split())
            if len(preview) > 88:
                preview = preview[:88] + "..."
            print(f"  [{i}] {block.kind}  {preview}")

    # --- the checks ------------------------------------------------------
    # PHASE 2: validation -- the 4 checks only. Repairs (which run further
    # below, if a check fails) are deliberately NOT included in this timing.
    validation_started = time.time()
    structural, notes = check_structure(page)
    grounding, found, total = check_grounding(page, verify_text)
    geometry = check_geometry(page, pdf_page)
    coverage_problems, coverage, _missing, missing_lines = check_coverage(
        page, verify_text
    )
    validation_elapsed = time.time() - validation_started
    total_validation_time += validation_elapsed

    print("\n  --- checks ---")

    print(f"  1. self-consistency:       {'PASS' if not structural else 'FAIL'}")
    for p in structural:
        print(f"       - {p}")
    for n in notes:
        print(f"       ~ {n}")

    # Checks 2 and 4 both need the PDF's own text to compare against. On a
    # scanned page there is none, so they are UNANSWERABLE rather than failed.
    # Reporting them as FAIL on every scan is how a check gets ignored.
    if total == 0 and verify_trust == "none":
        print("  2. text-layer grounding:   N/A    "
              "(no text layer -- nothing to verify the model against)")
    else:
        print(f"  2. text-layer grounding:   "
              f"{'PASS' if not grounding else 'FAIL'}"
              f"   ({found}/{total} strings verified against the PDF)")
        for p in grounding:
            print(f"       - {p}")

    print(f"  3. geometry (advisory):    "
          f"{'agrees' if not geometry else 'differs'}")
    for p in geometry:
        print(f"       - {p}")

    if coverage is None:
        print("  4. content coverage:       N/A    "
              "(no text layer -- dropped content cannot be detected)")
    else:
        print(f"  4. content coverage:       "
              f"{'PASS' if not coverage_problems else 'FAIL'}"
              f"   ({coverage:.0%} of the PDF's text was extracted)")
    for p in coverage_problems:
        print(f"       - {p}")

    print(f"  >>> PHASE 2 (validation, 4 checks) took {validation_elapsed:.3f}s")

    # --- repair: put dropped lines back, taken from the PDF itself ---------
    # The checks above deliberately report the model's RAW output, so how
    # often it drops things stays visible. The repair happens after, and says
    # exactly what it changed -- a silently patched page would look perfect
    # every run and hide the model getting worse.
    # Repairs restore text VERBATIM from the source, which is only safe when
    # the source is the PDF's own text layer. OCR is a reading of the pixels,
    # not the document -- pasting its output in would launder a guess into the
    # deliverable. Under OCR the disagreement is reported and left alone.
    if missing_lines and verify_trust == "ocr":
        print(f"       ! {len(missing_lines)} line(s) OCR found but the model "
              f"did not return -- NOT auto-restored (OCR is a reading, not "
              f"the document):")
        for m in missing_lines[:4]:
            print(f"           {m[:60]!r}")

    if missing_lines and verify_trust == "pdf":
        page, repairs, skipped = repair_missing_lines(
            page, pdf_page, missing_lines
        )

        for r in repairs:
            print(f"       + RESTORED from the PDF: {r}")
        for s in skipped:
            print(f"       ! inside a table, not restored: {s[:60]!r}")

        if repairs:
            _, coverage, _, still_missing = check_coverage(page, source_text)
            print(f"       => coverage after repair: {coverage:.0%}"
                  f"{'' if not still_missing else f', {len(still_missing)} line(s) still missing'}")

    # Dropped TABLE ROWS are invisible to the line check above -- cells sit in
    # the text layer as separate short lines, so a whole missing row scores as
    # a couple of absent words. Rows are therefore compared as rows.
    page, row_repairs = ((page, []) if verify_trust == "ocr"
                         else repair_table_rows(page, pdf_page))
    for r in row_repairs:
        print(f"       + RESTORED from the PDF: {r}")

    print()

    # --- images: only does anything if this page actually has some ---------
    page_images = extract_images(pdf_page, IMAGE_DIR, pno + 1)

    if is_scanned and page_images:
        # A scanned page's image IS the page -- the same pixels the text above
        # was just read from. Embedding it beside the transcript puts the whole
        # document in twice, which is what this gate exists to prevent. So it
        # never goes into the text document; under 'both' it goes into its own.
        if args.scan_mode == "both":
            scan_images_this_page = page_images
            print(f"\n  images: {len(page_images)} held back for the separate "
                  f"scan document (--scan-mode both)")
        else:
            print(f"\n  images: {len(page_images)} found but NOT embedded "
                  f"(--scan-mode text: the transcript replaces the scan)")
        page_images = []

    if page_images:
        print(f"\n  images: {len(page_images)} extracted from the PDF "
              f"(no model involved)")
        for im in page_images:
            print(f"       - {im['px'][0]}x{im['px'][1]}px, "
                  f"{im['width_in']:.2f}x{im['height_in']:.2f}in on page, "
                  f"{im['frac_above']:.0%} of text above it -> {im['path']}")

        # Text the model read OUT OF a picture is now a duplicate of that
        # picture, and wrong. Only considered when images exist on the page --
        # an ungrounded block on a page with no pictures is a misread to
        # report, not something to quietly delete.
        bogus = ungrounded_text_blocks(page, source_text)
        for i in bogus:
            print(f"       - dropping block[{i}] "
                  f"{page.blocks[i].text[:40]!r} -- not in the PDF's text, "
                  f"read from the image")

        # The same thing happens with structure, not just words. A bar chart
        # was read as a 2x5 table of its own axis labels and values, none of
        # which appear in the document -- the page has a chart, and the chart
        # is already being embedded as an image. Only tables whose cells are
        # ENTIRELY ungrounded qualify; one bad cell among 49 good ones is a
        # misread to report, not a table to delete.
        bogus_tables = ungrounded_table_blocks(page, source_text)
        for i in bogus_tables:
            t = page.blocks[i]
            print(f"       - dropping block[{i}] table "
                  f"{len(t.rows)}x{t.n_cols} {t.header[:4]} -- no cell appears "
                  f"in the PDF's text, read from the image")

        drop = set(bogus) | set(bogus_tables)
        if drop:
            page = Page(
                analysis=page.analysis,
                blocks=[b for i, b in enumerate(page.blocks) if i not in drop],
            )

    # --- appearance: measured where possible, asked for only where not -----
    # The model reports align/size/bold on every page, but on a digital page
    # the text layer knows them exactly. Measuring overrides the guesses; on a
    # scanned page this is skipped entirely and the model's values stand.
    if not is_scanned:
        fixed = measure_block_look(page, pdf_page)
        if fixed:
            print(f"\n  layout: appearance of {fixed} block(s) measured from "
                  f"the text layer, replacing the model's estimates")

    extracted_pages.append(page)
    page_image_sets.append(page_images)
    scan_image_sets.append(scan_images_this_page)
    source_texts.append(source_text)
    if source_layout is None:
        source_layout = measure_layout(pdf_page)

    markdown_out.append(f"\n---\n\n*(page {pno + 1})*\n")
    for block in page.blocks:
        markdown_out.append(render_block(block) + "\n")

with open(args.out, "w") as fh:
    fh.write("\n".join(markdown_out))

print("=" * 72)
print(f"clean extraction written to {args.out}")

# --- Tier 3: build the Word document ------------------------------------
# Deterministic. python-docx writes the file from the extracted structure;
# no model is asked anything here.
# PHASE 3: data -> .docx. Runs ONCE here, after every page has already been
# extracted and validated above -- NOT once per page.
if args.docx and extracted_pages:
    conversion_started = time.time()
    problems = build_docx(
        extracted_pages, args.docx, title=os.path.basename(args.pdf),
        layout=source_layout, image_sets=page_image_sets,
    )
    total_conversion_time += time.time() - conversion_started
    print(f"Word document written to {args.docx}")
    if source_layout:
        print(f"  page setup matched to source: "
              f"{source_layout['width_pt'] / 72:.1f}x"
              f"{source_layout['height_pt'] / 72:.1f}in, "
              f"{source_layout['left_pt'] / 72:.2f}in margins, "
              f"{source_layout['body_pt']}pt body")

    if problems:
        print("  structural repairs made while writing:")
        for p in problems:
            print(f"    - {p}")

    # Reading the file back is the only honest way to claim it contains the
    # document. Checking what we meant to write proves nothing.
    coverage, missing = verify_docx(args.docx, "\n".join(source_texts))
    print(f"  content check: {coverage:.0%} of the PDF's words are in the .docx")

    # --- the separate scan document, under --scan-mode both ---------------
    # Written as its own file rather than as extra pages, so the text document
    # stays a clean editable transcript and the scan stays a faithful picture.
    # Neither contains the other.
    if any(scan_image_sets):
        stem, ext = os.path.splitext(args.docx)
        scan_path = f"{stem}_scan{ext}"
        blank = [Page(analysis={"n_blocks": 0, "table_column_counts": []},
                      blocks=[]) for _ in scan_image_sets]
        scan_problems = build_docx(blank, scan_path, layout=source_layout,
                                   image_sets=scan_image_sets)
        n_scans = sum(len(s) for s in scan_image_sets)
        print(f"Scan document written to {scan_path}  "
              f"({n_scans} page image(s), no text)")
        for p in scan_problems:
            print(f"    - {p}")

    # Check 5 -- only says anything when the PDF had images at all.
    if any(page_image_sets):
        img_problems = verify_images(args.docx, page_image_sets)
        n = sum(len(s) for s in page_image_sets)
        print(f"  image check:   {'PASS' if not img_problems else 'FAIL'}"
              f"   ({n} image(s): count, bytes and position verified "
              f"against the PDF)")
        for p in img_problems:
            print(f"    - {p}")
    if missing:
        shown = ", ".join(missing[:8])
        more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
        print(f"    missing: {shown}{more}")

# --- Tier 3: the Excel file ---------------------------------------------
# Same Page objects, same checks, a different writer. Deterministic: nothing
# here asks the model anything.
if args.xlsx and extracted_pages:
    xl_problems = build_xlsx(extracted_pages, args.xlsx,
                             image_sets=page_image_sets)
    print(f"Excel file written to {args.xlsx}  "
          f"(one sheet, all pages, values kept as text)")
    for p in xl_problems:
        print(f"    - {p}")

    coverage, missing = verify_xlsx(args.xlsx, "\n".join(source_texts))
    print(f"  content check: {coverage:.0%} of the PDF's words are in the .xlsx")
    if missing:
        shown = ", ".join(missing[:8])
        more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
        print(f"    missing: {shown}{more}")

print(f"rendered page images in {RENDER_DIR}/ -- open these to see what the "
      f"model actually saw")

# --- latency breakdown: totals across every page, all three phases side by
# side so you can see which one is actually the bottleneck without adding
# per-page numbers up yourself.
total_time = total_extraction_time + total_validation_time + total_conversion_time
print()
print("=" * 72)
print("LATENCY BREAKDOWN (summed across all pages processed)")
print("=" * 72)
if total_time > 0:
    print(f"  PHASE 1  extraction  (PDF -> data)   "
          f"{total_extraction_time:8.2f}s  "
          f"({total_extraction_time / total_time:5.1%})")
    # TEMPORARY -- PHASE 1 sub-breakdown, from Ollama's own internal timers.
    _phase1_sub = (total_model_load_time + total_prefill_time
                  + total_generate_time) or 1e-9
    print(f"      model load:                       "
          f"{total_model_load_time:8.2f}s  "
          f"({total_model_load_time / _phase1_sub:5.1%} of phase 1)")
    print(f"      reading page (prefill):           "
          f"{total_prefill_time:8.2f}s  "
          f"({total_prefill_time / _phase1_sub:5.1%} of phase 1)  "
          f"-- {total_prefill_tokens} tokens")
    print(f"      generating (writing output):      "
          f"{total_generate_time:8.2f}s  "
          f"({total_generate_time / _phase1_sub:5.1%} of phase 1)  "
          f"-- {total_generate_tokens} tokens")
    print(f"  PHASE 2  validation  (4 checks)       "
          f"{total_validation_time:8.2f}s  "
          f"({total_validation_time / total_time:5.1%})")
    print(f"  PHASE 3  conversion  (data -> .docx)  "
          f"{total_conversion_time:8.2f}s  "
          f"({total_conversion_time / total_time:5.1%})")
    print(f"  {'-' * 68}")
    print(f"  TOTAL                                 {total_time:8.2f}s")
else:
    print("  no pages were timed -- nothing to report")
