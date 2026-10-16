"""
to_csv.py -- write extracted pages as a CSV file: text as lines, tables as rows and columns.

Same Page objects as to_xlsx.py / to_pptx.py, a different writer. Nothing here asks the model anything.

Everything on the page goes in, in reading order:

    heading / paragraph / caption   ONE row holding the whole text on ONE line (line breaks become spaces)
    list                            one row per item, the bullet or number removed
    table                           its header row, then one row per data row, one cell per column

A blank row sits before and after every table, so a table is easy to see and to cut out; prose lines follow
each other with no blank rows. Rows with every cell empty are dropped (reported as a problem), so a blank row
always means "a table starts or ends here". Pages follow each other without a marker.

The file is UTF-8 with a byte-order mark so Excel opens accented text and symbols correctly. It is ragged on
purpose: a text line is one cell, a table row has as many cells as the table has columns.

verify_csv() reads the SAVED file back and checks it against the source text, the same way verify_xlsx() does.
"""

import csv
import re
from typing import List

from blocks import Page, TableBlock, normalize, squash

_BULLET = re.compile(r"^\s*(?:[-*•●▪–]|\d+[.)])\s+")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def build_csv(pages: List[Page], path: str):
    """Write every block of every page. Returns a list of problems found.

    Writes NOTHING when the pages hold no content at all.
    """
    problems: List[str] = []
    out: List[list] = []          # rows; [] is the blank row around a table

    def blank():
        if out and out[-1] != []:
            out.append([])

    for pno, page in enumerate(pages, start=1):
        for bno, block in enumerate(page.blocks):
            where = f"page {pno} block[{bno}]"
            if isinstance(block, TableBlock):
                if not block.rows:
                    problems.append(f"{where}: table skipped (no rows)")
                    continue
                width = len(block.header) if block.has_header else len(block.rows[0])
                if width == 0:
                    problems.append(f"{where}: table skipped (no columns)")
                    continue
                rows = [list(block.header[:width])] if block.has_header else []
                for r, source_row in enumerate(block.rows):
                    # A row with nothing in it is never data: it is the model inventing a row, or a divider.
                    if not any(c.strip() for c in source_row):
                        problems.append(f"{where} row {r}: empty row dropped")
                        continue
                    if len(source_row) != width:
                        problems.append(f"{where} row {r}: had {len(source_row)} cells, grid is {width} -- "
                                        f"{'padded' if len(source_row) < width else 'truncated'}")
                    rows.append((list(source_row) + [""] * width)[:width])
                blank()
                out.extend(rows)
                out.append([])
                continue

            if block.kind == "list":
                items = [_BULLET.sub("", line).strip() for line in block.text.splitlines()]
                items = [i for i in items if i]
                if not items:
                    problems.append(f"{where}: empty list, skipped")
                out.extend([item] for item in items)
                continue
            text = _one_line(block.text)
            if not text:
                problems.append(f"{where}: empty {block.kind}, skipped")
                continue
            out.append([text])

    while out and out[-1] == []:
        out.pop()
    if not out:
        problems.append("nothing to write: no text and no table")
        return problems

    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        csv.writer(fh).writerows(out)
    return problems


def verify_csv(path: str, source_text: str):
    """Read the written .csv back and check it against the source text.

    Returns (coverage, missing_words), the same shape as verify_xlsx()/verify_xml(): the share of the source's
    distinct words (longer than 3 characters) found in the file, and the ones that are not.
    """
    with open(path, newline="", encoding="utf-8-sig") as fh:
        cells = [cell for row in csv.reader(fh) for cell in row]
    written = normalize(" ".join(cells))
    written_squashed = squash(" ".join(cells))

    words = [w for w in normalize(source_text).split() if len(w) > 3]
    if not words:
        return 1.0, []
    missing = sorted({w for w in words if w not in written and squash(w) not in written_squashed})
    return 1 - len(missing) / len(set(words)), missing
