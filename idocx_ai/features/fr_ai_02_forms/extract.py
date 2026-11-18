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

import pymupdf

from core import llm, rules
from core.pdfutil import ToolError, open_pdf
from core.text import page_text

from . import evidence, templates

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
            pages.append({"page": page.number + 1, "text": text, "source": source, "file_id": file_id})
    return pages


# ------------------------------------------------------------------ grounding

def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _numbers(s: str) -> list[str]:
    return [re.sub(r"[,\s]", "", n) for n in re.findall(r"\d[\d,\s]*(?:\.\d+)?", str(s))]


def ground(field: dict, value, quote, pages: list[dict]) -> dict:
    """Decide the status of one extracted value against the source pages."""
    if value is None or value == "" or (field["type"] == "checkbox" and value is False):
        return {"value": None, "status": "not_found"}
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
        item = {"type": "object", "properties": {"value": value, "quote": {"type": ["string", "null"]}},
                "required": ["value", "quote"]}
        props[f["name"]] = {"type": "object", "properties": {"candidates": {"type": "array", "maxItems": 3, "items": item}},
                            "required": ["candidates"]}
    return {"type": "object", "properties": props, "required": list(props)}


def _chunks(pages: list[dict]) -> list[list[dict]]:
    """Consecutive pages grouped so each group's text fits one model call: no page is ever left out."""
    chunks, current, size = [], [], 0
    for p in pages:
        if current and size + len(p["text"]) > MAX_DOC_CHARS:
            chunks.append(current)
            current, size = [], 0
        current.append(p)
        size += len(p["text"])
    return chunks + [current] if current else chunks


def _candidates(fields: list[dict], chunk: list[dict]) -> dict:
    """{field name: [{value, quote}]} the model found in one group of pages."""
    listing = "\n".join(f"- {f['name']}: {f['label']}" + (f" (one of: {', '.join(f['options'])})" if f.get("options")
                                                          else " (true/false)" if f["type"] == "checkbox" else "")
                        for f in fields)
    doc = "\n\n".join(f"--- page {p['page']} ({p['source']}) ---\n{p['text']}" for p in chunk)[:MAX_DOC_CHARS]
    r = llm.chat([{"role": "user", "content": f"{PROMPT}{listing}\n\nDocument:\n{doc}"}], temperature=0,
                 max_tokens=250 + 110 * len(fields),
                 response_format={"type": "json_schema", "json_schema": {"name": "fields", "schema": _schema(fields)}})
    try:
        answer = json.loads(r["choices"][0]["message"].get("content") or "{}")
    except json.JSONDecodeError:
        raise ToolError("the model's answer was not valid JSON")
    return {f["name"]: list((answer.get(f["name"]) or {}).get("candidates") or []) for f in fields}


class _Evidence:
    """Opens the source once and caches the OCR words and scan quality of each scanned page."""

    def __init__(self, pages: list[dict]):
        self.by_page = {p["page"]: p for p in pages}
        file_id = pages[0].get("file_id") if pages else None
        self.doc = open_pdf(file_id)[1] if file_id else None
        self._scans: dict[int, tuple] = {}

    def close(self):
        if self.doc:
            self.doc.close()

    def scan(self, page_no: int):
        if page_no not in self._scans:
            page = self.doc[page_no - 1]
            words = evidence.ocr_words(page)
            self._scans[page_no] = (words, evidence.scan_quality(words, page))
        return self._scans[page_no]

    def describe(self, field: dict, g: dict, quote) -> dict:
        """Add the kind of value, its format check, its box on the page and its confidence (0-100) to a grounded value."""
        if g["status"] == "not_found":
            return {**g, **rules.confidence(0), "box": None}
        kind, valid, note = evidence.value_kind(field["label"], g["value"]) if field["type"] == "text" else (field["type"], None, None)
        conf, box, reasons, ocr_conf = 100, None, [], None
        page_no, source = g.get("page"), g.get("read_by")
        if page_no and self.doc:
            page = self.doc[page_no - 1]
            if source == "text layer":
                conf = evidence.CONF_TEXT_LAYER
                if field["type"] == "text":
                    box = evidence.locate_in_text_layer(page, page_no, g["value"], quote)
            else:
                words, quality = self.scan(page_no)
                if source == "OCR":
                    box, ocr_conf = evidence.locate_in_scan(words, page, page_no, g["value"]) if field["type"] == "text" else (None, None)
                    ocr_conf = round(ocr_conf if ocr_conf is not None else quality["mean_confidence"])
                    conf = min(95, ocr_conf)
                else:
                    conf = evidence.CONF_VISION
                    reasons.append("read from the page image by the vision model")
                if quality["reason"]:
                    conf = min(conf, evidence.CONF_POOR_SCAN)
                    reasons.append(quality["reason"])
        if g["status"] == "unverified":
            conf = min(conf, evidence.CONF_UNVERIFIED)
            reasons.append(g.get("why", "not confirmed in the source"))
        if valid is False:
            conf = min(conf, evidence.CONF_FORMAT_BAD)
            reasons.append(f"{kind}: {note}")
        out = {**g, "value_type": kind, "format_valid": valid, "box": box, **rules.confidence(conf)}
        if ocr_conf is not None:
            out["ocr_confidence"] = ocr_conf
        if reasons:
            out["reason"] = "; ".join(reasons)
        return out


def extract_values(fields: list[dict], pages: list[dict], progress: list | None = None) -> dict:
    """{field name: {value, status, confidence, box, ...}} for the fields asked, reading every page.

    status: found, check (read from the page image), unverified (see reason), conflict (the document gives
    different values: all of them in `values`, best first) or not_found. progress, if given, gets one entry per
    group of pages (done or failed), so one failed group leaves the others' results in place.
    """
    cands: dict[str, list] = {f["name"]: [] for f in fields}
    failed = None
    for chunk in _chunks(pages):
        label = f"{chunk[0]['page']}-{chunk[-1]['page']}" if len(chunk) > 1 else str(chunk[0]["page"])
        try:
            found = _candidates(fields, chunk)
            for name, items in found.items():
                cands[name] += items
            if progress is not None:
                progress.append({"pages": label, "status": "done"})
        except Exception as e:            # one group failing must not lose the others (brief: partial results)
            failed = e
            if progress is not None:
                progress.append({"pages": label, "status": "failed", "error": str(e)[:160]})
    if failed and progress is not None and all(x["status"] == "failed" for x in progress):
        raise ToolError(f"the model could not read the document: {failed}")

    ev = _Evidence(pages)
    try:
        out = {}
        for f in fields:
            grounded, seen = [], set()
            for c in cands[f["name"]]:
                g = ground(f, c.get("value"), c.get("quote"), pages)
                if g["status"] == "not_found":
                    continue
                key = _norm(g["value"]) if not _numbers(g["value"]) else "".join(_numbers(g["value"]))
                if key in seen:
                    continue
                seen.add(key)
                grounded.append(ev.describe(f, g, c.get("quote")))
                if f["type"] in ("checkbox", "choice"):
                    break
            if not grounded:
                out[f["name"]] = ev.describe(f, {"value": None, "status": "not_found"}, None)
                continue
            grounded.sort(key=lambda g: -g["confidence"])
            best = dict(grounded[0])
            real = [g for g in grounded if g["status"] in ("found", "check: read from the page image")]
            if len(real) >= 2:
                best["status"] = "conflict"
                best["values"] = [{k: g[k] for k in ("value", "page", "quote", "confidence", "box") if k in g} for g in real]
                best["why"] = "also found: " + "; ".join(f"{g['value']} (page {g.get('page')})" for g in real[1:])
            out[f["name"]] = best
        return out
    finally:
        ev.close()


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


def extract_information(file_id: str, fields: list[str] | None = None, include_tables: bool = False,
                        template: str | None = None) -> dict:
    if template is not None and template not in templates.NAMES:
        raise ToolError(f"unknown template '{template}'. Templates: {', '.join(templates.NAMES)}")
    labels = list(fields or []) or (templates.fields_of(template) if template else [])
    if not labels:
        raise ToolError('give fields like ["invoice number", "invoice date", "total amount"], or a template: '
                        + ", ".join(templates.NAMES))
    pages = document_text(file_id)
    specs = _as_fields(labels)
    progress: list[dict] = []
    values = extract_values(specs, pages, progress)
    results = []
    for s in specs:
        v = values[s["name"]]
        row = {"field": s["label"], **v}
        if v["status"] == "not_found":
            row["value"] = rules.NOT_FOUND          # C3: say so, never guess
        results.append(row)

    warnings = []
    if template:
        found = sum(1 for r in results if r["status"] != "not_found")
        text = " ".join(p["text"] for p in pages)
        if (w := templates.wrong_template_warning(template, text, found, len(results))):
            warnings.append(w)
    failed = [x["pages"] for x in progress if x["status"] == "failed"]
    if failed:
        warnings.append(f"pages {', '.join(failed)} could not be read, so these results are partial")
    if any(p["source"] != "text layer" for p in pages):
        warnings.append("some pages were read by OCR or the vision model: check numbers carefully")
    result = {"file_id": file_id, "template": template, "values": results, "partial": bool(failed),
              "progress": progress, "warnings": warnings, "pages_read": len(pages),
              "read_by": sorted({p["source"] for p in pages}),
              "low_confidence_fields": [r["field"] for r in results if r.get("low_confidence") and r["status"] != "not_found"],
              "next_step": ("present each value with its confidence and page; show low-confidence, unverified and conflict "
                            "values as needing a check and say why; show 'Not found' as it is; never fill in a missing "
                            "value yourself")}
    if include_tables:
        result["tables"] = tables(file_id, pages)
    return result
