"""FR-AI-01 reading: hand the model a document's text, and check its task/deadline claims.

Digital pages are read from the text layer; scanned pages through OCR. verify_tasks
is the grounding check: a task or deadline counts only if the quote it cites is
really on the page, and the deadline is inside that quote.
"""

import re

from core.pdfutil import ToolError, open_pdf, page_ranges, parse_page_spec
from core.text import page_text as _page_text

MAX_CHARS = 10000      # per read_document call; the agent's context is 16K tokens


def read_document(file_id: str, pages: str | None = None) -> dict:
    meta, doc = open_pdf(file_id)
    with doc:
        n = doc.page_count
        wanted = parse_page_spec(pages, n) if pages else list(range(1, n + 1))
        out, used, rest = [], 0, []
        for i, p in enumerate(wanted):
            text, source = _page_text(doc, file_id, p)
            if out and used + len(text) > MAX_CHARS:
                rest = wanted[i:]
                break
            if len(text) > MAX_CHARS:
                text = text[:MAX_CHARS] + " [rest of page cut]"
            out.append({"page": p, "source": source, "text": text})
            used += len(text)
    result = {"file_id": file_id, "name": meta["name"], "pages_in_file": n,
              "pages_returned": page_ranges([o["page"] for o in out]), "pages": out,
              # In the result, not only the prompt: the model follows a tool result more reliably.
              "next_step": ("answer only from this text. If your answer will list tasks, deadlines or due "
                            "dates (however the question is worded), first send every candidate to "
                            "verify_tasks with its exact sentence as the quote and the deadline as written, "
                            "and present only verified items. Founding dates, past events and issue dates "
                            "are not deadlines.")}
    if rest:
        result["more"] = (f"capped at {MAX_CHARS} characters; call again with "
                          f'pages="{page_ranges(rest)}" for the rest')
    return result


def _norm(s: str) -> str:
    s = str(s).lower()
    s = re.sub(r"[‘’´`]", "'", s)
    s = re.sub(r"[“”]", '"', s)
    s = re.sub(r"[‐-―−]", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def verify_tasks(file_id: str, items: list[dict]) -> dict:
    if not isinstance(items, list) or not items:
        raise ToolError('items must be a list like [{"task": ..., "deadline": ..., "quote": ..., "page": 1}]')
    meta, doc = open_pdf(file_id)
    with doc:
        texts = {p: _norm(_page_text(doc, file_id, p)[0]) for p in range(1, doc.page_count + 1)}

    verified, rejected = [], []
    for item in items:
        if not isinstance(item, dict) or not item.get("task") or not item.get("quote"):
            rejected.append({"item": item, "reason": "each item needs a task and the exact quote it comes from"})
            continue
        quote = _norm(item["quote"])
        found = [p for p, t in texts.items() if quote in t]
        if not found:
            rejected.append({**item, "reason": "quote not found in the document; copy it exactly"})
            continue
        deadline = item.get("deadline")
        if deadline and _norm(deadline) not in quote:
            rejected.append({**item, "reason": "the deadline does not appear in the quote; copy it as written"})
            continue
        verified.append({**item, "page": item.get("page") if item.get("page") in found else found[0]})
    return {"verified": verified, "rejected": rejected,
            "note": "present only the verified items; say how many were dropped and why"}
