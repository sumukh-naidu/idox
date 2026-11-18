"""FR-PRD-105: heading levels, reading order and tables -- suggestions only, never a decision
(rule: "Return a suggestion with confidence, not a decision"), built from the visible layout
since most files have no real tag structure for a screen reader to read in the first place.
"""

import re
from collections import Counter

# A line this many times the body text's size may be a heading; level 1 needs the largest
# ratio, each later level a smaller one. Relative to body size, not a fixed point size, so
# it works across documents that set their body text at different sizes.
LEVEL_RATIOS = [(2.0, 1), (1.6, 2), (1.3, 3), (1.15, 4), (1.08, 5)]
MAX_HEADING_WORDS = 14


def _lines(doc):
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                text = " ".join(s["text"] for s in line["spans"]).strip()
                size = max((s["size"] for s in line["spans"]), default=0)
                if text and size:
                    yield page.number + 1, text, size


def _body_size(doc) -> float:
    """The most common text size by character count, not by line count: a paragraph's few
    long lines should outweigh a table's many short cells when picking what "body text" is."""
    sizes: Counter = Counter()
    for _page, text, size in _lines(doc):
        sizes[round(size)] += len(text)
    return sizes.most_common(1)[0][0] if sizes else 0


def _level_for(size: float, body: float) -> int | None:
    if not body:
        return None
    ratio = size / body
    for min_ratio, level in LEVEL_RATIOS:
        if ratio >= min_ratio:
            return level
    return None


def heading_candidates(doc) -> list[dict]:
    """Heuristic headings, in document order: lines noticeably larger than body text, with
    a level guessed from how much larger. Not read from real tags -- a suggestion only."""
    body = _body_size(doc)
    out = []
    for page, text, size in _lines(doc):
        if len(text.split()) > MAX_HEADING_WORDS:
            continue
        level = _level_for(size, body)
        if level:
            out.append({"page": page, "text": text, "level": level, "size": round(size, 1)})
    return out


def heading_anomalies(headings: list[dict]) -> list[dict]:
    """A heading more than one level deeper than the heading before it skips a level (the
    brief's own example: H1 straight to H3). Flagged for the user to confirm, not decided."""
    out = []
    prev = None
    for h in headings:
        if prev and h["level"] > prev["level"] + 1:
            skipped = f"H{prev['level'] + 1}"
            if h["level"] > prev["level"] + 2:
                skipped += f"-H{h['level'] - 1}"
            out.append({"after": {"page": prev["page"], "text": prev["text"], "level": prev["level"]},
                        "jumps_to": {"page": h["page"], "text": h["text"], "level": h["level"]},
                        "reason": f"H{prev['level']} is followed by H{h['level']}, skipping {skipped}"})
        prev = h
    return out


def suggested_reading_order(doc) -> list[dict]:
    """Top-to-bottom, left-to-right block order per page: a suggestion for an untagged
    document. The real order should come from tags once the document is re-authored."""
    out = []
    for page in doc:
        blocks = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
        blocks.sort(key=lambda b: (round(b["bbox"][1] / 6), b["bbox"][0]))
        for i, b in enumerate(blocks, 1):
            text = " ".join(s["text"] for line in b["lines"] for s in line["spans"]).strip()
            if text:
                out.append({"page": page.number + 1, "order": i,
                           "text": text[:80] + ("..." if len(text) > 80 else "")})
    return out


def table_pages(doc) -> list[int]:
    """Pages holding a visible table (ruling lines and aligned text), regardless of tags."""
    out = []
    for page in doc:
        if page.find_tables().tables:
            out.append(page.number + 1)
    return out


def suggest_structure(file_id: str) -> dict:
    """Headings, level skips and a reading order for one PDF -- all suggestions for the author
    to confirm when the document is re-authored with real tags; never a decision made for them."""
    from core.pdfutil import open_pdf  # imported here: checks.py imports this module

    meta, doc = open_pdf(file_id)
    with doc:
        headings = heading_candidates(doc)
        return {"file_id": file_id, "name": meta["name"], "headings": headings,
                "level_anomalies": heading_anomalies(headings),
                "suggested_reading_order": suggested_reading_order(doc),
                "note": "guessed from text size and position, not read from real tags; the author decides"}


def tagged_table_headers(doc) -> tuple[int, int]:
    """(tables, /TH tags) found in the real structure tree, for a tagged document."""
    tables = th = 0
    for x in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(x, compressed=True)
        except RuntimeError:                        # not every xref holds a dictionary
            continue
        tables += bool(re.search(r"/S\s*/Table\b", obj))
        th += bool(re.search(r"/S\s*/TH\b", obj))
    return tables, th
