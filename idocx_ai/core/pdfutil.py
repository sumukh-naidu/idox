"""Helpers shared by every tool: open by file ID, save a new output, page numbers, rendering."""

from pathlib import Path

import pymupdf
from PIL import Image

from core import store

STANDARD_INFO_KEYS = ("Title", "Author", "Subject", "Keywords", "Creator", "Producer",
                      "CreationDate", "ModDate", "Trapped")


class ToolError(Exception):
    pass


def info_keys(doc) -> list[str]:
    """Every key in the document Info dictionary, including custom ones."""
    kind, val = doc.xref_get_key(-1, "Info")
    if kind != "xref":
        return []
    return doc.xref_get_keys(int(val.split()[0]))


def open_pdf(file_id: str):
    try:
        meta, path = store.get_file(file_id)
    except store.NotFound as e:
        raise ToolError(str(e))
    return meta, pymupdf.open(path)


def save_new(doc, name: str, parents: list[str], source: str, page_origin: list) -> dict:
    """Write doc as a NEW file. garbage=4 drops orphaned objects, so removed
    metadata or pages are gone from the bytes, not merely unreferenced."""
    data = doc.tobytes(garbage=4, deflate=True)
    return store.save_file(data, name, pages=doc.page_count, source=source, parents=parents,
                           page_origin=page_origin)


def origin_of(meta: dict) -> list:
    return meta.get("page_origin") or [[meta["id"], n] for n in range(1, meta["pages"] + 1)]


def derived_name(original: str, suffix: str) -> str:
    return f"{Path(original).stem}-{suffix}.pdf"


def page_ranges(pages: list[int]) -> str:
    """[1, 2, 3, 7, 9, 10] -> '1-3, 7, 9-10'"""
    out, start, prev = [], None, None
    for p in sorted(pages):
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = p
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(out) or "none"


def check_pages(pages, page_count: int) -> list[int]:
    """Validate 1-based page numbers from the model; return them sorted and unique."""
    if not isinstance(pages, list) or not pages:
        raise ToolError("pages must be a non-empty list of page numbers, e.g. [3, 7]")
    try:
        nums = sorted({int(p) for p in pages})
    except (TypeError, ValueError):
        raise ToolError(f"page numbers must be integers, got {pages}")
    bad = [p for p in nums if not 1 <= p <= page_count]
    if bad:
        raise ToolError(f"pages {bad} do not exist; this file has pages 1-{page_count}")
    return nums


def render_gray(page, dpi: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)
