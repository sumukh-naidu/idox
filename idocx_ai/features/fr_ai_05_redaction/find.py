"""FR-AI-05: find sensitive information and turn it into a proposal plus a highlighted preview.

Nothing is redacted here. The proposal waits for the user's approval, which comes through
the API, never from the model.
"""

import pymupdf

from core import proposals, store
from core.pdfutil import ToolError, derived_name, open_pdf, origin_of, save_new

from .scan import scan_document

COLORS = {"secret": (1.0, 0.35, 0.35), "government_id": (1.0, 0.62, 0.2), "financial": (0.72, 0.5, 1.0),
          "contact": (0.35, 0.65, 1.0), "personal": (0.4, 0.82, 0.45)}


def build_preview(file_id: str, items: list[dict]) -> dict:
    meta, src = open_pdf(file_id)
    doc = pymupdf.open()
    with src:
        doc.insert_pdf(src)
    for it in items:
        page = doc[it["page"] - 1]         # keep the page alive while its annotation is edited
        for rect in it["rects"]:
            a = page.add_highlight_annot(pymupdf.Rect(rect))
            a.set_colors(stroke=COLORS[it["category"]])
            a.set_info(title="iDocx redaction preview", content=f"{it['id']} {it['kind']}: {it['masked']}")
            a.update()
    return save_new(doc, derived_name(meta["name"], "redaction-preview"), [file_id], "find_sensitive_info",
                    page_origin=origin_of(meta))


def find_sensitive_info(file_id: str, use_model: bool = True) -> dict:
    try:
        meta, _ = store.get_file(file_id)
    except store.NotFound as e:
        raise ToolError(str(e))
    try:
        items, ocr, dropped = scan_document(file_id, use_model=use_model)
    except Exception as e:
        if use_model and "urlopen" in repr(e).lower():
            raise ToolError(f"the model could not be reached to look for names and addresses: {e}")
        raise
    if not items:
        return {"file_id": file_id, "found": 0, "note": "no sensitive information found; nothing to redact"}

    preview = build_preview(file_id, [i for i in items if i["located"]])
    public_items = [{k: i[k] for k in ("id", "kind", "category", "masked", "page", "basis", "confidence",
                                       "read_by", "located")} for i in items]
    by_category = {}
    for i in items:
        by_category[i["category"]] = by_category.get(i["category"], 0) + 1
    proposal = proposals.create("redaction", file_id, public_items,
                                private={i["id"]: {"value": i["value"], "rects": i["rects"], "page": i["page"]}
                                         for i in items},
                                preview=preview, summary=by_category)
    result = {
        "file_id": file_id, "proposal_id": proposal["id"], "found": len(items), "by_category": by_category,
        "items": public_items,
        "next_step": ("tell the user what was found, grouped by category, using only the masked values; point them "
                      "to the highlighted preview file; and ask them to review and approve in the review card. "
                      "You cannot apply a redaction yourself: only the user's approval does that."),
        "files_created": [preview], "proposal": proposal,
    }
    warnings = []
    if ocr:
        warnings.append(f"pages {ocr} were read by OCR, which can misread digits: review those items carefully")
    if dropped:
        warnings.append(f"{dropped} name/address suggestion(s) from the model were discarded: not found in the text")
    unlocated = [i["id"] for i in items if not i["located"]]
    if unlocated:
        warnings.append(f"{', '.join(unlocated)} could not be located on the page and cannot be redacted automatically")
    if warnings:
        result["warnings"] = warnings
    return result
