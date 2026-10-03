"""Generate the FR-PRD-104 cleanup test set. Every name, number and path is made up.

    .venv/bin/python features/fr_prd_104_cleanup/input/make_samples.py

Writes three PDFs plus ground_truth.json, which records for each page whether
cleanup should remove it and why, and which pages must survive.
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

LOREM = [
    "Operations in the northern region expanded steadily through the year. Throughput at the "
    "test warehouse rose by eleven percent while dispatch errors fell to their lowest level "
    "since the sample programme began.",
    "Staffing remained stable. Two fictional training cohorts completed the safety module, and "
    "the onboarding checklist was shortened from fourteen steps to nine without any recorded "
    "loss in quality.",
    "Procurement costs were broadly flat. The sample supplier panel was reduced from six vendors "
    "to four, and payment terms were standardised at thirty days for all remaining vendors.",
    "Customer feedback scores in the pilot survey averaged 4.2 out of 5. The most common request "
    "was for clearer delivery windows, which the planning team has scheduled for next quarter.",
]


def text_page(doc, heading, paras, footer=None):
    p = doc.new_page(width=A4[0], height=A4[1])
    p.insert_text((72, 90), heading, fontsize=16, fontname="tibo")
    p.insert_textbox(pymupdf.Rect(72, 120, 523, 760), "\n\n".join(paras), fontsize=11, fontname="tiro")
    if footer:
        p.insert_text((72, 810), footer, fontsize=9, fontname="tiro")
    return p


def save(doc, name):
    """MuPDF stamps /Info <</Producer(MuPDF ...)>> into the catalog of every document
    it creates. Drop it so each file carries only the metadata the test intends."""
    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "Info", "null")
    doc.update_object(cat, re.sub(r"/Info\s*null", "", doc.xref_object(cat, compressed=True)))
    doc.save(OUT / name, garbage=0)


# ------------------------------------------------------------ 1. digital report

def digital():
    doc = pymupdf.open()
    gt = []

    def keep(reason):
        gt.append({"page": doc.page_count, "remove": False, "reason": reason})

    text_page(doc, "ACME TEST LTD - Annual Operations Review 2026",
              ["Sample document for cleanup testing. All figures are invented.", LOREM[0]])
    keep("cover page")
    text_page(doc, "1. Regional operations", LOREM[:2])
    keep("content")
    doc.new_page(width=A4[0], height=A4[1])
    gt.append({"page": 3, "remove": True, "kind": "blank", "category": "empty"})
    text_page(doc, "2. Procurement", [LOREM[2]])
    keep("content")
    doc.fullcopy_page(1)
    gt.append({"page": 5, "remove": True, "kind": "duplicate", "duplicate_of": 2})

    p = doc.new_page(width=A4[0], height=A4[1])          # chart only, no text layer
    for i, h in enumerate([180, 260, 140, 320, 230]):
        p.draw_rect(pymupdf.Rect(110 + i * 75, 600 - h, 160 + i * 75, 600), color=None,
                    fill=(0.18, 0.36, 0.7))
    p.draw_line((100, 600), (500, 600), width=1.2)
    keep("vector chart with no text: a text-only check would wrongly call it blank")

    p = doc.new_page(width=A4[0], height=A4[1])
    p.insert_text((72, 400), "          ", fontsize=11)
    gt.append({"page": 7, "remove": True, "kind": "blank", "category": "whitespace_only_text"})

    p = doc.new_page(width=A4[0], height=A4[1])
    p.insert_text((200, 420), "This page intentionally left blank", fontsize=11, fontname="tiit")
    gt.append({"page": 8, "remove": True, "kind": "blank", "category": "intentionally_blank_notice"})

    p = doc.new_page(width=A4[0], height=A4[1])
    p.insert_text((72, 700), "Approved by: ______________________", fontsize=11, fontname="tiro")
    gt.append({"page": 9, "remove": False, "flag": "nearly_empty",
               "reason": "a single line of real content (sign-off): not blank, so ask, never auto-remove"})

    doc.fullcopy_page(3)
    gt.append({"page": 10, "remove": True, "kind": "duplicate", "duplicate_of": 4})

    text_page(doc, "1. Regional operations", [LOREM[0], LOREM[3]])
    keep("same heading and first paragraph as page 2 but a different second paragraph: not a duplicate")

    p = doc.new_page(width=A4[0], height=A4[1])
    p.insert_text((72, 40), "ACME TEST LTD - Confidential", fontsize=8, fontname="tiro")
    p.insert_text((285, 815), "Page 12", fontsize=8, fontname="tiro")
    gt.append({"page": 12, "remove": True, "kind": "blank", "category": "header_footer_only",
               "note": "borderline: propose for removal, but the user should confirm"})

    doc.set_metadata({
        "title": "Annual Operations Review 2026 (sample)",
        "author": "Test User",
        "subject": "Internal draft - not for circulation",
        "keywords": "operations, draft, internal",
        "creator": "Microsoft Word for Fakes 16.0",
        "producer": "fake-pdf-lib 3.2",
        "creationDate": "D:20260115093000+05'30'",
        "modDate": "D:20260928181500+05'30'",
    })
    info = int(doc.xref_get_key(-1, "Info")[1].split()[0])
    doc.xref_set_key(info, "Company", pymupdf.get_pdf_str("ACME TEST LTD (internal)"))
    doc.xref_set_key(info, "SourcePath",
                     pymupdf.get_pdf_str(r"C:\Users\test.user\Documents\drafts\ops_review_v7_FINAL.docx"))
    doc.set_xml_metadata(
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        '<dc:creator><rdf:Seq><rdf:li>Test User</rdf:li></rdf:Seq></dc:creator>'
        '<xmp:CreatorTool>Microsoft Word for Fakes 16.0</xmp:CreatorTool>'
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')
    save(doc, "cleanup_digital.pdf")
    return {
        "pages": doc.page_count,
        "pages_detail": gt,
        "metadata_to_remove": ["author", "subject", "keywords", "creator", "producer",
                               "creationDate", "modDate", "Company (custom)", "SourcePath (custom)",
                               "XMP metadata stream"],
        "metadata_may_keep": ["title (FR-PRD-105 accessibility checks want a title)"],
    }


# ------------------------------------------------------------ 2. scanned document

W, H = 1240, 1754           # A4 at 150 dpi


def scan(content, seed, angle=0.0, shift=(0, 0)):
    rnd = random.Random(seed)
    im = Image.new("L", (W, H), 255)
    content(ImageDraw.Draw(im))
    im = im.rotate(angle, resample=Image.BICUBIC, fillcolor=255, translate=shift)
    px = im.load()
    for _ in range(9000):                                 # paper speckle
        x, y = rnd.randrange(W), rnd.randrange(H)
        px[x, y] = max(0, px[x, y] - rnd.randrange(40, 140))
    im = Image.eval(im, lambda v: int(v * 0.94))          # grey paper
    shadow = ImageDraw.Draw(im)
    for i in range(18):                                   # scanner-lid shadow on the left edge
        shadow.line([(i, 0), (i, H)], fill=150 + i * 5)
    return im.filter(ImageFilter.GaussianBlur(0.5))


def body(heading, paras):
    def draw(d):
        f_h, f_b = ImageFont.truetype(FONT, 40), ImageFont.truetype(FONT, 27)
        d.text((150, 170), heading, font=f_h, fill=20)
        y = 270
        for para in paras:
            words, line = para.split(), ""
            for w in words:
                if d.textlength(line + w, font=f_b) > 940:
                    d.text((150, y), line, font=f_b, fill=35); y += 40; line = ""
                line += w + " "
            d.text((150, y), line, font=f_b, fill=35); y += 75
    return draw


def stamp(d):
    f = ImageFont.truetype(FONT, 30)
    d.rectangle([880, 1480, 1140, 1600], outline=60, width=4)
    d.text((905, 1500), "RECEIVED", font=f, fill=60)
    d.text((905, 1545), "12-09-2026", font=f, fill=60)


def jpeg(im):
    b = io.BytesIO(); im.save(b, "JPEG", quality=80); return b.getvalue()


def scanned():
    page_a = jpeg(scan(body("Delivery Note DN-0000-TEST", LOREM[:2]), seed=1, angle=0.4))
    page_b = jpeg(scan(body("Inspection Summary (sample)", LOREM[2:]), seed=2, angle=-0.3))
    pages = [
        (page_a, {"remove": False, "reason": "content"}),
        (jpeg(scan(lambda d: None, seed=3, angle=0.2)),
         {"remove": True, "kind": "blank", "category": "scanned_blank",
          "note": "no text layer, grey paper, speckle and edge shadow: needs a pixel check"}),
        (page_b, {"remove": False, "reason": "content"}),
        (jpeg(scan(body("Delivery Note DN-0000-TEST", LOREM[:2]), seed=4, angle=-0.5, shift=(6, -4))),
         {"remove": True, "kind": "duplicate", "duplicate_of": 1,
          "note": "re-scan of page 1: different noise, angle and offset, so exact hashes differ"}),
        (page_b, {"remove": True, "kind": "duplicate", "duplicate_of": 3,
                  "note": "identical image bytes"}),
        (jpeg(scan(stamp, seed=5, angle=1.0)),
         {"remove": False, "flag": "nearly_empty",
          "reason": "only a small RECEIVED stamp: not blank, so ask, never auto-remove"}),
    ]
    doc = pymupdf.open()
    gt = []
    for i, (img, truth) in enumerate(pages, 1):
        p = doc.new_page(width=A4[0], height=A4[1])
        p.insert_image(p.rect, stream=img)
        gt.append({"page": i, **truth})
    doc.set_metadata({
        "title": "", "author": "scan-operator-07",
        "creator": "FakeScan Pro 2.1 (model TS-4000, serial 0000-TEST)",
        "producer": "FakeScan PDF Engine 2.1",
        "creationDate": "D:20260912101500+05'30'", "modDate": "D:20260912101500+05'30'",
    })
    save(doc, "cleanup_scanned.pdf")
    return {"pages": doc.page_count, "pages_detail": gt, "text_layer": False,
            "metadata_to_remove": ["author", "creator", "producer", "creationDate", "modDate"]}


# ------------------------------------------------------------ 3. control: nothing to clean

def control():
    doc = pymupdf.open()
    text_page(doc, "Meeting notes (sample)", [LOREM[3]])
    text_page(doc, "Action items", ["1. Confirm delivery windows.\n2. Review supplier terms."])
    p = text_page(doc, "Appendix", ["Figure 1 below."])
    p.draw_rect(pymupdf.Rect(150, 300, 450, 500), color=(0.2, 0.2, 0.2), width=1)
    doc.set_metadata({"title": "Meeting notes (sample)"})
    save(doc, "cleanup_control.pdf")
    return {"pages": doc.page_count,
            "pages_detail": [{"page": i, "remove": False, "reason": "content"} for i in (1, 2, 3)],
            "metadata_to_remove": [],
            "expected": "report that nothing needs cleaning; propose no changes"}


if __name__ == "__main__":
    truth = {
        "_comment": "FR-PRD-104 cleanup test set. All content invented. Pages are 1-based.",
        "cleanup_digital.pdf": digital(),
        "cleanup_scanned.pdf": scanned(),
        "cleanup_control.pdf": control(),
    }
    (OUT / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    for name in ("cleanup_digital.pdf", "cleanup_scanned.pdf", "cleanup_control.pdf"):
        print(f"{name:<22} {(OUT / name).stat().st_size // 1024:>5} KB")
