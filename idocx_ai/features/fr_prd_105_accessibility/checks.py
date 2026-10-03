"""FR-PRD-105 accessibility checks: what a screen reader needs from a PDF.

Based on the basics of PDF/UA and WCAG: tags, a declared language, a title the
viewer shows, embedded fonts, image descriptions, real text instead of scanned
images, a defined reading order, bookmarks in long documents, and permission for
assistive technology. Each check is pass / fail / warn / n/a with a plain reason.
"""

import re

import pymupdf

from core.pdfutil import open_pdf, page_ranges

LONG_DOC_PAGES = 20
SCAN_COVER = 0.5   # an image covering half the page, on a page with no text, is a scan
IMAGE_FIX = ("draft alt text with describe_images (or check_accessibility with describe_images=true); the drafts are suggestions for the author")

LANG_TAG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

LATIN_WORDS = {
    "en": "the and of to in is that for with on are this be as by will must was from".split(),
    "es": "el la de que y en los del las por un para con una es se".split(),
    "fr": "le la les de des et en du un une est que pour dans par".split(),
    "de": "der die und den das ist nicht mit von zu ein eine für auf dem".split(),
    "pt": "o a os as de do da que e em um uma para com não".split(),
}
SCRIPTS = {"hi": (0x0900, 0x097F), "bn": (0x0980, 0x09FF), "pa": (0x0A00, 0x0A7F),
           "gu": (0x0A80, 0x0AFF), "ta": (0x0B80, 0x0BFF), "te": (0x0C00, 0x0C7F),
           "kn": (0x0C80, 0x0CFF), "ml": (0x0D00, 0x0D7F)}
NAMES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese",
         "hi": "Hindi", "bn": "Bengali", "pa": "Punjabi", "gu": "Gujarati", "ta": "Tamil",
         "te": "Telugu", "kn": "Kannada", "ml": "Malayalam"}


def detect_language(text: str) -> dict | None:
    """Indian scripts by Unicode block; Latin-script languages by their commonest words."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 20:
        return None
    for code, (lo, hi) in SCRIPTS.items():
        share = sum(lo <= ord(c) <= hi for c in letters) / len(letters)
        if share > 0.3:
            note = " (Devanagari script: Marathi and Nepali use it too)" if code == "hi" else ""
            return {"code": code, "name": NAMES[code], "basis": f"{share:.0%} of letters in its script{note}"}
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    if not words:
        return None
    scores = {code: sum(w in vocab for w in words) / len(words) for code, vocab in LATIN_WORDS.items()}
    best = max(scores, key=scores.get)
    if scores[best] < 0.03:
        return None
    return {"code": best, "name": NAMES[best], "basis": f"{scores[best]:.0%} of words are common {NAMES[best]} words"}


def catalog_key(doc, key: str):
    return doc.xref_get_key(doc.pdf_catalog(), key)


def is_tagged(doc) -> bool:
    return catalog_key(doc, "StructTreeRoot")[0] != "null" and catalog_key(doc, "MarkInfo/Marked") == ("bool", "true")


def figure_alt_counts(doc) -> tuple[int, int]:
    total = with_alt = 0
    for x in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(x, compressed=True)
        except Exception:
            continue
        if re.search(r"/S\s*/Figure\b", obj):
            total += 1
            with_alt += bool(re.search(r"/Alt\s*[(<]", obj))
    return total, with_alt


def scanned_pages(doc) -> list[int]:
    out = []
    for page in doc:
        if page.get_text("text").strip():
            continue
        area = page.rect.width * page.rect.height
        for xref, *_ in page.get_images(full=True):
            if any(r.width * r.height >= SCAN_COVER * area for r in page.get_image_rects(xref)):
                out.append(page.number + 1)
                break
    return out


def content_images(doc, scans: list[int]) -> list[tuple[int, int, pymupdf.Rect]]:
    """(page, xref, where it is drawn) for images that carry content: not page scans."""
    seen, out = set(), []
    for page in doc:
        if page.number + 1 in scans:
            continue
        for xref, *_ in page.get_images(full=True):
            if xref in seen:
                continue
            seen.add(xref)
            rects = page.get_image_rects(xref)
            if rects:
                out.append((page.number + 1, xref, max(rects, key=lambda r: r.width * r.height)))
    return out


def _check(cid, name, status, detail, fix=None):
    return {"id": cid, "check": name, "status": status, "detail": detail, **({"fix": fix} if fix else {})}


def run_checks(doc) -> list[dict]:
    tagged = is_tagged(doc)
    scans = scanned_pages(doc)
    out = []

    out.append(_check("tagged", "Tagged PDF (structure for screen readers)",
                      "pass" if tagged else "fail",
                      "has a structure tree" if tagged else "no tags: a screen reader sees one undifferentiated stream",
                      None if tagged else "out of scope: needs re-authoring with tags (e.g. export from Word with tags on)"))

    kind, lang = catalog_key(doc, "Lang")
    ok = kind == "string" and bool(LANG_TAG.match(lang))
    out.append(_check("language", "Document language declared", "pass" if ok else "fail",
                      f"declared as '{lang}'" if ok else "no language declared: a screen reader may pronounce it wrongly",
                      None if ok else "fix_accessibility detects and declares it"))

    title = (doc.metadata or {}).get("title", "").strip()
    shown = catalog_key(doc, "ViewerPreferences/DisplayDocTitle") == ("bool", "true")
    out.append(_check("title", "Title set and shown by the viewer",
                      "pass" if title and shown else "warn" if title else "fail",
                      f"'{title}'" + ("" if shown else ", but viewers show the file name instead") if title
                      else "no title",
                      None if title and shown else "fix_accessibility sets it"))

    fonts, missing = set(), set()
    for page in doc:
        for xref, ext, ftype, base, *_ in page.get_fonts(full=True):
            fonts.add(base)
            if ext == "n/a" and ftype != "Type3":
                missing.add(base)
    out.append(_check("fonts_embedded", "All fonts embedded",
                      "n/a" if not fonts else "fail" if missing else "pass",
                      "no fonts" if not fonts else f"not embedded: {', '.join(sorted(missing))}" if missing
                      else f"{len(fonts)} font(s), all embedded",
                      "out of scope: re-export the document with fonts embedded" if missing else None))

    images = content_images(doc, scans)
    figs, alts = figure_alt_counts(doc)
    if not images and not figs:
        out.append(_check("image_descriptions", "Images have descriptions (alt text)", "n/a", "no content images"))
    elif not tagged:
        out.append(_check("image_descriptions", "Images have descriptions (alt text)", "fail",
                          f"{len(images)} image(s), none described: an untagged PDF has nowhere to hold alt text",
                          IMAGE_FIX))
    else:
        out.append(_check("image_descriptions", "Images have descriptions (alt text)",
                          "pass" if figs and alts == figs else "fail",
                          f"all {figs} tagged figure(s) have alt text" if alts == figs
                          else f"only {alts} of {figs} tagged figure(s) have alt text",
                          None if figs and alts == figs else IMAGE_FIX))

    out.append(_check("real_text", "Real text, not scanned images", "fail" if scans else "pass",
                      f"pages {page_ranges(scans)} are scanned images with no text layer" if scans
                      else "every page with content has a text layer",
                      "fix_accessibility adds an OCR text layer" if scans else None))

    out.append(_check("reading_order", "Reading order defined",
                      "pass" if tagged else "fail",
                      "follows the tag structure (not checked visually)" if tagged
                      else "no tags, so a screen reader has to guess the order",
                      None if tagged else "out of scope: comes with tagging"))

    if doc.page_count <= LONG_DOC_PAGES:
        out.append(_check("bookmarks", f"Bookmarks (documents over {LONG_DOC_PAGES} pages)", "n/a",
                          f"{doc.page_count} pages: not required"))
    else:
        has = bool(doc.get_toc())
        out.append(_check("bookmarks", f"Bookmarks (documents over {LONG_DOC_PAGES} pages)",
                          "pass" if has else "fail", "present" if has else f"{doc.page_count} pages, no bookmarks"))

    allowed = not doc.is_encrypted or bool(doc.permissions & pymupdf.PDF_PERM_ACCESSIBILITY)
    out.append(_check("assistive_access", "Assistive technology allowed to read it",
                      "pass" if allowed else "fail",
                      "not blocked" if allowed else "security settings block screen readers"))
    return out


def check_accessibility(file_id: str, describe_images: bool = False) -> dict:
    meta, doc = open_pdf(file_id)
    with doc:
        checks = run_checks(doc)
        sample = " ".join(doc[i].get_text("text") for i in range(min(5, doc.page_count)))
        lang = detect_language(sample)
    counts = {s: sum(c["status"] == s for c in checks) for s in ("pass", "fail", "warn", "n/a")}
    result = {"file_id": file_id, "name": meta["name"], "summary": counts, "checks": checks,
              "detected_language": lang or "unknown: no readable text (scanned pages are detected after OCR)"}
    # Drafting alt text is a flag on this first call, decided where the user's words are: as a
    # separate follow-up tool, the model sometimes reported the problem and never drafted.
    images_check = next(c for c in checks if c["id"] == "image_descriptions")
    if describe_images and images_check["status"] == "fail":
        from .images import describe_images as draft      # imported here: images.py imports this module
        result["suggested_alt_text"] = draft(file_id)
    if any(c["status"] in ("fail", "warn") and "fix_accessibility" in c.get("fix", "") for c in checks):
        # Said here rather than only in the system prompt: the model follows a tool result more reliably.
        result["next_step"] = ("if the user asked to improve, fix or make the file more accessible, call "
                               "fix_accessibility now instead of asking: it writes a new file and keeps the original")
    return result
