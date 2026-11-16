"""crop_reread.py -- win back content the model dropped from a SCANNED page, by re-reading only that part.

The problem (KNOWN_ISSUES.md #1): on a scanned page the model sometimes stops early. It plans the page
(LayoutAnalysis.n_blocks), miscounts, and obeys the schema's "output exactly this many blocks". On
agreement_scanned.pdf and scan_table_low.pdf it wrote 8 blocks and left out the table and the footer. The
coverage check sees it (Tesseract finds lines the model did not return) but only reports it.

The fix: the dropped lines are located on the page, the region around them is cropped out of the page image,
and the model reads that crop as if it were a page of its own. A crop holds one or two blocks, so there is no
long count to get wrong.

Rules this keeps:
- OCR is a checker, never an author. Tesseract finds WHERE the model left something out; what goes into the file
  is only what the model reads from the crop. No OCR text is ever pasted in.
- Nothing is accepted blindly. A re-read is kept only if it brings back at least one of the missing lines, and
  blocks that repeat text already extracted are dropped.
- Every step is reported, so how often the model drops things stays visible.
- Cost: one extra model call per run of dropped lines (usually one), only on pages where something is missing.
"""
import os
import re
import time
from types import SimpleNamespace

from PIL import Image, ImageOps

import ocr_layout
from blocks import LayoutAnalysis, Page, TableBlock, extract_page, normalize, squash

MIN_LINE = 8      # same rule as blocks.check_coverage: shorter lines are page numbers and cell fragments
MAX_RUNS = 3      # at most this many extra model calls per pass
MAX_ROUNDS = 2    # passes per page: the second picks up what the first left behind
EDGE_PAD = 1.0    # crop margin, in median line heights, never reaching into a neighbouring line the model wrote
MARGIN = 0.07     # white border added around the crop, as a share of the page width


def _produced(page):
    parts = []
    for b in page.blocks:
        if isinstance(b, TableBlock):
            parts.extend(b.header)
            parts.extend(c for row in b.rows for c in row)
        else:
            parts.append(b.text)
    joined = " ".join(parts)
    return normalize(joined), squash(joined)


def _tokens(text):
    return set(re.findall(r"[a-z0-9][a-z0-9.,%]*", normalize(text)))


def _confirmed(text, authority):
    """Is this OCR line one the coverage check also reported missing?

    Two Tesseract passes read the same page slightly differently (a one-digit table cell can vanish from one),
    so a line counts as missing only when the coverage check, which already decided the page has gaps, names
    it too. That keeps a quirk of this second reading from triggering a model call.
    """
    mine = _tokens(text)
    return bool(mine) and any(len(mine & _tokens(a)) / len(mine) >= 0.7 for a in authority)


def _state(lines, page, authority=None):
    """For each OCR line: 'missing' (the model left it out), 'short' (too short to judge) or 'present'.

    `authority`: the lines blocks.check_coverage reported missing. When given, only those can be 'missing'.
    """
    produced, squashed = _produced(page)
    out = []
    for ln in lines:
        n = normalize(ln["text"])
        if len(n) < MIN_LINE:
            out.append("short")
        elif n in produced or squash(ln["text"]) in squashed:
            out.append("present")
        elif authority is not None and not _confirmed(ln["text"], authority):
            out.append("present")
        else:
            out.append("missing")
    return out


def _runs(states):
    """Groups of neighbouring OCR line indices the model left out. A 'present' line ends a group."""
    runs, cur = [], []
    for i, s in enumerate(states + ["present"]):
        if s == "present":
            while cur and states[cur[-1]] == "short":
                cur.pop()
            if cur:
                runs.append(cur)
            cur = []
        elif s == "missing" or cur:
            cur.append(i)
    return runs


def _anchor(block):
    if isinstance(block, TableBlock):
        return " ".join(block.header) or " ".join(block.rows[0] if block.rows else [])
    return block.text


def _insert_index(page, lines, top):
    """Where in the block list a run starting at pixel row `top` belongs: after the last block that sits above it."""
    used, last = set(), -1
    for bi, b in enumerate(page.blocks):
        li, _ = ocr_layout.match_block(SimpleNamespace(text=_anchor(b)), lines, used)
        if li is not None:
            used.add(li)
            if lines[li]["top"] < top:
                last = bi
    return last + 1


def _squeeze(im, keep, min_gap):
    """Shorten long blank stretches (a table and a footer far below it) so the model sees one compact fragment."""
    w, h = im.size
    blank = [im.crop((0, y, w, y + 1)).getextrema()[0] > 245 for y in range(h)]
    bands, y = [], 0
    while y < h:
        j = y
        while j < h and blank[j] == blank[y]:
            j += 1
        bands.append((y, j, blank[y]))
        y = j
    parts = [im.crop((0, a, w, a + (keep if gap and b - a > min_gap else b - a))) for a, b, gap in bands]
    out = Image.new("L", (w, sum(p.height for p in parts)), 255)
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.height
    return out


def _crop(image_path, lines, run, tag):
    """Cut the run out of the page image and present it like a small page: white margin on every side.

    The margin matters. A crop whose table touched the image edge came back from the model as a table with 0
    columns and no blocks; the same crop with a white margin was read correctly (2026-10-05, scan_table_low).
    """
    img = Image.open(image_path).convert("L")
    heights = sorted(l["h"] for l in lines)
    pad = int(EDGE_PAD * heights[len(heights) // 2])
    first, last = lines[run[0]], lines[run[-1]]
    top, bottom = first["top"] - pad, last["bottom"] + pad
    # Never reach into a line the model already wrote: stop halfway to it.
    if run[0] > 0:
        top = max(top, (lines[run[0] - 1]["bottom"] + first["top"]) // 2)
    if run[-1] + 1 < len(lines):
        bottom = min(bottom, (last["bottom"] + lines[run[-1] + 1]["top"]) // 2)
    top, bottom = max(0, int(top)), min(img.height, int(bottom))
    piece = _squeeze(img.crop((0, top, img.width, bottom)), keep=int(0.04 * img.width), min_gap=int(0.10 * img.width))
    piece = ImageOps.expand(piece, border=int(MARGIN * img.width), fill=255)
    path = os.path.splitext(image_path)[0] + f"_reread{tag}.png"
    piece.save(path)
    return path, top, bottom


def _merge(page, new_blocks, at):
    """Insert the re-read blocks at index `at`, leaving out any that repeat what the page already has."""
    seen = {normalize(b.text) for b in page.blocks if not isinstance(b, TableBlock) and b.text.strip()}
    tables = {normalize(" ".join(b.header + (b.rows[0] if b.rows else []))) for b in page.blocks
              if isinstance(b, TableBlock)}
    keep = []
    for b in new_blocks:
        if isinstance(b, TableBlock):
            if normalize(" ".join(b.header + (b.rows[0] if b.rows else []))) in tables:
                continue
        elif not b.text.strip() or normalize(b.text) in seen:
            continue
        keep.append(b)
    blocks = list(page.blocks[:at]) + keep + list(page.blocks[at:])
    counts = [b.n_cols for b in blocks if isinstance(b, TableBlock)]
    return Page(analysis=LayoutAnalysis(n_blocks=len(blocks), table_column_counts=counts[:10]), blocks=blocks), keep


def _round(page, lines, image_path, model, base_url, include_look, reader, missing_lines):
    """One pass: re-read every run of missing lines once. Returns (page, notes); the same page object when
    nothing was missing or nothing helped."""
    states = _state(lines, page, missing_lines)
    runs = sorted(_runs(states), key=len, reverse=True)[:MAX_RUNS]
    if not runs:
        return page, []
    runs.sort()                                        # top of the page first, for the log
    where = [_insert_index(page, lines, lines[r[0]]["top"]) for r in runs]
    notes = []
    for n, run in reversed(list(enumerate(runs))):     # bottom first, so earlier insert positions stay valid
        path, top, bottom = _crop(image_path, lines, run, n + 1)
        started = time.time()
        label = f"run {n + 1} ({len(run)} line(s), pixel rows {top}-{bottom})"
        try:
            got = reader(path, model=model, base_url=base_url, include_look=include_look)
        except Exception as exc:
            notes.append(f"{label}: the model could not read the crop ({exc}); left as is")
            continue
        merged, added = _merge(page, got.blocks, where[n])
        before = _state(lines, page, missing_lines).count("missing")
        after = _state(lines, merged, missing_lines).count("missing")
        took = time.time() - started
        if added and after < before:
            tables = sum(isinstance(b, TableBlock) for b in added)
            notes.append(f"{label}: re-read in {took:.0f}s, added {len(added)} block(s)"
                         f"{f' including {tables} table(s)' if tables else ''}; "
                         f"missing lines {before} -> {after}")
            page = merged
        else:
            notes.append(f"{label}: re-read in {took:.0f}s did not bring any of the missing lines back; left as is")
    notes.reverse()
    return page, notes


def recover(page, image_path, model, base_url, include_look=True, reader=extract_page, missing_lines=None):
    """Re-read the parts of a scanned page the model left out.

    `missing_lines`: what blocks.check_coverage reported (the caller already has it). Returns (page, notes).
    `notes` holds one line per step for the log; the page is returned unchanged when nothing was missing or no
    re-read helped. `reader` is the model call, replaceable for tests.

    Up to MAX_ROUNDS passes. A second pass picks up what the first left behind: on scan_table_low the model
    returned the table but wrote the footer as an empty paragraph; once the table was in, the footer was a run
    of its own, and a crop of that single line was read correctly (4.7 s).
    """
    try:
        _, lines = ocr_layout.ocr_lines(image_path)
    except Exception as exc:
        return page, [f"re-read skipped: could not locate the missing lines ({exc})"]
    if not lines:
        return page, []
    notes = []
    for round_no in range(MAX_ROUNDS):
        better, new_notes = _round(page, lines, image_path, model, base_url, include_look, reader, missing_lines)
        notes += [f"round {round_no + 1}, {n}" for n in new_notes] if MAX_ROUNDS > 1 and new_notes else new_notes
        if better is page:
            break                                      # nothing was missing, or nothing helped: stop
        page = better
    return page, notes
