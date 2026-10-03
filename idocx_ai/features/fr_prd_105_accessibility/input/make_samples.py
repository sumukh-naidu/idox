"""Generate the FR-PRD-105 accessibility test set. Every name and figure is made up.

    .venv/bin/python features/fr_prd_105_accessibility/input/make_samples.py

Writes three PDFs plus ground_truth.json (expected status of every check, before
and after fix_accessibility, and words the image descriptions should contain).
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
SERIF = "/System/Library/Fonts/Supplemental/Times New Roman.ttf"
SANS = "/System/Library/Fonts/Supplemental/Arial.ttf"


def save(doc, name):
    cat = doc.pdf_catalog()                     # drop MuPDF's own catalog stamp
    doc.xref_set_key(cat, "Info", "null")
    doc.update_object(cat, re.sub(r"/Info\s*null", "", doc.xref_object(cat, compressed=True)))
    doc.save(OUT / name, garbage=3, deflate=True)


def png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def bar_chart():
    im = Image.new("RGB", (900, 560), "white")
    d = ImageDraw.Draw(im)
    f, ft = ImageFont.truetype(SANS, 26), ImageFont.truetype(SANS, 32)
    d.text((240, 20), "Orders shipped per quarter (sample)", font=ft, fill="black")
    for i, (q, v) in enumerate([("Q1", 120), ("Q2", 180), ("Q3", 150), ("Q4", 240)]):
        x = 120 + i * 180
        d.rectangle([x, 480 - v * 1.5, x + 100, 480], fill=(47, 109, 246))
        d.text((x + 30, 490), q, font=f, fill="black")
        d.text((x + 25, 440 - v * 1.5), str(v), font=f, fill="black")
    d.line([(90, 480), (850, 480)], fill="black", width=3)
    return png(im)


def flowchart():
    im = Image.new("RGB", (900, 300), "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(SANS, 34)
    for i, label in enumerate(["Order", "Pack", "Ship"]):
        x = 40 + i * 300
        d.rounded_rectangle([x, 100, x + 220, 200], radius=18, outline="black", width=4, fill=(232, 240, 255))
        d.text((x + 110 - d.textlength(label, font=f) / 2, 132), label, font=f, fill="black")
        if i < 2:
            d.line([(x + 225, 150), (x + 295, 150)], fill="black", width=4)
            d.polygon([(x + 295, 150), (x + 280, 140), (x + 280, 160)], fill="black")
    return png(im)


BODY = ("Operations in the northern region expanded steadily through the year. Throughput at the "
        "test warehouse rose by eleven percent while dispatch errors fell. All figures in this "
        "document are invented for testing.")


# ------------------------------------------------------------ 1. a typical untagged report

def report():
    doc = pymupdf.open()
    p = doc.new_page(width=A4[0], height=A4[1])
    p.insert_text((72, 90), "ACME Test Ltd - Quarterly Operations Summary", fontsize=18, fontname="hebo")
    p.insert_textbox(pymupdf.Rect(72, 115, 523, 250), BODY, fontsize=11, fontname="helv")
    p.insert_image(pymupdf.Rect(72, 270, 523, 550), stream=bar_chart())
    p = doc.new_page(width=A4[0], height=A4[1])
    p.insert_text((72, 90), "How an order moves", fontsize=14, fontname="hebo")
    p.insert_textbox(pymupdf.Rect(72, 110, 523, 200),
                     "Every order passes through three stages before it leaves the test warehouse.",
                     fontsize=11, fontname="helv")
    p.insert_image(pymupdf.Rect(72, 210, 523, 360), stream=flowchart())
    save(doc, "access_report.pdf")
    return {
        "pages": 2,
        "before": {"tagged": "fail", "language": "fail", "title": "fail", "fonts_embedded": "fail",
                   "image_descriptions": "fail", "real_text": "pass", "reading_order": "fail",
                   "bookmarks": "n/a", "assistive_access": "pass"},
        "after_fix": {"language": "pass", "title": "pass",
                      "tagged": "fail", "fonts_embedded": "fail", "reading_order": "fail"},
        "expected_title": "ACME Test Ltd - Quarterly Operations Summary",
        "expected_language": "en",
        "image_keywords": [{"page": 1, "all_of": ["Q1", "Q4"], "any_of": ["bar", "chart"]},
                           {"page": 2, "all_of": ["Order", "Pack", "Ship"], "any_of": []}],
    }


# ------------------------------------------------------------ 2. a scanned report

def scan_page(lines, seed):
    w, h = 1240, 1754
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    for i, (text, size) in enumerate(lines):
        d.text((150, 170 + i * 70), text, font=ImageFont.truetype(SERIF, size), fill=25)
    im = im.rotate(0.3, resample=Image.BICUBIC, fillcolor=255)
    rnd, px = random.Random(seed), im.load()
    for _ in range(6000):
        x, y = rnd.randrange(w), rnd.randrange(h)
        px[x, y] = max(0, px[x, y] - rnd.randrange(40, 120))
    im = Image.eval(im, lambda v: int(v * 0.94)).filter(ImageFilter.GaussianBlur(0.5))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=80)
    return b.getvalue()


def scanned():
    doc = pymupdf.open()
    pages = [
        [("Site Inspection Report (Sample)", 46), ("", 30),
         ("The inspection of the test warehouse took place on schedule.", 30),
         ("All fire exits were clear and the alarms were working.", 30),
         ("Two shelving units need new safety labels.", 30)],
        [("Recommendations", 40), ("", 30),
         ("Replace the safety labels on both shelving units.", 30),
         ("Repeat the inspection within the next quarter.", 30)],
    ]
    for i, lines in enumerate(pages):
        p = doc.new_page(width=A4[0], height=A4[1])
        p.insert_image(p.rect, stream=scan_page(lines, seed=20 + i))
    save(doc, "access_scanned.pdf")
    return {
        "pages": 2,
        "before": {"tagged": "fail", "language": "fail", "title": "fail", "fonts_embedded": "n/a",
                   "image_descriptions": "n/a", "real_text": "fail", "reading_order": "fail",
                   "bookmarks": "n/a", "assistive_access": "pass"},
        "after_fix": {"real_text": "pass", "language": "pass", "title": "pass", "fonts_embedded": "pass",
                      "tagged": "fail", "reading_order": "fail"},
        "expected_title": "Site Inspection Report (Sample)",
        "expected_language": "en",
        "text_after_fix": ["fire exits were clear", "Replace the safety labels"],
    }


# ------------------------------------------------------------ 3. a control that should pass

def tagged_control():
    doc = pymupdf.open()
    p = doc.new_page(width=A4[0], height=A4[1])
    p.insert_font(fontname="body", fontfile=SANS)
    p.insert_text((72, 90), "Accessible sample report", fontsize=18, fontname="body")
    p.insert_textbox(pymupdf.Rect(72, 115, 523, 250), BODY, fontsize=11, fontname="body")
    p.insert_image(pymupdf.Rect(72, 270, 523, 550), stream=bar_chart())

    # A minimal hand-made structure tree: Document -> P, Figure (with alt text). Enough
    # for the structural checks; it is NOT a fully tagged PDF (no marked content).
    root, top, para, fig = (doc.get_new_xref() for _ in range(4))
    doc.update_object(fig, f"<</Type/StructElem/S/Figure/P {top} 0 R"
                           f"/Alt{pymupdf.get_pdf_str('Bar chart of orders shipped per quarter, Q1 to Q4 (sample data)')}>>")
    doc.update_object(para, f"<</Type/StructElem/S/P/P {top} 0 R>>")
    doc.update_object(top, f"<</Type/StructElem/S/Document/P {root} 0 R/K[{para} 0 R {fig} 0 R]>>")
    doc.update_object(root, f"<</Type/StructTreeRoot/K {top} 0 R>>")
    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "StructTreeRoot", f"{root} 0 R")
    doc.xref_set_key(cat, "MarkInfo", "<</Marked true>>")
    doc.xref_set_key(cat, "Lang", pymupdf.get_pdf_str("en"))
    doc.xref_set_key(cat, "ViewerPreferences", "<</DisplayDocTitle true>>")
    doc.set_metadata({"title": "Accessible sample report"})
    doc.subset_fonts()
    save(doc, "access_tagged.pdf")
    return {
        "pages": 1,
        "before": {"tagged": "pass", "language": "pass", "title": "pass", "fonts_embedded": "pass",
                   "image_descriptions": "pass", "real_text": "pass", "reading_order": "pass",
                   "bookmarks": "n/a", "assistive_access": "pass"},
        "note": "tags are a minimal hand-made structure, enough for the structural checks only",
    }


if __name__ == "__main__":
    truth = {"_comment": "FR-PRD-105 accessibility test set. All content invented. Pages are 1-based.",
             "access_report.pdf": report(), "access_scanned.pdf": scanned(),
             "access_tagged.pdf": tagged_control()}
    (OUT / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    for name in ("access_report.pdf", "access_scanned.pdf", "access_tagged.pdf"):
        print(f"{name:<20} {(OUT / name).stat().st_size // 1024:>5} KB")
