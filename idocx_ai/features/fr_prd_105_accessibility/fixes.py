"""FR-PRD-105 fixes that are safe to apply automatically: an OCR text layer on
scanned pages, the declared language, and a title the viewer shows.

Tags, reading order and font embedding need the document re-authored, so they are
reported, not faked. Every fix is verified by re-running the checks on the new file.
"""

import pymupdf

from core.pdfutil import ToolError, derived_name, open_pdf, origin_of, page_ranges, save_new

from .checks import LANG_TAG, catalog_key, detect_language, run_checks, scanned_pages

OCR_DPI = 300
# Invisible OCR text needs a font. A built-in one is embedded (and subset) so the
# fix does not itself break the "all fonts embedded" check.
OCR_FONT = "ocrtext"


def _add_text_layer(page) -> int:
    """Invisible text, one line per OCR line, stretched to the line's real width so
    selecting text lines up with the image. Word by word gave each word its own size,
    which split lines and spaced words oddly."""
    tp = page.get_textpage_ocr(dpi=OCR_DPI, full=True, tessdata=pymupdf.get_tessdata())
    words = page.get_text("words", textpage=tp)
    if not words:
        return 0
    lines: dict[tuple[int, int], list] = {}
    for x0, y0, x1, y1, word, block, line, _ in words:
        lines.setdefault((block, line), []).append((x0, y0, x1, y1, word))
    page.insert_font(fontname=OCR_FONT, fontbuffer=pymupdf.Font("helv").buffer)
    for ws in lines.values():
        ws.sort()
        text = " ".join(w[4] for w in ws)
        x0, x1 = ws[0][0], max(w[2] for w in ws)
        y0, y1 = min(w[1] for w in ws), max(w[3] for w in ws)
        size = max(4, 0.8 * (y1 - y0))
        natural = pymupdf.get_text_length(text, fontname="helv", fontsize=size)
        base = pymupdf.Point(x0, y1 - 0.2 * (y1 - y0))
        page.insert_text(base, text, fontname=OCR_FONT, fontsize=size, render_mode=3,
                         morph=(base, pymupdf.Matrix((x1 - x0) / natural if natural else 1, 1)))
    return len(words)


def _largest_text_line(page) -> str | None:
    best, size = None, 0
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = " ".join(s["text"] for s in line["spans"]).strip()
            s = max((sp["size"] for sp in line["spans"]), default=0)
            if len(text) >= 4 and s > size:
                best, size = text, s
    return best


def fix_accessibility(file_id: str, title: str | None = None, language: str | None = None,
                      add_text_layer: bool = True) -> dict:
    if language and not LANG_TAG.match(language):
        raise ToolError(f"language must be a code like 'en', 'hi' or 'en-IN', got '{language}'")
    meta, doc = open_pdf(file_id)
    with doc:
        before = {c["id"]: c["status"] for c in run_checks(doc)}
        done = []

        scans = scanned_pages(doc) if add_text_layer else []
        if scans:
            words = sum(_add_text_layer(doc[p - 1]) for p in scans)
            done.append(f"added an OCR text layer to pages {page_ranges(scans)} ({words} words)")

        if not language:
            sample = " ".join(doc[i].get_text("text") for i in range(min(5, doc.page_count)))
            found = detect_language(sample)
            if not found:
                raise ToolError("could not detect the language (too little text); pass language, e.g. 'en'")
            language = found["code"]
            done.append(f"declared the language as '{language}' ({found['name']}, detected: {found['basis']})")
        else:
            done.append(f"declared the language as '{language}' (given)")
        doc.xref_set_key(doc.pdf_catalog(), "Lang", pymupdf.get_pdf_str(language))

        md = {k: v for k, v in (doc.metadata or {}).items() if k not in ("format", "encryption") and v}
        if title or not md.get("title"):
            title = title or (_largest_text_line(doc[0]) if doc.page_count else None)
            if not title:
                raise ToolError("could not find a title on page 1; pass title")
            md["title"] = title
            doc.set_metadata(md)
            done.append(f"set the title to '{title}'")
        if catalog_key(doc, "ViewerPreferences/DisplayDocTitle") != ("bool", "true"):
            doc.xref_set_key(doc.pdf_catalog(), "ViewerPreferences/DisplayDocTitle", "true")
            done.append("told viewers to show the title instead of the file name")

        doc.subset_fonts()
        new = save_new(doc, derived_name(meta["name"], "accessible"), [file_id], "fix_accessibility",
                       page_origin=origin_of(meta))

    _, check = open_pdf(new["id"])
    with check:
        after = {c["id"]: c for c in run_checks(check)}
        if scans and any(not check[p - 1].get_text().strip() for p in scans):
            raise ToolError("verification failed: a scanned page still has no text after OCR")
    improved = [after[k]["check"] for k, s in before.items() if s in ("fail", "warn") and after[k]["status"] == "pass"]
    remaining = [{"check": c["check"], "why": c.get("fix", c["detail"])}
                 for c in after.values() if c["status"] in ("fail", "warn")]
    return {"new_file_id": new["id"], "changes": done, "now_passing": improved,
            "still_failing": remaining, "verified": "re-ran every check on the new file",
            "files_created": [new]}
