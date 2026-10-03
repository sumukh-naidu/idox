"""Reading text out of PDF pages, shared by every feature that needs it.

A page's own text layer is used when it has one; a scanned page is read by OCR.
File IDs never change content, so results are cached for the life of the process.
"""

import pymupdf

OCR_DPI = 200

_TEXT: dict[tuple[str, int], tuple[str, str]] = {}


def textpage(page):
    """(TextPage, source): the page's own text if it has any, else OCR. None for an empty page."""
    if page.get_text("text").strip():
        return page.get_textpage(), "text layer"
    if page.get_images():
        return page.get_textpage_ocr(dpi=OCR_DPI, full=True, tessdata=pymupdf.get_tessdata()), "OCR"
    return None, "empty page"


def page_text(doc, file_id: str, page_no: int) -> tuple[str, str]:
    """(text, source) for a 1-based page, cached."""
    key = (file_id, page_no)
    if key not in _TEXT:
        page = doc[page_no - 1]
        tp, source = textpage(page)
        _TEXT[key] = (page.get_text("text", textpage=tp).strip() if tp else "", source)
    return _TEXT[key]
