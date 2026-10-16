"""
repair.py -- put lines the model dropped back where they belong, copied from the PDF's own text.

WHY THIS REPLACES THE OLD PLACEMENT. repair_missing_lines() in test_pdf.py and pdf_to_txt.py restored a dropped line
as a NEW BLOCK, at a position worked out from "what share of the page's characters sit above it" times the number of
blocks. Blocks are very different sizes, so the line landed somewhere near but not at its place, and a line that was
really the middle of a paragraph became a stray paragraph or, when its font was larger, a stray HEADING. On a dense
three-page text it left the Word, Excel, PowerPoint, XML and CSV outputs with the right words in the wrong order.

HOW THIS ONE DECIDES (digital PDFs only: the text layer is the author's own characters)
  1. The page's words are aligned, in order, with the PDF's words (difflib). The alignment is MONOTONIC, so a phrase
     that occurs twice in the document (Lorem ipsum does) can only match at its own place.
  2. A PDF line is MISSING only if few of its words are matched. A line the model merely misread or joined into a
     paragraph is matched and is NOT restored again (that is what doubled the text before).
  3. Each missing line is placed next to its real neighbours: the block holding the nearest matched word before it
     and the one holding the nearest matched word after it.
       - both neighbours are the same paragraph  -> the line is SPLICED INTO that paragraph at its exact place;
       - it continues the paragraph above it (same size, no paragraph gap) -> it is appended to that paragraph;
       - otherwise -> a new block right after the block above it.
     Consecutive missing lines of one paragraph are restored together as one piece of text.
  4. Lines inside a detected table are never restored as prose (they are reported), exactly as before.

restore_missing_lines() has the same contract as the function it replaces: (new_page, repairs, skipped).

KNOWN LIMIT. Which lines count as missing is decided by check_coverage() in blocks.py, which asks only whether a line's
text appears ANYWHERE in the output, so a dropped line that repeats text found elsewhere on the page is never flagged.
restore_missing_lines(scan_all=True) can find such lines by alignment, but it is OFF in the pipeline on purpose: where text
repeats, the alignment cannot tell which copy a line belongs to, and on a Lorem-ipsum test document it put restored text
in the wrong place (order score 90.6 -> 48.1). A repeated line that was dropped cannot be placed reliably.
"""

import difflib
import re

import pymupdf

from blocks import Page, TableBlock, TextBlock, normalize

SAME_PARAGRAPH_GAP = 0.6        # a line within this many line-heights of the previous one continues its paragraph
COVERED = 0.6                   # a PDF line is "covered" when at least this share of its words is matched


def _pdf_lines(pdf_page):
    """Every text line of the page in the text layer's order, and the body font size."""
    lines, sizes = [], {}
    for block in pdf_page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            text = "".join(s["text"] for s in line["spans"]).strip()
            if not text:
                continue
            size = max((s["size"] for s in line["spans"]), default=10)
            lines.append({"text": text, "bbox": line["bbox"], "size": size})
            sizes[round(size)] = sizes.get(round(size), 0) + len(text)
    return lines, (max(sizes, key=sizes.get) if sizes else 10)


def _block_tokens(block):
    """(display tokens, whether each is a word) for a block; a table is one run of its cell words."""
    if isinstance(block, TableBlock):
        text = " ".join(list(block.header) + [c for row in block.rows for c in row])
    else:
        text = block.text
    tokens = re.findall(r"\S+|\s+", text)
    return tokens


def _same_paragraph(prev, cur):
    height = max(prev["bbox"][3] - prev["bbox"][1], 1.0)
    gap = cur["bbox"][1] - prev["bbox"][3]
    return abs(cur["size"] - prev["size"]) < 0.5 and gap <= SAME_PARAGRAPH_GAP * height


MIN_SCAN = 20           # with scan_all, an unflagged line must be at least this long (normalised) to be restored;
                        # shorter ones are page numbers, bullets and the like

def _align(page, lines):
    """Align the PDF's words with the page's words, in order.

    Returns (pdf_norm, line_words, pdf_to_out, out_norm, out_pos, block_tokens): the PDF's normalised words, the
    word indices of each PDF line, which output word each matched PDF word lines up with, the output's normalised
    words, where each sits as (block index, token index), and each block's display tokens.
    """
    pdf_norm = []
    line_words = []                                  # per line: indices into pdf_norm
    for line in lines:
        idx = []
        for tok in line["text"].split():
            n = normalize(tok)
            if n:
                idx.append(len(pdf_norm))
                pdf_norm.append(n)
        line_words.append(idx)

    out_norm, out_pos = [], []                       # out_pos[j] = (block index, token index in that block)
    block_tokens = [_block_tokens(b) for b in page.blocks]
    for bi, tokens in enumerate(block_tokens):
        for ti, tok in enumerate(tokens):
            n = normalize(tok) if tok.strip() else ""
            if n:
                out_norm.append(n)
                out_pos.append((bi, ti))

    matcher = difflib.SequenceMatcher(None, pdf_norm, out_norm, autojunk=False)
    pdf_to_out = {}
    for i, j, n in matcher.get_matching_blocks():
        for t in range(n):
            pdf_to_out[i + t] = j + t
    return pdf_norm, line_words, pdf_to_out, out_norm, out_pos, block_tokens


def restore_missing_lines(page, pdf_page, missing_lines, scan_all=False):
    """Returns (new_page, repairs, skipped); see the module docstring.

    scan_all=True does not trust the caller's list of missing lines alone: every PDF line the alignment cannot find
    in the output (and that is at least MIN_SCAN characters) is restored. That catches a dropped line whose text
    also occurs elsewhere on the page, which the caller's "is it anywhere in the output?" check cannot see.
    """
    if not missing_lines and not scan_all:
        return page, [], []

    wanted = {normalize(m) for m in (missing_lines or [])}
    lines, body = _pdf_lines(pdf_page)
    if not lines:
        return page, [], []
    try:
        table_boxes = [pymupdf.Rect(t.bbox) for t in pdf_page.find_tables().tables]
    except Exception:
        table_boxes = []

    pdf_norm, line_words, pdf_to_out, out_norm, out_pos, block_tokens = _align(page, lines)

    # --- which lines are truly missing ----------------------------------------------------------------------
    missing, skipped = [], []
    for k, line in enumerate(lines):
        words = line_words[k]
        key = normalize(line["text"])
        if not words or (key not in wanted and not (scan_all and len(key) >= MIN_SCAN)):
            continue
        if sum(1 for w in words if w in pdf_to_out) / len(words) >= COVERED:
            continue                                  # present (perhaps misread or joined): not restored again
        rect = pymupdf.Rect(line["bbox"])
        if any(abs(rect & tb) / max(abs(rect), 1) > 0.5 for tb in table_boxes):
            skipped.append(line["text"])
            continue
        missing.append(k)
    if not missing:
        return page, [], skipped

    # --- group consecutive missing lines that are one paragraph -----------------------------------------------
    runs = []
    for k in missing:
        if runs and runs[-1][-1] == k - 1 and _same_paragraph(lines[k - 1], lines[k]):
            runs[-1].append(k)
        else:
            runs.append([k])

    # --- plan: for each run, a splice into a block's text, or a new block after some block -------------------
    splices = {}                                       # block index -> [(token index to insert after, text)]
    inserts = []                                       # (after block index or -1, order key, block, note)
    repairs = []
    for run in runs:
        first, last = run[0], run[-1]
        text = " ".join(lines[k]["text"] for k in run)
        s_idx, e_idx = line_words[first][0], line_words[last][-1]
        prev_i = next((i for i in range(s_idx - 1, -1, -1) if i in pdf_to_out), None)
        next_i = next((i for i in range(e_idx + 1, len(pdf_norm)) if i in pdf_to_out), None)
        jp = pdf_to_out[prev_i] if prev_i is not None else None
        jq = pdf_to_out[next_i] if next_i is not None else None
        bp, tp = out_pos[jp] if jp is not None else (None, None)
        bq = out_pos[jq][0] if jq is not None else None

        size = lines[first]["size"]
        kind = "heading" if size >= body * 1.25 else "caption" if size <= body * 0.88 else "paragraph"
        note_head = f"{text[:50]!r} ({size:.0f}pt vs {body}pt body"

        def is_prose(bi):
            return bi is not None and isinstance(page.blocks[bi], TextBlock) and page.blocks[bi].kind == "paragraph"

        # inside one paragraph: splice at the exact place
        if bp is not None and bp == bq and is_prose(bp):
            splices.setdefault(bp, []).append((tp, text))
            repairs.append(f"{note_head} -> spliced into paragraph {bp})")
            continue
        # continues the paragraph above it
        prev_line = lines[first - 1] if first > 0 else None
        if (bp is not None and is_prose(bp) and prev_line is not None and kind == "paragraph"
                and jp == max(j for j, (b, _) in enumerate(out_pos) if b == bp)
                and _same_paragraph(prev_line, lines[first])):
            splices.setdefault(bp, []).append((tp, text))
            repairs.append(f"{note_head} -> appended to paragraph {bp})")
            continue
        # a block of its own, right after the block above it
        align = "center" if abs((lines[first]["bbox"][0] + lines[first]["bbox"][2]) / 2
                                - pdf_page.rect.width / 2) < 20 else "left"
        block = TextBlock(kind=kind, align=align,
                          size="large" if kind == "heading" else "small" if kind == "caption" else "normal",
                          bold=False, text=text)
        after = bp if bp is not None else (bq - 1 if bq is not None else len(page.blocks) - 1)
        inserts.append((after, first, block))
        repairs.append(f"{note_head} -> {kind}, new block after block {after})")

    # --- apply -------------------------------------------------------------------------------------------------
    new_blocks = list(page.blocks)
    for bi, items in splices.items():
        tokens = list(block_tokens[bi])
        for tp, text in sorted(items, key=lambda it: it[0], reverse=True):
            tokens[tp + 1:tp + 1] = [" ", text]        # after the word at tp, with a space on each side
        old = new_blocks[bi]
        new_blocks[bi] = TextBlock(kind=old.kind, align=old.align, size=old.size, bold=old.bold,
                                   text="".join(tokens).replace("  ", " "))
    result = []
    for after, _, block in sorted((i for i in inserts if i[0] < 0), key=lambda i: i[1]):
        result.append(block)
    for bi, block in enumerate(new_blocks):
        result.append(block)
        for after, _, extra in sorted((i for i in inserts if i[0] == bi), key=lambda i: i[1]):
            result.append(extra)
    return Page(analysis=page.analysis, blocks=result), repairs, skipped


# ---------------------------------------------------------------------------------------------------------------
# Putting blocks the model wrote in the wrong ORDER back in the PDF's order
# ---------------------------------------------------------------------------------------------------------------
SHINGLE = 6                     # a block is located by its first words; this many must match to count


def _block_text(block):
    if isinstance(block, TableBlock):
        return " ".join(list(block.header) + [c for row in block.rows for c in row])
    return block.text


def reorder_blocks(page, pdf_page):
    """Return (new_page, moves). Blocks go back into the order of the PDF's own text.

    The model sometimes reads a page's blocks in the wrong order (a table before the caption and heading that sit
    above it). Each block is located in the PDF's word stream by its first words. Only an UNAMBIGUOUS location is
    trusted: if the same opening words occur more than once in the document (repeated text), the block is left
    where it is. A block that cannot be located (text read out of a picture, say) stays right behind the block
    before it. The sort is stable, so blocks that are already in order never move.
    """
    lines, _ = _pdf_lines(pdf_page)
    pdf_norm = [normalize(t) for line in lines for t in line["text"].split()]
    pdf_norm = [w for w in pdf_norm if w]
    if len(pdf_norm) < SHINGLE or len(page.blocks) < 2:
        return page, []

    # where each whole PDF line starts in the word stream
    whole_lines, pos = {}, 0
    for line in lines:
        ws = [w for w in (normalize(t) for t in line["text"].split()) if w]
        if ws:
            whole_lines.setdefault(tuple(ws), []).append(pos)
            pos += len(ws)

    # where each run of SHINGLE consecutive words starts in the PDF
    where = {}
    for i in range(len(pdf_norm) - SHINGLE + 1):
        where.setdefault(tuple(pdf_norm[i:i + SHINGLE]), []).append(i)

    anchors = []
    for block in page.blocks:
        words = [w for w in (normalize(t) for t in _block_text(block).split()) if w]
        anchor = None
        if len(words) >= SHINGLE:
            hits = where.get(tuple(words[:SHINGLE]), [])
            if len(hits) == 1:
                anchor = hits[0]
        elif len(words) >= 2:
            # a short block (a heading): located only when its text is an ENTIRE line of the PDF that occurs once.
            # A fragment of a longer line does not count: text read out of a picture often repeats part of a caption.
            hits = whole_lines.get(tuple(words), [])
            if len(hits) == 1:
                anchor = hits[0]
        anchors.append(anchor)

    # a block with no anchor sorts right behind the block before it
    keys, last = [], -1.0
    for n, anchor in enumerate(anchors):
        last = float(anchor) if anchor is not None else last + 1e-6
        keys.append((last, n))
    order = [n for _, n in sorted(keys)]
    if order == list(range(len(order))):
        return page, []

    moves = []
    for new_pos, n in enumerate(order):
        if n != new_pos:
            moves.append(f"{_block_text(page.blocks[n])[:40]!r} moved from position {n} to {new_pos}")
    return Page(analysis=page.analysis, blocks=[page.blocks[n] for n in order]), moves


# ---------------------------------------------------------------------------------------------------------------
# Removing text the model wrote a second time
# ---------------------------------------------------------------------------------------------------------------
SPURIOUS_UNMATCHED = 0.8        # a block is a candidate when at least this share of its words lines up with nothing
SPURIOUS_SHINGLE = 4            # ... and it is a copy of real PDF text when this share of its 4-word runs occur in the PDF
SPURIOUS_REAL = 0.7
SPURIOUS_MIN_WORDS = 12         # shorter blocks are left alone: too little text to be sure


def drop_spurious_blocks(page, pdf_page):
    """Return (new_page, dropped). Remove blocks that repeat PDF text the model had already written elsewhere.

    On dense pages the model sometimes writes a paragraph a second time, or at the wrong place. The word alignment
    keeps the best-fitting copy, so the extra one lines up with nothing; yet its words are real PDF words, which tells
    it apart from text the model read out of a picture (that is not in the PDF at all and is kept). Tables and short
    blocks are never touched. If a dropped block was the only copy of some text, the restore step that follows puts
    the text back in its proper place, so this cannot lose content.
    """
    lines, _ = _pdf_lines(pdf_page)
    if not lines or len(page.blocks) < 2:
        return page, []
    pdf_norm, _lw, pdf_to_out, out_norm, out_pos, _bt = _align(page, lines)
    if len(pdf_norm) < SPURIOUS_SHINGLE:
        return page, []
    matched_out = set(pdf_to_out.values())
    pdf_shingles = {tuple(pdf_norm[i:i + SPURIOUS_SHINGLE]) for i in range(len(pdf_norm) - SPURIOUS_SHINGLE + 1)}

    by_block = {}
    for j, (bi, _ti) in enumerate(out_pos):
        by_block.setdefault(bi, []).append(j)

    drop, notes = set(), []
    for bi, block in enumerate(page.blocks):
        if isinstance(block, TableBlock) or block.kind == "image":
            continue
        js = by_block.get(bi, [])
        if len(js) < SPURIOUS_MIN_WORDS:
            continue
        if sum(1 for j in js if j not in matched_out) / len(js) < SPURIOUS_UNMATCHED:
            continue
        words = [out_norm[j] for j in js]
        runs = [tuple(words[i:i + SPURIOUS_SHINGLE]) for i in range(len(words) - SPURIOUS_SHINGLE + 1)]
        if not runs or sum(1 for r in runs if r in pdf_shingles) / len(runs) < SPURIOUS_REAL:
            continue                                   # not PDF text (e.g. read from a picture): keep
        drop.add(bi)
        notes.append(f"{block.text[:40]!r} ({len(js)} words) repeats PDF text written elsewhere: dropped")
    if not drop:
        return page, []
    return Page(analysis=page.analysis, blocks=[b for i, b in enumerate(page.blocks) if i not in drop]), notes
