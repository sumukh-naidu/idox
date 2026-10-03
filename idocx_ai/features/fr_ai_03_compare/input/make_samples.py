"""Generate the FR-AI-03 comparison test set: two versions of a made-up contract.

    .venv/bin/python features/fr_ai_03_compare/input/make_samples.py

Every difference between the versions is planted on purpose and recorded in
ground_truth.json, including the ones that must be IGNORED (headers, page numbers,
renumbering after a removed clause, punctuation-only edits).
"""

import io
import json
import re
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent
A4 = (595, 842)

PARTIES = ('This agreement is made between ACME Test Ltd (the "Supplier") and Example Buyer Pvt Ltd '
           '(the "Buyer"). All names in this document are invented for testing.')
TERM = "This agreement starts on 1 November 2026 and continues for 12 months unless ended earlier."
PAY_V1 = "The Buyer shall pay each undisputed invoice within 30 days of receiving it."
PAY_V2 = "The Buyer shall pay each undisputed invoice within 45 days of receiving it."
EXCL = ("During the term, the Buyer shall not purchase similar services from any other supplier "
        "without the Supplier's written consent.")
CONF = ("Each party shall keep the other party's confidential information secret and use it only "
        "to perform this agreement.")
DATA = ("The Supplier shall process personal data only on the Buyer's documented written instructions "
        "and shall report any data breach within 72 hours.")
LIAB_V1 = "The Supplier's total liability is limited to the fees paid in the previous 12 months."
LIAB_V2 = "The Supplier's total liability is limited to the fees paid in the previous 6 months."
TERM_V1 = "Either party may terminate this agreement with 60 days' written notice to the other party."
TERM_V2 = "Either party may terminate this agreement by giving 60 days' written notice to the other party."
LAW_V1 = "This agreement is governed by the laws of India."
LAW_V2 = "This agreement is governed by the laws of India"

V1 = [("Parties", PARTIES), ("Term", TERM), ("Payment", PAY_V1), ("Exclusivity", EXCL),
      ("Confidentiality", CONF), ("Liability", LIAB_V1), ("Termination", TERM_V1), ("Governing Law", LAW_V1)]
V2 = [("Parties", PARTIES), ("Term", TERM), ("Payment", PAY_V2), ("Confidentiality", CONF),
      ("Data Protection", DATA), ("Liability", LIAB_V2), ("Governing Law", LAW_V2), ("Termination", TERM_V2)]


def build(clauses, version):
    doc = pymupdf.open()
    per_page, pages = 3, (len(clauses) + 2) // 3
    for i, (title, body) in enumerate(clauses):
        if i % per_page == 0:
            p = doc.new_page(width=A4[0], height=A4[1])
            p.insert_text((72, 40), f"Service Agreement - Version {version} (sample)", fontsize=8, fontname="helv")
            p.insert_text((270, 815), f"Page {len(doc)} of {pages}", fontsize=8, fontname="helv")
            y = 90
        p.insert_text((72, y), f"{i + 1}. {title}", fontsize=12, fontname="hebo")
        p.insert_textbox(pymupdf.Rect(72, y + 10, 523, y + 110), body, fontsize=11, fontname="helv")
        y += 200
    doc.set_metadata({"title": f"Service Agreement v{version} (sample)"})
    return doc


def save(doc, name):
    cat = doc.pdf_catalog()                     # drop MuPDF's own catalog stamp
    doc.xref_set_key(cat, "Info", "null")
    doc.update_object(cat, re.sub(r"/Info\s*null", "", doc.xref_object(cat, compressed=True)))
    doc.save(OUT / name, garbage=3, deflate=True)


def scanned_copy(doc):
    """Version 2 as a scan: each page rendered to an image, no text layer."""
    out = pymupdf.open()
    for page in doc:
        pix = page.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
        p = out.new_page(width=A4[0], height=A4[1])
        p.insert_image(p.rect, stream=pix.tobytes("jpeg", jpg_quality=80))
    return out


if __name__ == "__main__":
    v1, v2 = build(V1, 1), build(V2, 2)
    save(v1, "contract_v1.pdf")
    save(v2, "contract_v2.pdf")
    save(scanned_copy(v2), "contract_v2_scanned.pdf")
    truth = {
        "_comment": "FR-AI-03 comparison test set. All content invented.",
        "contract_v1.pdf": {"pages": v1.page_count},
        "contract_v2.pdf": {"pages": v2.page_count},
        "contract_v2_scanned.pdf": {"pages": v2.page_count, "text_layer": False},
        "changes": [
            {"clause": "Payment", "type": "modified", "material": True, "old": "30", "new": "45"},
            {"clause": "Liability", "type": "modified", "material": True, "old": "12 months", "new": "6 months"},
            {"clause": "Exclusivity", "type": "removed", "material": True},
            {"clause": "Data Protection", "type": "added", "material": True},
            {"clause": "Termination", "type": "modified", "material": False,
             "note": "'with' -> 'by giving': same meaning"},
            {"clause": "Governing Law", "type": "moved", "material": False,
             "note": "moved before Termination; only its final full stop differs, which is not a wording change"},
        ],
        "unchanged": ["Parties", "Term", "Confidentiality"],
        "must_ignore": ["running header 'Version 1' vs 'Version 2'", "page numbers",
                        "clause renumbering after Exclusivity was removed",
                        "Governing Law's missing full stop"],
    }
    (OUT / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    for n in ("contract_v1.pdf", "contract_v2.pdf", "contract_v2_scanned.pdf"):
        print(f"{n:<24} {(OUT / n).stat().st_size // 1024:>5} KB")
