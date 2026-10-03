"""Generate the FR-AI-02 test set: two fillable forms and the documents to fill them from.
Every person, company, number and amount is FAKE.

    .venv/bin/python features/fr_ai_02_forms/input/make_samples.py

Traps, recorded in ground_truth.json:
- a nominee and an agent named next to the customer; several dates; a subtotal next to the total;
  the buyer's GSTIN next to the seller's
- form fields the source does not contain (phone, email): must stay EMPTY, never guessed
- a declaration checkbox and a signature field: never filled by the AI, always left to the user
"""

import json
import re
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent
A4 = (595, 842)


def gstin(prefix14: str) -> str:
    chars, total = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0
    for i, ch in enumerate(prefix14):
        p = chars.index(ch) * (2 if i % 2 else 1)
        total += p // 36 + p % 36
    return prefix14 + chars[(36 - total % 36) % 36]


SELLER_GSTIN, BUYER_GSTIN = gstin("27ABCPE1234F1Z"), gstin("29AAACX9876K1Z")


def save(doc, name):
    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "Info", "null")
    doc.update_object(cat, re.sub(r"/Info\s*null", "", doc.xref_object(cat, compressed=True)))
    doc.save(OUT / name, garbage=3, deflate=True)


def lines(page, rows, x=60, y=80, gap=20):
    for text, size, bold in rows:
        if text:
            page.insert_text((x, y), text, fontsize=size, fontname="hebo" if bold else "helv")
        y += gap if size < 13 else gap + 8
    return y


# ------------------------------------------------------------------ fillable forms

KIND = {"text": pymupdf.PDF_WIDGET_TYPE_TEXT, "combo": pymupdf.PDF_WIDGET_TYPE_COMBOBOX,
        "check": pymupdf.PDF_WIDGET_TYPE_CHECKBOX}


def field(page, name, label, y, kind="text", options=None, height=20, width=300, x=220):
    page.insert_text((60, y + 14), label, fontsize=10, fontname="helv")
    w = pymupdf.Widget()
    w.field_name, w.field_label, w.field_type = name, label, KIND[kind]
    w.rect = pymupdf.Rect(x, y, x + (16 if kind == "check" else width), y + (16 if kind == "check" else height))
    w.text_fontsize = 10
    w.border_color, w.fill_color = (0.45, 0.45, 0.45), (0.95, 0.97, 1.0)
    if kind == "combo":
        w.choice_values = options
        w.field_value = ""
    if kind == "check":
        w.field_value = False
    if height > 24:
        w.field_flags |= pymupdf.PDF_TX_FIELD_IS_MULTILINE
    page.add_widget(w)


def application_form():
    doc = pymupdf.open()
    p = doc.new_page(width=A4[0], height=A4[1])
    lines(p, [("Health Insurance Application Form (TEST)", 15, True),
              ("Fill in from the policy schedule. Fields not on the schedule stay blank.", 9, False)])
    y = 140
    for name, label, kind, opts, h in [
        ("full_name", "Full name of policyholder", "text", None, 20),
        ("date_of_birth", "Date of birth (DD/MM/YYYY)", "text", None, 20),
        ("address", "Residential address", "text", None, 40),
        ("policy_number", "Policy number", "text", None, 20),
        ("plan_type", "Plan type", "combo", ["Individual", "Family Floater", "Senior Citizen"], 20),
        ("sum_insured", "Sum insured (INR)", "text", None, 20),
        ("nominee_name", "Nominee name", "text", None, 20),
        ("phone", "Mobile number", "text", None, 20),
        ("email", "Email address", "text", None, 20),
    ]:
        field(p, name, label, y, kind, opts, h)
        y += h + 22
    field(p, "declaration", "I declare that the above information is true", y + 10, "check", x=330)
    save(doc, "application_form.pdf")


def payment_form():
    doc = pymupdf.open()
    p = doc.new_page(width=A4[0], height=A4[1])
    lines(p, [("Vendor Payment Request Form (TEST)", 15, True),
              ("Fill in from the supplier's invoice.", 9, False)])
    y = 140
    for name, label, kind, opts in [
        ("payee_name", "Payee (supplier) name", "text", None),
        ("payee_gstin", "Payee GSTIN", "text", None),
        ("gst_registered", "Payee is GST registered", "check", None),
        ("invoice_number", "Invoice number", "text", None),
        ("invoice_date", "Invoice date", "text", None),
        ("due_date", "Payment due date", "text", None),
        ("amount_payable", "Amount payable (INR, incl. GST)", "text", None),
        ("bank_account", "Payee bank account number", "text", None),
        ("ifsc", "Payee IFSC", "text", None),
        ("payment_mode", "Payment mode", "combo", ["NEFT", "RTGS", "IMPS", "Cheque"]),
        ("approver_signature", "Approver signature", "text", None),
    ]:
        field(p, name, label, y, kind, opts)
        y += 42
    save(doc, "payment_form.pdf")


# ------------------------------------------------------------------ source documents

def policy_schedule():
    doc = pymupdf.open()
    p = doc.new_page(width=A4[0], height=A4[1])
    lines(p, [("SAMPLE HEALTH INSURANCE - Policy Schedule (TEST DATA)", 14, True),
              ("Issued on 28/10/2026 by Sample Insurance Test Co.", 9, False), ("", 10, False),
              ("Policyholder: Meera Testcase", 11, False),
              ("Date of birth: 22/03/1985", 11, False),
              ("Address: 45, Sample Nagar, 2nd Cross, Pune 411001", 11, False),
              ("Policy No: POL/2026/004512", 11, False),
              ("Plan: Family Floater", 11, False),
              ("Sum insured: Rs. 5,00,000", 11, False),
              ("Annual premium: Rs. 18,450 (incl. GST)", 11, False),
              ("Policy period: 01/11/2026 to 31/10/2027", 11, False),
              ("Nominee: Arjun Testcase (Spouse)", 11, False), ("", 10, False),
              ("Servicing agent: Kiran Agentname, agent code AG-1102", 9, False)])
    save(doc, "policy_schedule.pdf")


HEAD = ("Description", "Qty", "Rate", "Amount")
ITEMS = [("A4 paper (ream)", "20", "250.00", "5,000.00"), ("Printer toner", "4", "2,500.00", "10,000.00"),
         ("Stapler", "10", "150.00", "1,500.00")]


def invoice():
    doc = pymupdf.open()
    p = doc.new_page(width=A4[0], height=A4[1])
    y = lines(p, [("TAX INVOICE (TEST DATA)", 15, True),
                  ("ACME Test Supplies Pvt Ltd", 11, True),
                  (f"GSTIN: {SELLER_GSTIN}", 10, False),
                  ("12, Test Industrial Estate, Mumbai 400001", 10, False), ("", 10, False),
                  ("Bill to: Example Buyer Pvt Ltd", 10, False),
                  (f"Buyer GSTIN: {BUYER_GSTIN}", 10, False), ("", 10, False),
                  ("Invoice No: INV-2026-0187", 10, False),
                  ("Invoice date: 05/10/2026", 10, False),
                  ("Due date: 04/11/2026", 10, False)])
    cols, x = [60, 300, 360, 450, 535], [62, 302, 362, 452]
    rows = [HEAD, *ITEMS]
    top = y
    for i, row in enumerate(rows):
        for c, text in enumerate(row):
            p.insert_text((x[c] + 2, top + 15 + i * 22), text, fontsize=10, fontname="hebo" if i == 0 else "helv")
    bottom = top + 22 * len(rows) + 4
    for c in cols:
        p.draw_line((c, top), (c, bottom), width=0.6)
    for i in range(len(rows) + 1):
        p.draw_line((60, top + i * 22 + (4 if i == len(rows) else 0)), (535, top + i * 22 + (4 if i == len(rows) else 0)), width=0.6)
    lines(p, [("Subtotal: 16,500.00", 10, False), ("CGST 9%: 1,485.00", 10, False),
              ("SGST 9%: 1,485.00", 10, False), ("Total amount payable: Rs. 19,470.00", 11, True), ("", 10, False),
              ("Payment by NEFT to:", 10, False), ("Account No 001234567890, IFSC TEST0001234", 10, False),
              ("Thank you for your business.", 9, False)], x=300, y=bottom + 24)
    save(doc, "invoice.pdf")
    scan = pymupdf.open()
    pix = doc[0].get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
    sp = scan.new_page(width=A4[0], height=A4[1])
    sp.insert_image(sp.rect, stream=pix.tobytes("jpeg", jpg_quality=85))
    save(scan, "invoice_scanned.pdf")


if __name__ == "__main__":
    application_form(); payment_form(); policy_schedule(); invoice()
    truth = {
        "_comment": "FR-AI-02 test set. Every value is FAKE.",
        "forms": {
            "application_form.pdf": {"fields": ["full_name", "date_of_birth", "address", "policy_number", "plan_type",
                                                "sum_insured", "nominee_name", "phone", "email", "declaration"],
                                     "never_fill": ["declaration"]},
            "payment_form.pdf": {"fields": ["payee_name", "payee_gstin", "gst_registered", "invoice_number",
                                            "invoice_date", "due_date", "amount_payable", "bank_account", "ifsc",
                                            "payment_mode", "approver_signature"],
                                 "never_fill": ["approver_signature"]},
        },
        "fills": [
            {"source": "policy_schedule.pdf", "form": "application_form.pdf",
             "expected": {"full_name": "Meera Testcase", "date_of_birth": "22/03/1985",
                          "address": "45, Sample Nagar, 2nd Cross, Pune 411001", "policy_number": "POL/2026/004512",
                          "plan_type": "Family Floater", "sum_insured": "5,00,000", "nominee_name": "Arjun Testcase"},
             "must_stay_empty": ["phone", "email"], "never_fill": ["declaration"],
             "traps": {"nominee_name": "not Meera", "full_name": "not Kiran Agentname or Arjun",
                       "date_of_birth": "not 28/10/2026 or 01/11/2026"}},
            {"source": "invoice.pdf", "form": "payment_form.pdf",
             "expected": {"payee_name": "ACME Test Supplies Pvt Ltd", "payee_gstin": SELLER_GSTIN,
                          "gst_registered": True, "invoice_number": "INV-2026-0187", "invoice_date": "05/10/2026",
                          "due_date": "04/11/2026", "amount_payable": "19,470.00", "bank_account": "001234567890",
                          "ifsc": "TEST0001234", "payment_mode": "NEFT"},
             "must_stay_empty": [], "never_fill": ["approver_signature"],
             "traps": {"amount_payable": "not the subtotal 16,500.00", "payee_gstin": f"not the buyer's {BUYER_GSTIN}",
                       "payee_name": "not Example Buyer Pvt Ltd"}},
        ],
        "extraction": {"source": "invoice.pdf",
                       "table": [list(HEAD), *[list(r) for r in ITEMS]],
                       "totals": {"subtotal": "16,500.00", "cgst": "1,485.00", "sgst": "1,485.00", "total": "19,470.00"}},
        "pages": {"application_form.pdf": 1, "payment_form.pdf": 1, "policy_schedule.pdf": 1, "invoice.pdf": 1,
                  "invoice_scanned.pdf": 1},
    }
    (OUT / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    for n in ("application_form.pdf", "payment_form.pdf", "policy_schedule.pdf", "invoice.pdf", "invoice_scanned.pdf"):
        print(f"{n:<22} {(OUT / n).stat().st_size // 1024:>5} KB")
