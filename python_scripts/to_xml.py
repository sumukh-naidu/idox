"""
to_xml.py -- write extracted pages as an XML document.

Same Page objects as to_xlsx.py / to_pptx.py, a different writer. Nothing here asks the model anything.

    <document source="report.pdf" pages="2">
      <page number="1">
        <heading>Quarterly report</heading>
        <paragraph>Text of a paragraph.</paragraph>
        <list><item>first</item><item>second</item></list>
        <caption>Figure 1</caption>
        <table>
          <header><cell>Item</cell><cell>Price</cell></header>
          <row><cell>Pen</cell><cell>1.20</cell></row>
        </table>
      </page>
    </document>

Block kinds map one to one: heading, paragraph, caption, list (one <item> per line), image (the model's
description of a picture) and table. Page order and reading order are kept. Characters XML 1.0 cannot hold
(control codes) are dropped, so the file always parses.

verify_xml() parses the SAVED file back, so it also proves the file is well-formed XML.
"""

import re
import xml.etree.ElementTree as ET
from typing import List

from blocks import Page, TableBlock, normalize, squash

_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_BULLET = re.compile(r"^\s*(?:[-*•●▪–]|\d+[.)])\s+")


def _clean(text: str) -> str:
    return _ILLEGAL.sub("", text)


def _table(parent, block: TableBlock, where: str, problems: List[str]):
    if not block.rows:
        problems.append(f"{where}: table skipped (no rows)")
        return
    width = len(block.header) if block.has_header else len(block.rows[0])
    if width == 0:
        problems.append(f"{where}: table skipped (no columns)")
        return
    table = ET.SubElement(parent, "table")
    if block.has_header:
        head = ET.SubElement(table, "header")
        for label in block.header[:width]:
            ET.SubElement(head, "cell").text = _clean(label)
    for r, source_row in enumerate(block.rows):
        if len(source_row) != width:
            problems.append(f"{where} row {r}: had {len(source_row)} cells, grid is {width} -- "
                            f"{'padded' if len(source_row) < width else 'truncated'}")
        row = ET.SubElement(table, "row")
        for value in (list(source_row) + [""] * width)[:width]:
            ET.SubElement(row, "cell").text = _clean(value)


def build_xml(pages: List[Page], path: str, source_name: str = ""):
    """Write every page. Returns a list of problems found."""
    problems: List[str] = []
    root = ET.Element("document", pages=str(len(pages)))
    if source_name:
        root.set("source", _clean(source_name))

    for pno, page in enumerate(pages, start=1):
        page_el = ET.SubElement(root, "page", number=str(pno))
        for bno, block in enumerate(page.blocks):
            where = f"page {pno} block[{bno}]"
            if isinstance(block, TableBlock):
                _table(page_el, block, where, problems)
                continue
            text = _clean(block.text).strip()
            if not text:
                problems.append(f"{where}: empty {block.kind}, skipped")
                continue
            if block.kind == "list":
                list_el = ET.SubElement(page_el, "list")
                for line in text.splitlines():
                    line = _BULLET.sub("", line).strip()
                    if line:
                        ET.SubElement(list_el, "item").text = line
            else:
                ET.SubElement(page_el, block.kind).text = text

    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return problems


def verify_xml(path: str, source_text: str):
    """Parse the written .xml back and check it against the source text.

    Returns (coverage, missing_words), the same shape as verify_xlsx()/verify_pptx().
    """
    root = ET.parse(path).getroot()
    parts = [el.text for el in root.iter() if el.text and el.text.strip()]
    written = normalize(" ".join(parts))
    written_squashed = squash(" ".join(parts))

    words = [w for w in normalize(source_text).split() if len(w) > 3]
    if not words:
        return 1.0, []
    missing = sorted({w for w in words if w not in written and squash(w) not in written_squashed})
    return 1 - len(missing) / len(set(words)), missing
