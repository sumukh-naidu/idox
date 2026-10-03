"""FR-AI-02 form filling: propose values for a fillable form, let the user review and edit them,
then fill a NEW copy and read every field back.

prepare_form_fill is one call (read the form, extract, check, propose): given separate steps,
the model has skipped some and reported results it never produced. Filling happens only on the
user's click (the approve endpoint); signatures and declarations are never filled at all.
"""

import pymupdf

from core import proposals, store
from core.pdfutil import ToolError, derived_name, open_pdf, origin_of, save_new

from . import extract, forms

FILLABLE = ("text", "choice", "checkbox")


def build_proposal(form_file_id: str, source_file_id: str, fields: list[dict], values: dict) -> dict:
    """values: {field name: grounding result from extract.ground}. Separate from the model call so
    it can be checked without one."""
    items, private = [], {}
    for f in fields:
        item = {"id": f["name"], "label": f["label"], "type": f["type"]}
        if f.get("options"):
            item["options"] = f["options"]
        if f.get("fill") == "user only" or f["type"] not in FILLABLE:
            item.update(status="user only", value=None)          # not in private: it cannot be approved
        else:
            v = values.get(f["name"], {"value": None, "status": "empty"})
            item.update({k: v[k] for k in ("value", "status", "page", "why", "read_by") if k in v})
            private[f["name"]] = {"type": f["type"], "options": f.get("options"), "proposed": v.get("value")}
        items.append(item)
    summary = {}
    for it in items:
        summary[it["status"]] = summary.get(it["status"], 0) + 1
    return proposals.create("form_fill", form_file_id, items, private=private,
                            summary={**summary, "source_file_id": source_file_id})


def prepare_form_fill(source_file_id: str, form_file_id: str) -> dict:
    try:
        src_meta, _ = store.get_file(source_file_id)
    except store.NotFound as e:
        raise ToolError(str(e))
    _, form_doc = open_pdf(form_file_id)
    with form_doc:
        fields = forms.form_fields(form_doc)
    if not fields:
        _, src_doc = open_pdf(source_file_id)
        with src_doc:
            swapped = bool(forms.form_fields(src_doc))
        raise ToolError("the form file has no fillable fields" +
                        ("; the files look swapped: pass the fillable form as form_file_id" if swapped else
                         "; only fillable PDF forms are supported"))
    fillable = [f for f in fields if f.get("fill") != "user only" and f["type"] in FILLABLE]
    pages = extract.document_text(source_file_id)
    values = extract.extract_values(fillable, pages)
    proposal = build_proposal(form_file_id, source_file_id, fields, values)
    items = proposal["items"]
    return {
        "proposal_id": proposal["id"], "source": src_meta["name"], "fields": items,
        "next_step": ("summarise for the user: the values found (with page), the unverified ones and why, the fields "
                      "left empty because the source does not state them, and the fields only the user may complete "
                      "(signatures, declarations). Ask them to review and edit in the card, then click Fill form. You "
                      "cannot fill the form yourself, and never suggest values for empty fields."),
        "proposal": proposal,
        **({"warning": "some source pages were read by OCR or the vision model: check values carefully"}
           if any(p["source"] != "text layer" for p in pages) else {}),
    }


def _final(spec: dict, value):
    """The value to write, from what the user approved. ValueError if it cannot go in this field."""
    if spec["type"] == "checkbox":
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in ("true", "yes", "on", "1"):
            return True
        if str(value).strip().lower() in ("false", "no", "off", "0", ""):
            return False
        raise ValueError(f"'{value}' is not a yes/no value")
    text = "" if value is None else str(value).strip()
    if spec["type"] == "choice" and text and text not in (spec["options"] or []):
        raise ValueError(f"'{text}' is not one of the options {spec['options']}")
    return text


def apply_form_fill(proposal: dict, item_ids: list[str], edits: dict | None = None) -> dict:
    edits = edits or {}
    final, problems = {}, []
    for fid in item_ids:
        spec = proposal["private"][fid]
        try:
            v = _final(spec, edits.get(fid, spec["proposed"]))
        except ValueError as e:
            problems.append(f"{fid}: {e}")
            continue
        if v not in ("", None, False):
            final[fid] = v
    if problems:
        raise ValueError("; ".join(problems))
    if not final:
        raise ValueError("every chosen field is empty; nothing to fill")

    meta, doc = open_pdf(proposal["file_id"])
    with doc:
        for page in doc:
            for w in page.widgets():
                if w.field_name in final:
                    w.field_value = final[w.field_name]
                    w.update()
        new = save_new(doc, derived_name(meta["name"], "filled"), [proposal["file_id"]], "form_fill",
                       page_origin=origin_of(meta))

    user_only = [i["id"] for i in proposal["items"] if i["status"] == "user only"]
    _, check = open_pdf(new["id"])
    wrong = []
    with check:
        for page in check:
            for w in page.widgets():
                got = w.field_value
                if w.field_name in final:
                    want = final[w.field_name]
                    ok = (got not in (False, "Off", "", None)) if want is True else str(got) == str(want)
                    if not ok:
                        wrong.append(f"{w.field_name}: holds {got!r}, approved {want!r}")
                elif w.field_name in user_only and got not in (False, "Off", "", None):
                    wrong.append(f"{w.field_name}: user-only field was filled")
    if wrong:
        raise ValueError(f"verification failed, do not use {new['name']}: " + "; ".join(wrong))
    empty = [i["id"] for i in proposal["items"] if i["status"] != "user only" and i["id"] not in final]
    return {"file": new, "filled": len(final), "left_empty": empty, "user_only": user_only,
            "verified": "re-read the new form: every filled field holds exactly the approved value, and the "
                        "signature/declaration fields are untouched",
            "note": f'{len(final)} field(s) filled into {new["id"]} "{new["name"]}" (verified); left empty: '
                    f'{", ".join(empty) or "none"}; for the user to complete: {", ".join(user_only) or "none"}.'}
