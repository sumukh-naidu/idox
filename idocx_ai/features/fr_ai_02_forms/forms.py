"""FR-AI-02: read a fillable PDF form's fields.

Supported forms are AcroForms (PDFs with fillable fields). A flat PDF that merely looks
like a form has no fields to fill and is refused with a clear message.

Signatures, declarations and consent boxes are marked "user only": the AI never fills
them, because ticking "I declare this is true" or signing is the person's own act.
"""

import re

import pymupdf

from core.pdfutil import ToolError, open_pdf

USER_ONLY = re.compile(r"sign|declar|consent|\bagree|undertak|attest|certif|authori[sz]e", re.I)
TYPES = {pymupdf.PDF_WIDGET_TYPE_TEXT: "text", pymupdf.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
         pymupdf.PDF_WIDGET_TYPE_COMBOBOX: "choice", pymupdf.PDF_WIDGET_TYPE_LISTBOX: "choice",
         pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON: "radio", pymupdf.PDF_WIDGET_TYPE_SIGNATURE: "signature"}


def _printed_label(page, rect) -> str:
    """When a field has no tooltip, use the text printed to its left on the same line."""
    words = [w for w in page.get_text("words")
             if w[2] <= rect.x0 + 1 and min(w[3], rect.y1) - max(w[1], rect.y0) > 0.4 * (w[3] - w[1])]
    return " ".join(w[4] for w in sorted(words, key=lambda w: w[0]))


def form_fields(doc) -> list[dict]:
    out = []
    for page in doc:
        for w in page.widgets():
            label = (w.field_label or "").strip() or _printed_label(page, w.rect)
            kind = TYPES.get(w.field_type, "other")
            f = {"name": w.field_name, "label": label, "type": kind, "page": page.number + 1}
            if kind == "choice":
                f["options"] = [o if isinstance(o, str) else o[0] for o in (w.choice_values or [])]
            if kind == "signature" or USER_ONLY.search(f"{w.field_name} {label}"):
                f["fill"] = "user only"
            value = w.field_value
            if value not in ("", None, False, "Off"):
                f["current_value"] = value
            out.append(f)
    return out


def read_form_fields(file_id: str) -> dict:
    meta, doc = open_pdf(file_id)
    with doc:
        fields = form_fields(doc)
    if not fields:
        raise ToolError(f"'{meta['name']}' has no fillable fields. Only fillable PDF forms are supported; a flat PDF "
                        f"that just looks like a form cannot be filled.")
    return {"file_id": file_id, "name": meta["name"], "fields": fields,
            "user_only": [f["name"] for f in fields if f.get("fill") == "user only"]}
