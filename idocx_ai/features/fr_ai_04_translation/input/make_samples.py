"""Generate the FR-AI-04 translation test set: a made-up two-page agreement. All content is FAKE.

    .venv/bin/python features/fr_ai_04_translation/input/make_samples.py

It has what layout preservation must survive: a bold title, numbered clause headings,
paragraphs, a ruled fee table with number-only cells, a logo image, footers, and values
that must come through unchanged (amounts, dates, an email, an ID, company names).
"""

import io
import json
import re
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent
A4 = (595, 842)
SANS = "/System/Library/Fonts/Supplemental/Arial.ttf"

CLAUSES_P1 = [
    ("1. Parties", 'This services agreement is made between ACME Test Ltd (the "Supplier") and Example Buyer Pvt Ltd '
                   '(the "Buyer"). All names in this document are invented for testing.'),
    ("2. Services", "The Supplier will provide document processing services as described in Schedule A. The services "
                    "start on 1 November 2026 and continue for 12 months."),
    ("3. Fees", "The Buyer shall pay the fees below. All amounts are in Indian rupees and exclude GST at 18%."),
]
FEES = [("Item", "Amount (INR)"), ("Setup and configuration", "25,000.00"), ("Monthly service fee", "15,000.00"),
        ("Training (2 days)", "8,000.00")]
CLAUSES_P2 = [
    ("4. Payment", "The Buyer shall pay each undisputed invoice within 45 days of receiving it. Late payments carry "
                   "interest of 1.5% per month on the amount outstanding."),
    ("5. Confidentiality", "Each party shall keep the other party's confidential information secret and use it only "
                           "to perform this agreement. This obligation continues for 3 years after the agreement ends."),
    ("6. Termination", "Either party may terminate this agreement by giving 60 days' written notice to the other party "
                       "at legal@example.com. The Supplier's GSTIN is 27ABCPE1234F1ZB."),
]
MUST_SURVIVE = ["1 November 2026", "12", "18%", "25,000.00", "15,000.00", "8,000.00", "45", "1.5%", "3", "60",
                "legal@example.com", "27ABCPE1234F1ZB", "ACME Test Ltd", "Example Buyer Pvt Ltd"]


def logo() -> bytes:
    im = Image.new("RGB", (360, 120), (47, 109, 246))
    d = ImageDraw.Draw(im)
    d.text((28, 30), "ACME", font=ImageFont.truetype(SANS, 56), fill="white")
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def clause(p, y, heading, body):
    p.insert_text((72, y), heading, fontsize=12, fontname="hebo")
    p.insert_textbox(pymupdf.Rect(72, y + 8, 523, y + 90), body, fontsize=10.5, fontname="helv")


def build():
    doc = pymupdf.open()
    p = doc.new_page(width=A4[0], height=A4[1])
    p.insert_image(pymupdf.Rect(433, 40, 523, 70), stream=logo())
    p.insert_text((72, 70), "Services Agreement", fontsize=18, fontname="hebo")
    p.insert_text((72, 90), "Sample document for translation testing", fontsize=10, fontname="helv")
    y = 130
    for h, b in CLAUSES_P1:
        clause(p, y, h, b)
        y += 95
    top, cols = y, [72, 360, 523]
    for i, (item, amount) in enumerate(FEES):
        font = "hebo" if i == 0 else "helv"
        p.insert_text((78, top + 16 + i * 24), item, fontsize=10.5, fontname=font)
        p.insert_text((366, top + 16 + i * 24), amount, fontsize=10.5, fontname=font)
    bottom = top + 24 * len(FEES) + 4
    for c in cols:
        p.draw_line((c, top), (c, bottom), width=0.7)
    for i in range(len(FEES) + 1):
        yy = top + i * 24 + (4 if i == len(FEES) else 0)
        p.draw_line((72, yy), (523, yy), width=0.7)
    p.insert_text((270, 810), "Page 1 of 2", fontsize=8, fontname="helv")

    p = doc.new_page(width=A4[0], height=A4[1])
    y = 70
    for h, b in CLAUSES_P2:
        clause(p, y, h, b)
        y += 100
    p.insert_text((72, y + 20), "Signed for ACME Test Ltd", fontsize=10.5, fontname="helv")
    p.insert_text((72, y + 60), "Authorised signatory", fontsize=10.5, fontname="helv")
    p.insert_text((270, 810), "Page 2 of 2", fontsize=8, fontname="helv")

    doc.set_metadata({"title": "Services Agreement (sample)"})
    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "Info", "null")
    doc.update_object(cat, re.sub(r"/Info\s*null", "", doc.xref_object(cat, compressed=True)))
    doc.save(OUT / "agreement.pdf", garbage=3, deflate=True)
    return {"pages": 2, "must_survive": MUST_SURVIVE, "images": 1, "table_rows": len(FEES),
            "number_only_cells": [a for _, a in FEES[1:]]}


def scanned_copy():
    """Page 1 of the agreement as a scan: an image with no text layer."""
    src = pymupdf.open(OUT / "agreement.pdf")
    out = pymupdf.open()
    pix = src[0].get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
    page = out.new_page(width=A4[0], height=A4[1])
    page.insert_image(page.rect, stream=pix.tobytes("jpeg", jpg_quality=85))
    out.save(OUT / "agreement_scanned.pdf", garbage=3, deflate=True)
    return {"pages": 1, "text_layer": False, "number_only_cells": [a for _, a in FEES[1:]]}


if __name__ == "__main__":
    truth = {"_comment": "FR-AI-04 translation test set. All content invented.", "agreement.pdf": build(),
             "agreement_scanned.pdf": scanned_copy()}
    (OUT / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    for n in ("agreement.pdf", "agreement_scanned.pdf"):
        print(f"{n:<24} {(OUT / n).stat().st_size // 1024:>4} KB")
