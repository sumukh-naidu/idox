"""FR-AI-02 extraction: the model reads the document and fills a schema; code checks every value.

Text comes from the text layer, or OCR for scans. If OCR fails on a page (too few words,
or mostly symbols), the vision model transcribes the page image instead, and anything
read that way is marked for checking.

The model must give, for each field, the value AND the exact quote it took it from.
A value counts as "found" only if the quote is in the source and the value is in the
quote; otherwise it is "unverified". Fields the document does not state stay null:
the schema allows null, and the prompt forbids guessing.
"""

import json
import re

from core import llm
from core.pdfutil import ToolError, open_pdf
from core.text import page_text

MIN_OCR_WORDS = 15         # fewer words than this on a scanned page: OCR failed
MIN_OCR_CLEAN = 0.6        # share of letters/digits among non-space characters below this: OCR failed
MAX_DOC_CHARS = 12000      # text sent to the model per extraction
VISION_LONG_SIDE = 1400    # px: legible text, about 1,400 image tokens


def ocr_failed(text: str) -> bool:
    chars = [c for c in text if not c.isspace()]
    clean = sum(c.isalnum() for c in chars) / len(chars) if chars else 0
    return len(text.split()) < MIN_OCR_WORDS or clean < MIN_OCR_CLEAN


def _vision_transcribe(page) -> str:
    zoom = VISION_LONG_SIDE / max(page.rect.width, page.rect.height)
    png = page.get_pixmap(matrix=__import__("pymupdf").Matrix(zoom, zoom)).tobytes("png")
    return llm.ask_about_image("Transcribe all the text on this page exactly, line by line, keeping numbers and "
                               "punctuation as printed. Output only the text.", png, max_tokens=1500)


def document_text(file_id: str, force_vision: bool = False) -> list[dict]:
    """[{page, text, source}] where source is 'text layer', 'OCR' or 'vision model'."""
    meta, doc = open_pdf(file_id)
    pages = []
    with doc:
        for page in doc:
            text, source = page_text(doc, file_id, page.number + 1)
            if source == "OCR" and (force_vision or ocr_failed(text)):
                text, source = _vision_transcribe(page), "vision model"
            pages.append({"page": page.number + 1, "text": text, "source": source})
    return pages


# ------------------------------------------------------------------ grounding

def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _numbers(s: str) -> list[str]:
    return [re.sub(r"[,\s]", "", n) for n in re.findall(r"\d[\d,\s]*(?:\.\d+)?", str(s))]


def ground(field: dict, value, quote, pages: list[dict]) -> dict:
    """Decide the status of one extracted value against the source pages."""
    if value is None or value == "" or (field["type"] == "checkbox" and value is False):
        return {"value": None, "status": "empty"}
    hit = next((p for p in pages if quote and _norm(quote) in _norm(p["text"])), None)
    if not hit:
        return {"value": value, "status": "unverified", "why": "its quote is not in the document"}
    if field["type"] == "checkbox":
        ok = value is True
    elif field["type"] == "choice":
        ok = _norm(value) in _norm(quote)
    else:
        v = str(value)
        ok = _norm(v) in _norm(quote) or (bool(_numbers(v)) and all(n in _numbers(quote) for n in _numbers(v)))
    if not ok:
        return {"value": value, "status": "unverified", "why": "the value is not in its quote"}
    status = "check: read from the page image" if hit["source"] == "vision model" else "found"
    return {"value": value, "status": status, "page": hit["page"], "quote": quote, "read_by": hit["source"]}


# ------------------------------------------------------------------ the model call

PROMPT = ("Fill the fields below from the document. For each field give the value and the exact quote from the "
          "document it comes from (the full line or phrase, copied character for character). Copy values exactly as "
          "written: same spelling, digits and punctuation. If the document does not state a field, give null for "
          "both; never guess, infer or calculate. A choice field takes one of its options only if the document "
          "states it. A checkbox is true only if the document clearly shows it, otherwise null.\n\nFields:\n")


def _schema(fields: list[dict]) -> dict:
    props = {}
    for f in fields:
        if f["type"] == "checkbox":
            value = {"type": ["boolean", "null"]}
        elif f["type"] == "choice" and f.get("options"):
            value = {"type": ["string", "null"], "enum": [*f["options"], None]}
        else:
            value = {"type": ["string", "null"]}
        props[f["name"]] = {"type": "object", "properties": {"value": value, "quote": {"type": ["string", "null"]}},
                            "required": ["value", "quote"]}
    return {"type": "object", "properties": props, "required": list(props)}


def extract_values(fields: list[dict], pages: list[dict]) -> dict:
    """{field name: {value, status, page?, quote?, why?}} for the fields asked."""
    listing = "\n".join(f"- {f['name']}: {f['label']}" + (f" (one of: {', '.join(f['options'])})" if f.get("options")
                                                          else " (true/false)" if f["type"] == "checkbox" else "")
                        for f in fields)
    doc = "\n\n".join(f"--- page {p['page']} ({p['source']}) ---\n{p['text']}" for p in pages)[:MAX_DOC_CHARS]
    r = llm.chat([{"role": "user", "content": f"{PROMPT}{listing}\n\nDocument:\n{doc}"}], temperature=0,
                 max_tokens=250 + 70 * len(fields),
                 response_format={"type": "json_schema", "json_schema": {"name": "fields", "schema": _schema(fields)}})
    try:
        answer = json.loads(r["choices"][0]["message"].get("content") or "{}")
    except json.JSONDecodeError:
        raise ToolError("the model's answer was not valid JSON; try again")
    out = {}
    for f in fields:
        a = answer.get(f["name"]) or {}
        out[f["name"]] = ground(f, a.get("value"), a.get("quote"), pages)
    return out


# ------------------------------------------------------------------ tables

TABLE_SCHEMA = {"type": "object", "properties": {
    "header": {"type": "array", "items": {"type": "string"}},
    "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
}, "required": ["header", "rows"]}


def _vision_table(doc, page: dict) -> dict | None:
    """A table on a scanned page, read by the vision model from the page image.

    OCR reads ruled tables badly: on the test invoice it garbled the header and dropped a
    whole row, because the grid lines confuse it. So OCR only cross-checks the cells here:
    cells it also saw are confirmed, the rest are marked to be checked.
    """
    import base64

    import pymupdf
    pg = doc[page["page"] - 1]
    zoom = VISION_LONG_SIDE / max(pg.rect.width, pg.rect.height)
    url = "data:image/png;base64," + base64.b64encode(pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
                                                      .tobytes("png")).decode()
    r = llm.chat([{"role": "user", "content": [
        {"type": "text", "text": "If this page has a table, copy its header and every row exactly, cell by cell, as "
                                 "printed. If there is no table, return an empty header and no rows."},
        {"type": "image_url", "image_url": {"url": url}}]}], temperature=0, max_tokens=1200,
        response_format={"type": "json_schema", "json_schema": {"name": "table", "schema": TABLE_SCHEMA}})
    try:
        t = json.loads(r["choices"][0]["message"].get("content") or "{}")
    except json.JSONDecodeError:
        return None
    if not t.get("rows"):
        return None
    ocr = _norm(page["text"])
    unconfirmed = [c for row in [t["header"], *t["rows"]] for c in row if c.strip() and _norm(c) not in ocr]
    out = {"page": page["page"], "header": t["header"], "rows": t["rows"], "read_by": "vision model (page image)"}
    if unconfirmed:
        out["check_these_cells"] = {"cells": unconfirmed, "why": "read from the image; OCR could not confirm them"}
    return out


def tables(file_id: str, pages: list[dict]) -> list[dict]:
    """Ruled tables on digital pages, read exactly by PyMuPDF; on scans, read by the vision model and cross-checked."""
    meta, doc = open_pdf(file_id)
    out = []
    with doc:
        for p in pages:
            if p["source"] == "text layer":
                for t in doc[p["page"] - 1].find_tables().tables:
                    rows = t.extract()
                    if len(rows) >= 2:
                        out.append({"page": p["page"], "header": rows[0], "rows": rows[1:], "read_by": "table layout"})
            elif (t := _vision_table(doc, p)):
                out.append(t)
    return out


def _as_fields(fields: list[str]) -> list[dict]:
    out, seen = [], set()
    for label in fields:
        name = re.sub(r"\W+", "_", str(label).strip().lower()).strip("_") or "field"
        while name in seen:
            name += "_2"
        seen.add(name)
        out.append({"name": name, "label": str(label), "type": "text"})
    return out


def extract_information(file_id: str, fields: list[str], include_tables: bool = False) -> dict:
    if not isinstance(fields, list) or not fields:
        raise ToolError('fields must be a list like ["invoice number", "invoice date", "total amount"]')
    pages = document_text(file_id)
    specs = _as_fields(fields)
    values = extract_values(specs, pages)
    result = {"file_id": file_id,
              "values": [{"field": s["label"], **values[s["name"]]} for s in specs],
              "read_by": sorted({p["source"] for p in pages}),
              "next_step": ("present the found values; show unverified ones as needing a check and say why; never "
                            "fill in empty ones yourself")}
    if include_tables:
        result["tables"] = tables(file_id, pages)
    if any(p["source"] != "text layer" for p in pages):
        result["warning"] = "some pages were read by OCR or the vision model: check numbers carefully"
    return result
