"""FR-AI-05: run the detectors (and, optionally, the model) over every page and find
where each item sits.

Digital pages use their text layer; scanned pages use OCR, whose word boxes say where
to black out the image. Positions are what the preview highlights and the redaction
removes. Raw values stay here; callers get masked ones.
"""

from core.pdfutil import open_pdf
from core.text import textpage

from .detectors import find_in_text, mask
from .people import find_people


def _rects(page, value: str, tp) -> list[tuple]:
    rects = []
    for part in (ln.strip() for ln in value.splitlines() if ln.strip()):
        rects += [tuple(round(v, 1) for v in r) for r in page.search_for(part, textpage=tp)]
    return rects


def scan_document(file_id: str, use_model: bool = True) -> tuple[list[dict], list[int], int]:
    """(items with raw values and rects, pages read by OCR, model candidates discarded as not in the text)."""
    meta, doc = open_pdf(file_id)
    items, ocr, dropped = [], [], 0
    with doc:
        for page in doc:
            tp, source = textpage(page)
            if not tp:
                continue
            if source == "OCR":
                ocr.append(page.number + 1)
            text = page.get_text("text", textpage=tp)
            found, seen = [], set()
            for f in find_in_text(text):
                key = (f["kind"], " ".join(f["value"].split()))
                if key not in seen:
                    seen.add(key)
                    found.append({**f, "rects": _rects(page, f["value"], tp)})
            if use_model:
                people, n = find_people(page, tp, " ".join(f["value"] for f in found))
                found += people
                dropped += n
            for f in found:
                items.append({**f, "page": page.number + 1, "read_by": source,
                              "masked": mask(f["kind"], f["value"]), "located": bool(f["rects"])})
    for i, it in enumerate(items, 1):
        it["id"] = f"S{i}"
    return items, ocr, dropped
