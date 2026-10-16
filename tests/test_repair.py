"""Tests for repair.restore_missing_lines: dropped lines go back at their real place and are never duplicated.
None of them calls the model. Run from the idox folder:  python -m unittest discover -s tests -p "test_repair.py" -v
"""
import unittest

import _load  # noqa: F401  (puts the repo on sys.path)
import pymupdf
from test_no_model import page_of, table, text

from blocks import TableBlock, check_coverage
from repair import drop_spurious_blocks, reorder_blocks, restore_missing_lines

PITCH = 14        # line spacing for 12pt text: lines of one paragraph touch
GAP = 28          # a paragraph break


def make_pdf(paragraphs, size=12):
    """A one-page PDF; each paragraph is a list of lines. Returns the pymupdf page."""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for lines in paragraphs:
        for line in lines:
            page.insert_text((72, y), line, fontsize=size)
            y += PITCH
        y += GAP - PITCH
    return page


def all_text(page):
    return " ".join(b.text if not isinstance(b, TableBlock) else "" for b in page.blocks)


def repair(page_blocks, pdf_page):
    source = pdf_page.get_text()
    page = page_of(page_blocks)
    _, _, _, missing = check_coverage(page, source)
    return restore_missing_lines(page, pdf_page, missing)


class RestoreMissingLines(unittest.TestCase):
    L1 = "alpha bravo charlie delta echo foxtrot golf hotel"
    L2 = "india juliet kilo lima mike november oscar papa"
    L3 = "quebec romeo sierra tango uniform victor whiskey xray"

    def test_line_dropped_from_the_middle_of_a_paragraph_is_spliced_back_in_place(self):
        pdf = make_pdf([[self.L1, self.L2, self.L3]])
        page, repairs, _ = repair([text("paragraph", f"{self.L1} {self.L3}")], pdf)
        self.assertEqual(len(page.blocks), 1, "the line belongs inside the paragraph, not in a new block")
        self.assertEqual(page.blocks[0].text.split(), f"{self.L1} {self.L2} {self.L3}".split())
        self.assertEqual(len(repairs), 1)

    def test_a_misread_line_is_not_restored_again(self):
        pdf = make_pdf([[self.L1, self.L2, self.L3]])
        misread = self.L2.replace("kilo", "kllo").replace("oscar", "oscer")
        blocks = [text("paragraph", f"{self.L1} {misread} {self.L3}")]
        page, repairs, _ = repair(blocks, pdf)
        self.assertEqual(repairs, [], "present but misread is not missing; inserting it would double the text")
        self.assertEqual(len(page.blocks), 1)

    def test_a_dropped_paragraph_goes_between_its_neighbours_as_one_block(self):
        p1 = ["first paragraph opens here with some words", "and carries on for a second line of text"]
        p2 = ["second paragraph that the model dropped entirely", "continuing on its own second line as well"]
        p3 = ["third paragraph closes the page with words", "and ends on a second line of its own"]
        pdf = make_pdf([p1, p2, p3])
        blocks = [text("paragraph", " ".join(p1)), text("paragraph", " ".join(p3))]
        page, repairs, _ = repair(blocks, pdf)
        self.assertEqual([b.text for b in page.blocks], [" ".join(p1), " ".join(p2), " ".join(p3)])

    def test_a_dropped_first_paragraph_is_put_at_the_start(self):
        p1 = ["opening paragraph the model forgot", "which really starts the page"]
        p2 = ["following paragraph that was kept", "so the order has a landmark"]
        pdf = make_pdf([p1, p2])
        page, _, _ = repair([text("paragraph", " ".join(p2))], pdf)
        self.assertEqual([b.text for b in page.blocks], [" ".join(p1), " ".join(p2)])

    def test_nothing_missing_changes_nothing(self):
        pdf = make_pdf([[self.L1, self.L2]])
        page = page_of([text("paragraph", f"{self.L1} {self.L2}")])
        out, repairs, skipped = restore_missing_lines(page, pdf, [])
        self.assertIs(out, page)
        self.assertEqual((repairs, skipped), ([], []))


class ReorderBlocks(unittest.TestCase):
    A = "alpha bravo charlie delta echo foxtrot golf hotel"
    B = "india juliet kilo lima mike november oscar papa"
    C = "quebec romeo sierra tango uniform victor whiskey xray"

    def test_blocks_come_back_in_the_pdf_order(self):
        pdf = make_pdf([[self.A], [self.B], [self.C]])
        page = page_of([text("paragraph", self.C), text("paragraph", self.A), text("paragraph", self.B)])
        out, moves = reorder_blocks(page, pdf)
        self.assertEqual([b.text for b in out.blocks], [self.A, self.B, self.C])
        self.assertTrue(moves)

    def test_blocks_already_in_order_are_left_alone(self):
        pdf = make_pdf([[self.A], [self.B]])
        page = page_of([text("paragraph", self.A), text("paragraph", self.B)])
        out, moves = reorder_blocks(page, pdf)
        self.assertIs(out, page)
        self.assertEqual(moves, [])

    def test_repeated_text_is_never_reordered(self):
        pdf = make_pdf([[self.A], [self.B], [self.A]])        # self.A occurs twice: its place is ambiguous
        page = page_of([text("paragraph", self.B), text("paragraph", self.A)])
        out, _ = reorder_blocks(page, pdf)
        self.assertEqual([b.text for b in out.blocks], [self.B, self.A])

    def test_a_short_heading_stays_behind_the_block_before_it(self):
        pdf = make_pdf([[self.A], ["Short Heading"], [self.B]])
        page = page_of([text("paragraph", self.B), text("paragraph", self.A), text("heading", "Short Heading")])
        out, _ = reorder_blocks(page, pdf)
        self.assertEqual([b.text for b in out.blocks], [self.A, "Short Heading", self.B])

    def test_a_short_heading_that_came_after_its_content_is_found_and_moved_back(self):
        pdf = make_pdf([["Quarterly Totals"], [self.A], [self.B]])
        page = page_of([text("paragraph", self.A), text("paragraph", self.B), text("heading", "Quarterly Totals")])
        out, _ = reorder_blocks(page, pdf)
        self.assertEqual([b.text for b in out.blocks], ["Quarterly Totals", self.A, self.B])

    def test_a_short_heading_that_occurs_twice_is_not_moved(self):
        pdf = make_pdf([["Notes Section"], [self.A], ["Notes Section"], [self.B]])
        page = page_of([text("paragraph", self.A), text("paragraph", self.B), text("heading", "Notes Section")])
        out, _ = reorder_blocks(page, pdf)
        self.assertEqual([b.text for b in out.blocks], [self.A, self.B, "Notes Section"])

    def test_the_real_page_where_the_table_came_before_its_caption_and_heading(self):
        """Page 2 of pdf_to_ppt_input/sample_test_document.pdf, with the block order the model really wrote."""
        import os
        path = os.path.join(_load.REPO, "input", "pdf_to_ppt_input", "sample_test_document.pdf")
        if not os.path.exists(path):
            self.skipTest("sample PDF not present")
        pdf_page = pymupdf.open(path)[1]
        header = ["Equipment ID", "Last Inspected", "Status", "Next Due"]
        rows = [["FL-102", "2026-07-12", "Pass", "2026-10-12"], ["FL-103", "2026-07-15", "Pass", "2026-10-15"],
                ["FL-107", "2026-07-18", "Flagged", "2026-08-01"], ["DK-3-GATE", "2026-08-02", "Pass", "2026-11-02"]]
        blocks = [text("heading", "Facility Compliance Notes"),
                  text("paragraph", "The warehouse safety audit conducted this quarter identified one active violation, "
                                    "shown in the notice below. Facilities management has been notified and corrective "
                                    "signage has been ordered."),
                  text("paragraph", "Warehouse Safety Notice"),
                  table(header, rows),
                  text("caption", "Figure 1: Warehouse safety notice posted at Dock 3"),
                  text("heading", "Equipment Inspection Log")]
        out, moves = reorder_blocks(page_of(blocks), pdf_page)
        kinds = [("table" if isinstance(b, TableBlock) else b.text[:10]) for b in out.blocks]
        self.assertEqual(kinds, ["Facility C", "The wareho", "Warehouse ", "Figure 1: ", "Equipment ", "table"])


class DropSpuriousBlocks(unittest.TestCase):
    PARA = ["the first paragraph holds quite a few ordinary words", "that continue across a second line of plain text",
            "and then a third line closes this paragraph here"]
    OTHER = ["a different paragraph carries its own separate vocabulary", "spread over two lines with nothing shared",
             "and a third line to keep it long enough to judge"]

    def test_a_paragraph_the_model_wrote_twice_is_dropped(self):
        pdf = make_pdf([self.PARA, self.OTHER])
        one, two = " ".join(self.PARA), " ".join(self.OTHER)
        page = page_of([text("paragraph", one), text("paragraph", two), text("paragraph", one)])
        out, dropped = drop_spurious_blocks(page, pdf)
        self.assertEqual([b.text for b in out.blocks], [one, two])
        self.assertEqual(len(dropped), 1)

    def test_text_read_out_of_a_picture_is_kept(self):
        pdf = make_pdf([self.PARA])
        picture = "text that exists only inside a picture and is therefore nowhere in the text layer of this document"
        page = page_of([text("paragraph", " ".join(self.PARA)), text("paragraph", picture)])
        out, dropped = drop_spurious_blocks(page, pdf)
        self.assertEqual(len(out.blocks), 2)
        self.assertEqual(dropped, [])

    def test_short_blocks_and_tables_are_never_dropped(self):
        pdf = make_pdf([self.PARA, ["Short Heading"]])
        page = page_of([text("paragraph", " ".join(self.PARA)), text("heading", "Short Heading"),
                        text("heading", "Short Heading"), table(["A", "B"], [["1", "2"]])])
        out, dropped = drop_spurious_blocks(page, pdf)
        self.assertEqual(len(out.blocks), 4)
        self.assertEqual(dropped, [])

    def test_a_clean_page_is_returned_unchanged(self):
        pdf = make_pdf([self.PARA, self.OTHER])
        page = page_of([text("paragraph", " ".join(self.PARA)), text("paragraph", " ".join(self.OTHER))])
        out, dropped = drop_spurious_blocks(page, pdf)
        self.assertIs(out, page)
        self.assertEqual(dropped, [])


if __name__ == "__main__":
    unittest.main()


class WholeTableRestore(unittest.TestCase):
    """repair_table_rows rebuilds a table the model never returned, but not one it wrote as prose."""

    @classmethod
    def setUpClass(cls):
        cls.repair = staticmethod(_load.load_script_defs("test_pdf.py").repair_table_rows)
        doc = pymupdf.open()
        pg = doc.new_page()
        pg.insert_text((72, 60), "Sales by region", fontsize=14)
        cells = [["Region", "Units", "Revenue"], ["North", "120", "4500"], ["South", "95", "3800"], ["West", "60", "2100"]]
        x0, y0, cw, rh = 72, 90, 110, 24
        for r, row in enumerate(cells):
            for c, val in enumerate(row):
                rect = pymupdf.Rect(x0 + c * cw, y0 + r * rh, x0 + (c + 1) * cw, y0 + (r + 1) * rh)
                pg.draw_rect(rect, width=0.8)
                pg.insert_text((rect.x0 + 4, rect.y0 + 16), val, fontsize=10)
        cls.pdf_page = pg

    def test_missing_table_is_restored(self):
        page = page_of([text("heading", "Sales by region")])
        new, repairs = self.repair(page, self.pdf_page)
        tables = [b for b in new.blocks if isinstance(b, TableBlock)]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].header, ["Region", "Units", "Revenue"])
        self.assertEqual(len(tables[0].rows), 3)

    def test_table_written_as_prose_is_not_duplicated(self):
        page = page_of([text("heading", "Sales by region"),
                        text("paragraph", "Region Units Revenue North 120 4500 South 95 3800 West 60 2100")])
        new, repairs = self.repair(page, self.pdf_page)
        self.assertFalse(any(isinstance(b, TableBlock) for b in new.blocks))
        self.assertEqual(repairs, [])

    def test_a_dropped_column_rebuilds_the_table_without_duplicating_rows(self):
        model = table(["Region", "Units"], [["North", "120"], ["South", "95"], ["West", "60"]])
        new, repairs = self.repair(page_of([text("heading", "Sales by region"), model]), self.pdf_page)
        tables = [b for b in new.blocks if isinstance(b, TableBlock)]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].header, ["Region", "Units", "Revenue"])
        self.assertEqual([r[0] for r in tables[0].rows], ["North", "South", "West"])
        self.assertTrue(any("3 columns" in r for r in repairs))

    def test_a_missing_row_is_still_restored_and_present_rows_are_not_repeated(self):
        model = table(["Region", "Units", "Revenue"], [["North", "120", "4500"], ["West", "60", "2100"]])
        new, repairs = self.repair(page_of([text("heading", "Sales by region"), model]), self.pdf_page)
        rows = [b for b in new.blocks if isinstance(b, TableBlock)][0].rows
        self.assertEqual([r[0] for r in rows], ["North", "South", "West"])


class TablePairing(unittest.TestCase):
    """A model table is repaired from the PDF table it shares cells with, not from whichever table comes first."""

    def test_the_second_pdf_table_is_not_mixed_into_a_model_table_that_is_the_first_the_model_wrote(self):
        doc = pymupdf.open()
        pg = doc.new_page()

        def grid(y0, cells):
            for r, row in enumerate(cells):
                for c, val in enumerate(row):
                    rect = pymupdf.Rect(72 + c * 110, y0 + r * 24, 72 + (c + 1) * 110, y0 + (r + 1) * 24)
                    pg.draw_rect(rect, width=0.8)
                    pg.insert_text((rect.x0 + 4, rect.y0 + 16), val, fontsize=10)
        grid(80, [["Region", "Units"], ["North", "120"], ["South", "95"]])
        grid(300, [["Case", "Input"], ["01", "Short"], ["02", "Longer text"], ["03", "Mixed"]])
        repair = staticmethod(_load.load_script_defs("test_pdf.py").repair_table_rows)
        # the model wrote only the second table, and missed its last row
        model = table(["Case", "Input"], [["01", "Short"], ["02", "Longer text"]])
        new, _ = repair(page_of([model]), pg)
        rows = [b for b in new.blocks if isinstance(b, TableBlock)][0].rows
        self.assertEqual([r[0] for r in rows], ["01", "02", "03"])
