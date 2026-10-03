"""FR-AI-05: the model finds what rules cannot, people's names and postal addresses.

The model reads one page's text and answers in JSON forced by a schema (llama-server
constrains decoding to it). Every candidate is then checked in code: it is kept only
if it appears in the page text exactly (spacing aside) and can be located on the page.
Anything else is discarded and counted, so a made-up name can never be proposed.
"""

import json
import re

from core import llm

MAX_PAGE_CHARS = 6000

SCHEMA = {"type": "object", "properties": {
    "names": {"type": "array", "items": {"type": "string"}},
    "addresses": {"type": "array", "items": {"type": "string"}},
}, "required": ["names", "addresses"]}

PROMPT = ("Below is the text of one page of a document. List every PERSON's full name and every postal "
          "address in it. Copy each exactly as written, character for character. Do not include company "
          "names, labels such as 'Name:', or anything not in the text. If there are none, return empty lists.")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def ask_model(page_text: str) -> dict:
    r = llm.chat([{"role": "user", "content": f"{PROMPT}\n\n---\n{page_text[:MAX_PAGE_CHARS]}"}],
                 temperature=0, max_tokens=500,
                 response_format={"type": "json_schema", "json_schema": {"name": "people", "schema": SCHEMA}})
    try:
        out = json.loads(r["choices"][0]["message"].get("content") or "{}")
    except json.JSONDecodeError:
        return {"names": [], "addresses": []}
    return {k: [str(x) for x in out.get(k, []) if isinstance(x, (str, int))] for k in ("names", "addresses")}


def locate(page, text: str, tp) -> list[tuple]:
    """Rects for text on the page; an address broken over lines is located line by line."""
    rects = page.search_for(text, textpage=tp)
    if not rects:
        target = _norm(text)
        for line in page.get_text("text", textpage=tp).splitlines():
            if len(line.strip()) >= 6 and _norm(line) in target:
                rects += page.search_for(line.strip(), textpage=tp)
    return [tuple(round(v, 1) for v in r) for r in rects]


def find_people(page, tp, taken_text: str) -> tuple[list[dict], int]:
    """(grounded name/address items, how many candidates were discarded as not in the text)."""
    text = page.get_text("text", textpage=tp)
    if not text.strip():
        return [], 0
    answer = ask_model(text)
    page_norm, items, dropped = _norm(text), [], 0
    for kind, values in (("person name", answer["names"]), ("postal address", answer["addresses"])):
        for v in dict.fromkeys(v.strip() for v in values):
            if len(v) < 3 or _norm(v) not in page_norm or _norm(v) in _norm(taken_text):
                dropped += len(v) >= 3 and _norm(v) not in page_norm
                continue
            rects = locate(page, v, tp)
            if not rects:
                dropped += 1
                continue
            items.append({"kind": kind, "category": "personal", "value": v, "rects": rects,
                          "basis": "found by the model, checked as exact text", "confidence": "medium"})
    return items, dropped
