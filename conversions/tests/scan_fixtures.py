"""Made-up scanned test pages for the tests (copy of test_data/scanned_baseline/make_scans.py, so the repo tests need nothing outside it).

Originally made to measure the lost-table fix (KNOWN_ISSUES.md #1).

Each PDF is a single A4 page holding ONE grayscale JPEG and no text layer, the same shape as
01 PDF to Word/agreement_scanned.pdf (200 dpi). expected.json lists what a correct conversion must
contain, written from the same data that is drawn, so the two cannot drift apart.

Run:  python make_scans.py      (needs pymupdf and Pillow; no model, no network)
All names, numbers and addresses are invented.
"""
import io
import json
import pathlib

import pymupdf
from PIL import Image

OUT = pathlib.Path(__file__).parent
W, H, M = 595, 842, 56          # A4 in points, page margin
DPI, JPEG_QUALITY = 200, 85


class Sheet:
    """A vector page that is drawn top to bottom, then flattened into a scan."""

    def __init__(self):
        self.doc = pymupdf.open()
        self.page = self.doc.new_page(width=W, height=H)
        self.y = M

    def line(self, text, size=10, bold=False, gap=6):
        self.page.insert_text((M, self.y + size), text, fontname="hebo" if bold else "helv", fontsize=size)
        self.y += size * 1.25 + gap

    def paragraph(self, text, size=10, gap=14):
        words, row = text.split(), ""
        for w in words:
            trial = f"{row} {w}".strip()
            if pymupdf.get_text_length(trial, fontname="helv", fontsize=size) > W - 2 * M and row:
                self.line(row, size, gap=2)
                row = w
            else:
                row = trial
        if row:
            self.line(row, size, gap=2)
        self.y += gap

    def skip(self, points):
        self.y += points

    def table(self, widths, header, rows, row_h=24, gap=18):
        top, x0 = self.y, M
        for r, cells in enumerate([header] + rows):
            x = x0
            for c, text in enumerate(cells):
                self.page.insert_text((x + 6, top + r * row_h + 16), text,
                                      fontname="hebo" if r == 0 else "helv", fontsize=10)
                x += widths[c]
        x = x0
        for i in range(len(widths) + 1):
            self.page.draw_line((x, top), (x, top + row_h * (len(rows) + 1)), width=0.8)
            x += widths[i] if i < len(widths) else 0
        for r in range(len(rows) + 2):
            self.page.draw_line((x0, top + r * row_h), (x0 + sum(widths), top + r * row_h), width=0.8)
        self.y = top + row_h * (len(rows) + 1) + gap

    def footer(self, text):
        n = pymupdf.get_text_length(text, fontname="helv", fontsize=8)
        self.page.insert_text(((W - n) / 2, H - 48), text, fontname="helv", fontsize=8)

    def save_as_scan(self, path):
        pix = self.page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=JPEG_QUALITY)
        out = pymupdf.open()
        page = out.new_page(width=W, height=H)
        page.insert_image(page.rect, stream=buf.getvalue())
        out.save(path)


# --------------------------------------------------------------------------- the five pages
def table_high(path):
    s = Sheet()
    s.line("Quotation QT-2026-031", 20, bold=True, gap=8)
    s.line("Prepared for Example Buyer Pvt Ltd. All names in this document are invented.", gap=22)
    t = (["Item", "Quantity", "Price (INR)"],
         [["Paper A4", "10", "450.00"], ["Toner cartridge", "2", "3,200.00"],
          ["Stapler", "1", "275.00"], ["Folder set", "5", "600.00"]])
    s.table([250, 110, 110], *t)
    texts = [("1. Validity", "This quotation is valid for 30 days from the date above."),
             ("2. Delivery", "Goods are delivered within 7 working days of the order."),
             ("3. Payment", "Payment is due within 15 days of delivery.")]
    for head, body in texts:
        s.line(head, 12, bold=True)
        s.paragraph(body)
    s.footer("Page 1 of 1")
    s.save_as_scan(path)
    return dict(describe="table right under the title, three clauses below it",
                lines=["Quotation QT-2026-031", "Prepared for Example Buyer Pvt Ltd. All names in this document are invented."] + [x for h, b in texts for x in (h, b)] + ["Page 1 of 1"],
                tables=[dict(header=t[0], rows=t[1])])


def table_low(path):
    s = Sheet()
    s.line("Services Order SO-77", 20, bold=True, gap=8)
    s.line("Sample order for translation testing", gap=22)
    texts = [("1. Parties", 'This order is made between Sample Supplier Ltd (the "Supplier") and Example Buyer '
                            'Pvt Ltd (the "Buyer"). All names are invented for testing.'),
             ("2. Scope", "The Supplier will scan and index the Buyer's archive as described in Schedule B. "
                          "Work starts on 2 December 2026 and runs for 6 months."),
             ("3. Charges", "The Buyer shall pay the charges below. All amounts are in Indian rupees and "
                            "exclude GST at 18%.")]
    for head, body in texts:
        s.line(head, 12, bold=True)
        s.paragraph(body, gap=22)
    s.skip(90)
    t = (["Item", "Amount (INR)"],
         [["Setup and indexing", "40,000.00"], ["Monthly scanning", "22,500.00"],
          ["Quality review", "9,500.00"], ["Project management", "12,000.00"]])
    s.table([330, 160], *t)
    s.footer("Page 1 of 2")
    s.save_as_scan(path)
    return dict(describe="same shape as agreement_scanned.pdf: clauses first, table low on the page, footer",
                lines=["Services Order SO-77", "Sample order for translation testing"]
                      + [x for h, b in texts for x in (h, b)] + ["Page 1 of 2"],
                tables=[dict(header=t[0], rows=t[1])])


def two_tables(path):
    s = Sheet()
    s.line("Project Plan PP-12", 20, bold=True, gap=8)
    s.line("Draft schedule and team. All names are invented.", gap=22)
    s.line("1. Schedule", 12, bold=True)
    s.paragraph("The project runs in three phases as shown below.")
    t1 = (["Phase", "Start", "End"],
          [["Discovery", "01-12-2026", "15-12-2026"], ["Build", "16-12-2026", "31-01-2027"],
           ["Handover", "01-02-2027", "15-02-2027"]])
    s.table([220, 120, 120], *t1)
    s.line("2. Team", 12, bold=True)
    s.paragraph("The following people are assigned to the project.")
    t2 = (["Role", "Name"],
          [["Project lead", "Asha Verma"], ["Engineer", "Ravi Menon"], ["Reviewer", "Meera Rao"]])
    s.table([250, 210], *t2)
    s.paragraph("Dates may change by agreement of both teams.")
    s.footer("Page 1 of 1")
    s.save_as_scan(path)
    return dict(describe="two tables (3 columns and 2 columns) with text between them and after",
                lines=["Project Plan PP-12", "Draft schedule and team. All names are invented.", "1. Schedule", "2. Team",
                       "The project runs in three phases as shown below.",
                       "The following people are assigned to the project.",
                       "Dates may change by agreement of both teams.", "Page 1 of 1"],
                tables=[dict(header=t1[0], rows=t1[1]), dict(header=t2[0], rows=t2[1])])


def no_table(path):
    s = Sheet()
    s.line("Meeting Note MN-5", 20, bold=True, gap=8)
    s.line("Weekly review. All names are invented.", gap=22)
    texts = [("1. Attendees", "Four people attended: the lead, two engineers and one reviewer."),
             ("2. Decisions", "The team agreed to ship the report on Friday and to freeze changes on "
                              "Thursday evening."),
             ("3. Actions", "Send the final draft to notes@example.com by 17:00 on Wednesday. "
                            "Reference MN-5-2026."),
             ("4. Next meeting", "The next review is on 12 November 2026 at 10:30.")]
    for head, body in texts:
        s.line(head, 12, bold=True)
        s.paragraph(body)
    s.footer("Page 1 of 1")
    s.save_as_scan(path)
    return dict(describe="control: text only. A correct conversion must NOT invent a table",
                lines=["Meeting Note MN-5", "Weekly review. All names are invented."] + [x for h, b in texts for x in (h, b)] + ["Page 1 of 1"],
                tables=[])


def wide_table(path):
    s = Sheet()
    s.line("Stock Report SR-9", 20, bold=True, gap=8)
    s.line("Warehouse A, month end. All items are invented.", gap=22)
    t = (["SKU", "Description", "Qty", "Value (INR)"],
         [["A-101", "Paper ream A4", "120", "54,000.00"], ["A-102", "Toner cartridge", "18", "57,600.00"],
          ["A-103", "Stapler heavy duty", "35", "9,625.00"], ["B-201", "Folder set of 10", "64", "38,400.00"],
          ["B-202", "Marker pen pack", "90", "13,500.00"], ["C-301", "Desk lamp LED", "12", "21,600.00"]])
    s.table([70, 210, 60, 120], *t)
    s.paragraph("Totals were counted on 30 October 2026.")
    s.footer("Page 1 of 1")
    s.save_as_scan(path)
    return dict(describe="four-column table with six rows, a note below, footer",
                lines=["Stock Report SR-9", "Warehouse A, month end. All items are invented.", "Totals were counted on 30 October 2026.", "Page 1 of 1"],
                tables=[dict(header=t[0], rows=t[1])])


if __name__ == "__main__":
    expected = {}
    for fn in (table_high, table_low, two_tables, no_table, wide_table):
        name = f"scan_{fn.__name__}.pdf"
        expected[name] = fn(OUT / name)
        print("wrote", name)
    (OUT / "expected.json").write_text(json.dumps(expected, indent=2, ensure_ascii=False))
    print("wrote expected.json")
