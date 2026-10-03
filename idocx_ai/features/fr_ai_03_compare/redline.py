"""The marked-up copy of the new version: added text highlighted green, changed words
yellow, and a note where something was removed or moved. Each mark carries its change id."""

import pymupdf

from core.pdfutil import derived_name, origin_of, save_new

GREEN, YELLOW = (0.55, 0.9, 0.55), (1.0, 0.9, 0.3)
AUTHOR = "iDocx compare"


def _note(page, point, text):
    a = page.add_text_annot(point, text, icon="Comment")
    a.set_info(title=AUTHOR, content=text)
    a.update()


def _highlight(page, rect, color, text):
    a = page.add_highlight_annot(rect)
    a.set_colors(stroke=color)
    a.set_info(title=AUTHOR, content=text)
    a.update()


def _whole_word_hits(page, words: str, clip) -> list:
    """search_for matches inside words ("6" in "60"); for a single word, keep only hits
    that cover a whole word on the page."""
    hits = page.search_for(words, clip=clip)
    if " " in words.strip():
        return hits
    target = words.strip(".,;:()\"'").lower()
    on_page = [pymupdf.Rect(w[:4]) for w in page.get_text("words", clip=clip)
               if w[4].strip(".,;:()\"'").lower() == target]
    return [h for h in hits if any(abs(h.x0 - r.x0) < 2 and abs(h.x1 - r.x1) < 4 for r in on_page)] or \
        [h for h in hits if any(r.contains(h.tl + (1, 1)) for r in on_page)]


def build_redline(new_doc, new_meta: dict, changes: list[dict], new_units: list[dict]) -> dict:
    doc = pymupdf.open()
    doc.insert_pdf(new_doc)
    for c in changes:
        unit = c.get("_unit")
        label = f"{c['id']} {c['type']}: {c['where']}"
        if c["type"] == "removed":
            # The note goes where the removed text used to sit: just before the next unit that follows it.
            nxt = int(c["_new"] + 0.5)
            if nxt < len(new_units):
                page_no, (x0, y0, *_) = new_units[nxt]["blocks"][0]["page"], new_units[nxt]["blocks"][0]["rect"]
            else:
                page_no, x0, y0 = doc.page_count, 72, 760
            _note(doc[page_no - 1], (max(20, x0 - 30), y0), f"{label}\n\n{c['old_text']}")
            continue
        blocks = unit["blocks"]
        first = doc[blocks[0]["page"] - 1]
        if c["type"] == "added":
            for b in blocks:
                _highlight(doc[b["page"] - 1], pymupdf.Rect(b["rect"]), GREEN, label)
            continue
        marked = False
        # A heading-only first block ("6. Liability") holds the clause number, not changed wording.
        body = blocks[1:] if unit.get("head_only") else blocks
        for words in c.get("_added_words", []):
            for b in body:
                page = doc[b["page"] - 1]
                for hit in _whole_word_hits(page, words, pymupdf.Rect(b["rect"])):
                    _highlight(page, hit, YELLOW, f"{label}\n{c.get('diff', '')}")
                    marked = True
        x0, y0, *_ = blocks[0]["rect"]
        if not marked or c["type"].startswith("moved"):
            _note(first, (max(20, x0 - 30), y0), f"{label}\n{c.get('moved_note', '')}\n{c.get('diff', '')}".strip())
    return save_new(doc, derived_name(new_meta["name"], "changes"), [new_meta["id"]], "compare_documents",
                    page_origin=origin_of(new_meta))
