"""FR-AI-05: find signatures -- a PDF signature form field (digital) or a hand-drawn mark
under a "Signature" label (ink strokes, as opposed to a flat ruled line for someone to sign
on). Only the box and a confidence are returned; nothing about a signature is "the value",
so there is nothing to mask. The user decides what a flagged mark actually is.
"""

import re

import pymupdf

LABEL = re.compile(r"\bsign(ature|ed)\b", re.I)
BELOW = 48    # how far under a "Signature" label a mark still counts as belonging to it
SIDE = 220    # how far right of the label's left edge


def _labels(page, tp) -> list[pymupdf.Rect]:
    rects = []
    for b in page.get_text("dict", textpage=tp)["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            text = "".join(s["text"] for s in line["spans"])
            if LABEL.search(text):
                rects.append(pymupdf.Rect(line["bbox"]))
    return rects


def _is_ink(drawing) -> bool:
    """A flat ruled line to sign on is a single straight segment; a signature already made
    has several strokes or real vertical variation -- that is what tells the two apart."""
    rect = drawing["rect"]
    return len(drawing["items"]) > 1 or rect.height > 3


def find_signatures(page, tp=None) -> list[dict]:
    labels = _labels(page, tp)
    items = []
    for w in page.widgets() or []:
        if w.field_type_string == "Signature":
            items.append({"kind": "digital signature field", "category": "signature",
                          "value": w.field_name or "signature field", "rects": [tuple(round(v, 1) for v in w.rect)],
                          "basis": "a PDF signature form field", "confidence": 90})
    if not labels:
        return items
    near = lambda r, lb: lb.x0 - 10 <= r.x0 <= lb.x0 + SIDE and -lb.height <= r.y0 - lb.y1 <= BELOW  # noqa: E731
    for d in page.get_drawings():
        if not _is_ink(d):
            continue
        rect = d["rect"]
        if any(near(rect, lb) for lb in labels):
            items.append({"kind": "handwritten signature", "category": "signature",
                          "value": "(ink mark, no text)", "rects": [tuple(round(v, 1) for v in rect)],
                          "basis": "ink strokes under a 'Signature' label", "confidence": 55})
    return items
