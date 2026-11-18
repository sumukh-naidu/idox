"""FR-AI-06 summarisation: make sure the summary is written from the WHOLE document, not a few pages.

A document that fits the budget is handed over whole. A longer one is read in parts: the model writes short
notes for each part (every page lands in exactly one part), and the notes of all parts go to the chat model to
write the final summary. The result says which pages each part covers, so the answer can be checked.
"""

import re
from collections import Counter

from core import llm
from core.pdfutil import ToolError, open_pdf, page_ranges
from core.text import page_text

from .qa import TEXT_BUDGET, _STOP, _numbers_in, _parse_pages

PART_CHARS = 12000      # about 3,000 tokens of page text per part
MAX_PARTS = 40          # about 480,000 characters; beyond that the user picks a page range
NOTES_MAX_TOKENS = 800
NOTES_PROMPT = ("You are making notes for a summary of a document. Below is part of it (pages {pages}). Write notes "
                "of 120 to 200 words covering EVERYTHING important in this part, in order: topics, key facts, "
                "figures, names, dates and decisions, exactly as written. Use only this text; add nothing. "
                "Reply with the notes only.\n\n{text}")


def _parts(texts: dict[int, str]) -> list[list[int]]:
    """Group consecutive pages into parts of about PART_CHARS; every page is in exactly one part."""
    parts, current, size = [], [], 0
    for n in sorted(texts):
        if current and size + len(texts[n]) > PART_CHARS:
            parts.append(current)
            current, size = [], 0
        current.append(n)
        size += len(texts[n])
    return parts + [current] if current else parts


def _write_notes(text: str, pages: str) -> str:
    resp = llm.chat([{"role": "user", "content": NOTES_PROMPT.format(pages=pages, text=text)}],
                    temperature=0, max_tokens=NOTES_MAX_TOKENS)
    return (resp["choices"][0]["message"].get("content") or "").strip()


def summarize_document(file_id: str, pages: str | None = None, notes_writer=None) -> dict:
    write = notes_writer or _write_notes
    meta, doc = open_pdf(file_id)
    with doc:
        wanted = _parse_pages(pages, doc.page_count)
        total = doc.page_count
        texts = {n: page_text(doc, file_id, n) for n in wanted}
    body = {n: t for n, (t, _) in texts.items()}
    if not any(body.values()):
        raise ToolError("the file has no readable text, so there is nothing to summarise")

    base = {"file_id": file_id, "name": meta["name"], "pages_in_file": total, "pages_summarised": page_ranges(wanted)}
    instruction = ("Write the summary only from this. Cover every part, in order, and do not add anything that is "
                   "not here. Keep the numbers, dates and names exactly as written. A one-line overview first, then "
                   "short bullets; about 1 bullet per page for short documents, at most 12 for long ones.")

    if sum(len(t) for t in body.values()) <= TEXT_BUDGET:
        return {**base, "mode": "whole_text", "complete": True,
                "pages": [{"page": n, "read_by": texts[n][1], "text": body[n]} for n in wanted],
                "how_to_summarise": instruction}

    parts = _parts(body)
    if len(parts) > MAX_PARTS:
        raise ToolError(f"the document is too long to summarise in one go ({len(parts)} parts of text); "
                        f"ask for a page range, for example pages 1-60")
    out = []
    for part in parts:
        label = page_ranges(part)
        text = "\n\n".join(f"[Page {n}]\n{body[n]}" for n in part)
        notes = write(text, label)
        out.append({"pages": label, "notes": notes or "(no notes could be made for this part)",
                    "notes_made": bool(notes)})
    return {**base, "mode": "notes", "complete": all(p["notes_made"] for p in out), "parts": out,
            "how_to_summarise": instruction + " The parts are notes made from the pages named; the whole "
                                              "document is covered."}


# ---- Checking a finished summary ------------------------------------------------------------------------

_SUMMARY_WORDS = re.compile(r"\b(?:summar\w*|overview|gist|tl;?dr|key points|main points)\b", re.I)
_NUMBER = re.compile(r"\d[\d,.]*\d|\d")


def wants_summary(question: str) -> bool:
    return bool(_SUMMARY_WORDS.search(question or ""))


def _top_words(text: str, n: int = 8) -> list[str]:
    words = [w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP]
    return [w for w, _ in Counter(words).most_common(n)]


def check_summary(answer: str, calls: list) -> dict | None:
    """Was the whole document read, do the numbers match it, does the summary touch every part?"""
    reads = [(args, out["result"]) for name, args, out in calls if name == "summarize_document" and out.get("ok")]
    if not reads:
        return None
    args, result = reads[-1]
    _, doc = open_pdf(result["file_id"])
    with doc:
        wanted = _parse_pages(args.get("pages"), doc.page_count)
        body = {n: page_text(doc, result["file_id"], n)[0] for n in wanted}
    problems = []
    if not result["complete"]:
        problems.append("some parts of the document could not be read, so the summary is partial")

    in_doc = _numbers_in(" ".join(body.values()))
    missing = sorted(n for n in _numbers_in(answer) if len(n) > 1 and n not in in_doc)
    if missing:
        problems.append(f"{', '.join(missing)} not found in the document")

    # Parts: each should leave at least one of its most frequent words in the summary.
    groups = _parts(body)
    lowered = answer.lower()
    untouched = [page_ranges(g) for g in groups
                 if len(groups) > 1 and not any(w in lowered for w in _top_words(" ".join(body[n] for n in g)))]
    if untouched:
        problems.append("nothing in the summary seems to come from pages " + "; ".join(untouched))
    note = f"summarised {len(wanted)} page(s) of {result['pages_in_file']}, in {len(groups)} part(s)"
    return {"validation": {"status": "check" if problems else "verified", "problems": problems, "note": note}}
