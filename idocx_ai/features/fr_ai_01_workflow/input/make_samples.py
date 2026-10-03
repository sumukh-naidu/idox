"""Generate the FR-AI-01 workflow test set. Every name, date and figure is made up.

    .venv/bin/python features/fr_ai_01_workflow/input/make_samples.py

Writes five PDFs plus ground_truth.json.
"""

import io
import json
import random
import re
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent
A4 = (595, 842)
FONT = "/System/Library/Fonts/Supplemental/Times New Roman.ttf"


def save(doc, name, title):
    doc.set_metadata({"title": title})
    cat = doc.pdf_catalog()                     # drop MuPDF's own catalog stamp
    doc.xref_set_key(cat, "Info", "null")
    doc.update_object(cat, re.sub(r"/Info\s*null", "", doc.xref_object(cat, compressed=True)))
    doc.save(OUT / name, garbage=3, deflate=True)


def text_page(doc, heading, body, marker):
    """marker: a unique line per page, so checks can tell exactly which page landed where."""
    p = doc.new_page(width=A4[0], height=A4[1])
    p.insert_text((72, 90), heading, fontsize=16, fontname="tibo")
    p.insert_textbox(pymupdf.Rect(72, 120, 523, 740), body, fontsize=11, fontname="tiro")
    p.insert_text((72, 800), marker, fontsize=8, fontname="tiro")
    return p


# ------------------------------------------------------------ merge pair (example 1)

def merge_pair():
    a = pymupdf.open()
    for i in range(1, 4):
        text_page(a, f"Report A - Section {i}",
                  "Sample report text for merge testing. Figures are invented and carry no meaning. "
                  "Warehouse throughput, dispatch accuracy and staffing are summarised here.",
                  f"[marker A{i}]")
    save(a, "merge_a.pdf", "Report A (sample)")

    b = pymupdf.open()
    # A large, smooth "photo" at 300 dpi: what compression is for.
    w, h = 2480, 3508
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            c = (int(90 + 80 * x / w), int(120 + 60 * y / h), int(160 - 50 * x / w))
            for dy in range(4):
                for dx in range(4):
                    px[x + dx, y + dy] = c
    d = ImageDraw.Draw(im)
    rnd = random.Random(7)
    for _ in range(60):
        x, y, r = rnd.randrange(w), rnd.randrange(h), rnd.randrange(40, 260)
        d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(rnd.randrange(60, 230) for _ in range(3)))
    im = im.filter(ImageFilter.GaussianBlur(3))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    p = b.new_page(width=A4[0], height=A4[1])
    p.insert_image(p.rect, stream=buf.getvalue())
    p.insert_text((72, 800), "[marker B1]", fontsize=8, fontname="tiro")
    b.new_page(width=A4[0], height=A4[1])                      # blank
    text_page(b, "Appendix B", "Supporting notes for report B (sample data).", "[marker B3]")
    save(b, "merge_b.pdf", "Report B (sample)")
    return {"merge_a.pdf": {"pages": 3, "markers": ["A1", "A2", "A3"]},
            "merge_b.pdf": {"pages": 3, "markers": ["B1", None, "B3"], "blank_pages": [2],
                            "note": "page 1 is a 300 dpi image: the compression target"}}


# ------------------------------------------------------------ contract (example 2)

SECTIONS = [("Section 1 - Definitions", 3), ("Section 2 - Payment Terms", 3),
            ("Schedule A - Deliverables", 3), ("Schedule B - Contacts", 3)]


def contract():
    doc, n = pymupdf.open(), 0
    for title, count in SECTIONS:
        for k in range(1, count + 1):
            n += 1
            text_page(doc, f"{title} ({k} of {count})",
                      "Sample agreement between ACME Test Ltd and Example Buyer Pvt Ltd. "
                      "This text is invented for testing and has no legal meaning.",
                      f"[marker C{n}]")
    save(doc, "contract_12p.pdf", "Service Agreement (sample)")
    return {"pages": 12, "sections": [{"title": t, "pages": f"{1 + 3 * i}-{3 + 3 * i}"}
                                       for i, (t, _) in enumerate(SECTIONS)]}


# ------------------------------------------------------------ tasks and deadlines (digital)

MEMO = [
    ("PROJECT ORION (TEST) - Kick-off memo",
     "ACME Test Ltd was founded on 1 March 2019 and moved to its current office in 2022.\n\n"
     "Action items:\n"
     "1. Vendor onboarding must be completed by 15 October 2026.\n"
     "2. Submit the revised budget to the finance team by 20/10/2026.\n"
     "3. The test team will schedule the safety audit before 31 Oct 2026."),
    ("Kick-off memo - continued",
     "The previous audit took place on 12 June 2025 and raised no major findings.\n\n"
     "4. Please send the signed NDA to the legal team no later than 5 November 2026.\n\n"
     "The next quarterly review meeting is planned for 1 December 2026."),
]


def memo():
    doc = pymupdf.open()
    for i, (h, body) in enumerate(MEMO, 1):
        text_page(doc, h, body, f"[marker M{i}]")
    save(doc, "project_memo.pdf", "Project Orion kick-off memo (sample)")
    return {"pages": 2,
            "deadlines": ["15 October 2026", "20/10/2026", "31 Oct 2026", "5 November 2026"],
            "not_deadlines": ["1 March 2019", "12 June 2025"],
            "either": ["1 December 2026"],
            "note": "1 Dec 2026 is a scheduled meeting, not an action item: listing it is acceptable"}


# ------------------------------------------------------------ tasks and deadlines (scanned)

def scanned_notice():
    w, h = 1240, 1754
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    f_h, f_b = ImageFont.truetype(FONT, 46), ImageFont.truetype(FONT, 30)
    d.text((150, 170), "NOTICE (TEST) - Building maintenance", font=f_h, fill=20)
    lines = ["This notice was issued on 2 October 2026.", "",
             "All teams must clear the storage room by 18 October 2026.", "",
             "Fire drill participation is mandatory. Submit the attendance",
             "sheets to the facilities desk by 25 October 2026.", "",
             "Questions: facilities desk, extension 0000 (test)."]
    for i, line in enumerate(lines):
        d.text((150, 300 + i * 52), line, font=f_b, fill=30)
    im = im.rotate(0.4, resample=Image.BICUBIC, fillcolor=255)
    rnd, px = random.Random(11), im.load()
    for _ in range(7000):
        x, y = rnd.randrange(w), rnd.randrange(h)
        px[x, y] = max(0, px[x, y] - rnd.randrange(40, 120))
    im = Image.eval(im, lambda v: int(v * 0.94)).filter(ImageFilter.GaussianBlur(0.5))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    doc = pymupdf.open()
    p = doc.new_page(width=A4[0], height=A4[1])
    p.insert_image(p.rect, stream=buf.getvalue())
    save(doc, "scanned_notice.pdf", "")
    return {"pages": 1, "text_layer": False,
            "deadlines": ["18 October 2026", "25 October 2026"], "not_deadlines": ["2 October 2026"]}


if __name__ == "__main__":
    truth = {"_comment": "FR-AI-01 workflow test set. All content invented. Pages are 1-based."}
    truth.update(merge_pair())
    truth["contract_12p.pdf"] = contract()
    truth["project_memo.pdf"] = memo()
    truth["scanned_notice.pdf"] = scanned_notice()
    (OUT / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    for name in ("merge_a.pdf", "merge_b.pdf", "contract_12p.pdf", "project_memo.pdf", "scanned_notice.pdf"):
        print(f"{name:<20} {(OUT / name).stat().st_size // 1024:>5} KB")
