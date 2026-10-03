"""FR-AI-05: apply an approved redaction proposal, then prove the result.

Reached only through the approve endpoint, i.e. the user's click; no tool calls this.
The redaction REMOVES the text under each box (and blacks out the image pixels on
scanned pages); it does not just draw a black rectangle over readable text. Then the
new file is checked: on digital pages each redacted value must be gone from the text,
on scanned pages each box must be black.
"""

import re

import pymupdf

from core.pdfutil import derived_name, open_pdf, origin_of, save_new

DARK = 60   # mean grey (0-255) a redacted area on a scan must be under


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def apply_redaction(proposal: dict, item_ids: list[str], edits: dict | None = None) -> dict:
    items = {i["id"]: i for i in proposal["items"]}
    chosen = {i: proposal["private"][i] for i in item_ids}
    meta, doc = open_pdf(proposal["file_id"])
    with doc:
        touched = {}
        for d in chosen.values():
            page = touched.setdefault(d["page"], doc[d["page"] - 1])
            for r in d["rects"]:
                page.add_redact_annot(pymupdf.Rect(r), fill=(0, 0, 0))
        for page in touched.values():
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,   # keep table lines
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        new = save_new(doc, derived_name(meta["name"], "redacted"), [proposal["file_id"]], "redaction",
                       page_origin=origin_of(meta))

    _, check = open_pdf(new["id"])
    still = []
    with check:
        for iid, d in chosen.items():
            page = check[d["page"] - 1]
            if items[iid]["read_by"] == "OCR":
                for r in d["rects"]:
                    pix = page.get_pixmap(clip=pymupdf.Rect(r), dpi=72, colorspace=pymupdf.csGRAY)
                    if pix.samples and sum(pix.samples) / len(pix.samples) > DARK:
                        still.append(iid)
                        break
            elif _squash(d["value"]) in _squash(page.get_text()):
                still.append(iid)
    if still:
        raise ValueError(f"verification failed: {', '.join(still)} still readable in {new['name']}; do not use it")
    return {"file": new, "redacted": len(chosen), "left_unredacted": len(items) - len(chosen),
            "verified": "re-read the new file: every redacted value is gone from the text, and every box on a "
                        "scanned page is black",
            "note": f'{len(chosen)} item(s) redacted into {new["id"]} "{new["name"]}" (verified); '
                    f'{len(items) - len(chosen)} left unredacted by the user\'s choice.'}
