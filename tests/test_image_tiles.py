"""Tests for image_tiles: where a page picture is cut, and how the strips' blocks are joined. No model is called.
Run from the idox folder:  python -m unittest discover -s tests -p "test_image_tiles.py" -v
"""
import os
import tempfile
import unittest

import _load  # noqa: F401  (puts the repo on sys.path)
from PIL import Image, ImageDraw
from test_no_model import page_of, text

from image_tiles import extract_page_tiled, split_points


def page_with_text_bands(height=1000, gap=(480, 540), width=600):
    """A white page with black bands standing for lines of text, except for an empty gap."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    for y in range(40, height - 40, 24):
        if gap[0] <= y <= gap[1] - 12:
            continue
        draw.rectangle([40, y, width - 40, y + 12], fill="black")
    return img


class SplitPoints(unittest.TestCase):
    def test_one_part_is_the_whole_page(self):
        self.assertEqual(split_points(page_with_text_bands(), 1), [(0, 1000)])

    def test_the_cut_lands_in_the_empty_gap_and_strips_tile_the_page(self):
        (t1, b1), (t2, b2) = split_points(page_with_text_bands(gap=(480, 560)), 2)
        self.assertEqual((t1, b2), (0, 1000))
        self.assertEqual(b1, t2, "with a gap there is no overlap")
        self.assertTrue(480 <= b1 <= 560, f"cut at {b1} is not inside the empty gap")

    def test_three_strips(self):
        boxes = split_points(page_with_text_bands(height=1500, gap=(490, 520)), 3)
        self.assertEqual(len(boxes), 3)
        self.assertEqual((boxes[0][0], boxes[-1][1]), (0, 1500))
        for (_, b), (t, _) in zip(boxes, boxes[1:]):
            self.assertLessEqual(t, b)

    def test_a_page_with_no_gap_is_cut_with_a_small_overlap(self):
        img = Image.new("RGB", (600, 1000), "white")
        draw = ImageDraw.Draw(img)
        for y in range(0, 1000, 14):
            draw.rectangle([40, y, 560, y + 12], fill="black")          # ink everywhere
        (t1, b1), (t2, b2) = split_points(img, 2)
        self.assertLess(t2, b1, "no empty band, so the strips overlap")

    def test_a_tiny_picture_is_never_split(self):
        self.assertEqual(split_points(Image.new("RGB", (100, 150), "white"), 3), [(0, 150)])


class FakeModel:
    """Stands in for extract_page: names each strip it is given and records its height."""
    def __init__(self):
        self.heights = []

    def __call__(self, path, **kwargs):
        with Image.open(path) as im:
            self.heights.append(im.height)
        n = len(self.heights)
        return page_of([text("paragraph", f"words of strip {n} read here")]), {"prefill_s": 1.0, "generate_s": 2.0}


class ExtractTiled(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "page.png")
        page_with_text_bands().save(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_blocks_come_back_in_strip_order_and_timings_add_up(self):
        fake = FakeModel()
        page, timing = extract_page_tiled(self.path, parts=2, extractor=fake, return_timing=True)
        self.assertEqual([b.text for b in page.blocks], ["words of strip 1 read here", "words of strip 2 read here"])
        self.assertEqual(len(fake.heights), 2)
        self.assertEqual(timing, {"prefill_s": 2.0, "generate_s": 4.0})

    def test_one_part_makes_exactly_one_read_of_the_original_file(self):
        calls = []

        def extractor(path, **kwargs):
            calls.append(path)
            return page_of([text("paragraph", "whole page")]), {}

        extract_page_tiled(self.path, parts=1, extractor=extractor, return_timing=True)
        self.assertEqual(calls, [self.path])

    def test_a_block_read_twice_in_an_overlap_is_removed(self):
        img = Image.new("RGB", (600, 1000), "white")
        draw = ImageDraw.Draw(img)
        for y in range(0, 1000, 14):
            draw.rectangle([40, y, 560, y + 12], fill="black")
        path = os.path.join(self.dir.name, "tight.png")
        img.save(path)
        same = "the same long sentence that both strips read inside the overlap band of the page"

        def extractor(p, **kwargs):
            return page_of([text("paragraph", same)]), {}

        page, _ = extract_page_tiled(path, parts=2, extractor=extractor, return_timing=True)
        self.assertEqual(len(page.blocks), 1)


class AutoParts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def save(self, height, name):
        path = os.path.join(self.dir.name, name)
        page_with_text_bands(height=height, gap=(height // 2 - 30, height // 2 + 30)).save(path)
        return path

    def test_a_full_page_picture_is_read_in_two_strips(self):
        from image_tiles import auto_parts
        self.assertEqual(auto_parts(self.save(1650, "full.png")), 2)

    def test_a_big_model_reads_a_full_page_whole(self):
        from image_tiles import auto_parts
        self.assertEqual(auto_parts(self.save(1650, "big.png"), big_model=True), 1)

    def test_model_size_is_read_from_the_server_and_a_dead_server_means_unknown(self):
        from unittest import mock
        import image_tiles
        reply = mock.Mock()
        reply.json.return_value = {"data": [{"meta": {"n_params": 4022468096}}]}
        with mock.patch("requests.get", return_value=reply):
            self.assertEqual(image_tiles.model_params("http://x"), 4022468096)
        with mock.patch("requests.get", side_effect=OSError("down")):
            self.assertIsNone(image_tiles.model_params("http://x"))

    def test_a_short_picture_is_read_whole(self):
        from image_tiles import auto_parts
        self.assertEqual(auto_parts(self.save(900, "short.png")), 1)

    def test_extract_page_auto_reads_twice_for_a_tall_picture_and_once_for_a_short_one(self):
        from image_tiles import extract_page_auto
        tall, short = FakeModel(), FakeModel()
        extract_page_auto(self.save(1650, "t.png"), extractor=tall, return_timing=True)
        extract_page_auto(self.save(900, "s.png"), extractor=short, return_timing=True)
        self.assertEqual((len(tall.heights), len(short.heights)), (2, 1))

    def test_the_idox_split_variable_overrides_the_rule(self):
        from image_tiles import auto_parts
        tall = self.save(1650, "env.png")
        os.environ["IDOX_SPLIT"] = "1"
        try:
            self.assertEqual(auto_parts(tall), 1)
            os.environ["IDOX_SPLIT"] = "3"
            self.assertEqual(auto_parts(self.save(900, "env2.png")), 3)
            os.environ["IDOX_SPLIT"] = "junk"
            self.assertEqual(auto_parts(tall), 2, "a value that is not a number is ignored")
        finally:
            del os.environ["IDOX_SPLIT"]

    def test_parts_one_forces_a_single_read_even_for_a_tall_picture(self):
        from image_tiles import extract_page_auto
        fake = FakeModel()
        extract_page_auto(self.save(1650, "f.png"), parts=1, extractor=fake, return_timing=True)
        self.assertEqual(len(fake.heights), 1)


class SeamJoin(unittest.TestCase):
    @staticmethod
    def rows_table(rows, has_header=False):
        from blocks import TableBlock
        n = len(rows[0])
        return TableBlock(kind="table", has_header=has_header, n_data_rows=len(rows), n_cols=n,
                          header=["A"] * n if has_header else [], rows=rows)

    def test_a_table_cut_by_the_seam_is_joined_back_into_one(self):
        from image_tiles import _join_seam
        top = [self.rows_table([["1", "2"], ["3", "4"]], has_header=True)]
        bottom = [self.rows_table([["5", "6"], ["7", "8"]])]
        out = _join_seam(top, bottom)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].rows, [["1", "2"], ["3", "4"], ["5", "6"], ["7", "8"]])
        self.assertEqual(out[0].n_data_rows, 4)

    def test_two_separate_tables_are_not_joined(self):
        from image_tiles import _join_seam
        top = [self.rows_table([["1", "2"]], has_header=True)]
        bottom = [self.rows_table([["x", "y"]], has_header=True)]       # the lower one has its own header
        self.assertEqual(len(_join_seam(top, bottom)), 2)

    def test_tables_with_a_different_column_count_are_not_joined(self):
        from image_tiles import _join_seam
        top = [self.rows_table([["1", "2"]], has_header=True)]
        bottom = [self.rows_table([["a", "b", "c"]])]
        self.assertEqual(len(_join_seam(top, bottom)), 2)

    def test_text_blocks_are_just_appended(self):
        from image_tiles import _join_seam
        out = _join_seam([text("paragraph", "one")], [text("paragraph", "two")])
        self.assertEqual([b.text for b in out], ["one", "two"])


if __name__ == "__main__":
    unittest.main()


class PromptChoice(unittest.TestCase):
    """prompts_for: the page prompt for a tall picture, the plain one for a short picture, the override wins."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def pic(self, h):
        path = os.path.join(self.dir.name, f"{h}.png")
        Image.new("RGB", (800, h), "white").save(path)
        return path

    def test_short_picture_gets_the_plain_prompt_and_a_tall_one_the_page_prompt(self):
        import blocks
        self.assertEqual(blocks.prompts_for(self.pic(900)), (blocks.SYSTEM_PROMPT_SHORT, blocks.USER_PROMPT_SHORT))
        self.assertEqual(blocks.prompts_for(self.pic(1650)), (blocks.SYSTEM_PROMPT, blocks.USER_PROMPT))

    def test_an_unreadable_picture_falls_back_to_the_page_prompt(self):
        import blocks
        self.assertEqual(blocks.prompts_for("no_such_file.png"), (blocks.SYSTEM_PROMPT, blocks.USER_PROMPT))


class SplitMergedTitle(unittest.TestCase):
    """A title the model wrote inside the first paragraph becomes a heading; anything else is left alone."""

    def test_title_is_split_only_when_it_is_clearly_a_title(self):
        from image_tiles import split_merged_title
        nl = chr(10)
        merged = page_of([text("paragraph", "Sample PDF Document" + nl + "This is a sample one-page PDF generated for demonstration purposes.")])
        out = split_merged_title(merged)
        self.assertEqual([b.kind for b in out.blocks], ["heading", "paragraph"])
        self.assertEqual(out.blocks[0].text, "Sample PDF Document")
        self.assertEqual(out.blocks[1].text, "This is a sample one-page PDF generated for demonstration purposes.")
        for keep in ("Dear John," + nl + "Thank you for your letter of the third of March, which we have read with care.",
                     "A short line with no break that is just a paragraph of text.",
                     "Acme Corp" + nl + "123 Main Street"):
            page = page_of([text("paragraph", keep)])
            self.assertIs(split_merged_title(page), page, keep)
