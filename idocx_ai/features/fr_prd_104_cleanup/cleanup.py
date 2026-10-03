"""FR-PRD-104 One-click Document Cleanup: blank pages, duplicate pages, metadata.

Blank and duplicate detection judge the RENDERED page, not just the text layer:
a page holding only a vector chart has no text but is not blank, and a scanned
blank page has an image but nothing on it.
"""

import difflib
import hashlib
import re

import pymupdf

from core.pages import pages_from_original
from core.pdfutil import (STANDARD_INFO_KEYS, ToolError, check_pages, derived_name, info_keys, open_pdf,
                          origin_of, page_ranges, render_gray, save_new)

DPI = 60
BORDER = 0.03        # ignore the outer 3%: scanner edge shadows live there
BAND = 0.08          # top/bottom 8% counts as header/footer
INK_DELTA = 60       # a pixel this much darker than the paper is ink
BLANK_INK = 0.0003   # below 0.03% inked pixels, nothing is visibly on the page
NEARLY_EMPTY_LINES = 2     # a page with this few lines of text is shown to the user, never auto-removed
NEARLY_EMPTY_INK = 0.005   # a scan under 0.5% inked is nearly empty (scanned text pages measure ~1.7-1.9%)
SIMILAR_THUMB = 6.0  # mean abs grey difference (0-255) between page thumbnails
# In the result, not only the prompt: without it the model listed safe pages and then waited.
NEXT_STEP = ("to remove them, call clean_pdf with remove_blank / remove_duplicates (and strip_metadata if asked): it removes only the safe pages and reports the ones to ask about")
SAME_OCR_TEXT = 0.90 # difflib ratio for two scans to count as the same page

NOTICE = re.compile(r"^(this\s+)?page\s+(is\s+|has\s+been\s+)?(intentionally|deliberately)?\s*(left\s+)?blank\.?$")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _measure(page) -> dict:
    """Ink fractions for the whole page (inside the border) and for the body
    (between the header and footer bands)."""
    im = render_gray(page, DPI)
    w, h = im.size
    im = im.crop((int(w * BORDER), int(h * BORDER), int(w * (1 - BORDER)), int(h * (1 - BORDER))))
    w, h = im.size
    hist = im.histogram()
    half, seen, paper = w * h / 2, 0, 255
    for v in range(255, -1, -1):          # median grey = the paper colour
        seen += hist[v]
        if seen >= half:
            paper = v
            break
    cut = max(0, paper - INK_DELTA)
    px = im.load()
    top, bottom = int(h * (BAND - BORDER)), int(h * (1 - BAND + BORDER))
    ink_all = ink_body = 0
    for y in range(h):
        row_ink = sum(1 for x in range(w) if px[x, y] < cut)
        ink_all += row_ink
        if top <= y < bottom:
            ink_body += row_ink
    return {"ink": ink_all / (w * h), "body_ink": ink_body / (w * max(1, bottom - top)), "image": im}


def _classify_blank(page, m: dict) -> dict | None:
    raw = page.get_text("text")
    text = raw.strip()
    has_images = bool(page.get_images())

    if not text:
        if m["ink"] < BLANK_INK:
            if has_images:
                return {"reason": "scanned page with nothing on it", "needs_confirmation": False}
            if raw:
                return {"reason": "text layer holds only whitespace", "needs_confirmation": False}
            return {"reason": "empty page", "needs_confirmation": False}
        return None

    h = page.rect.height
    body = [w[4] for w in page.get_text("words")
            if BAND * h < (w[1] + w[3]) / 2 < (1 - BAND) * h]
    if not body and m["body_ink"] < BLANK_INK:
        return {"reason": "only a header and/or footer", "needs_confirmation": True}
    if not has_images and NOTICE.match(_norm(" ".join(body))):
        return {"reason": "'intentionally left blank' notice only", "needs_confirmation": False}
    return None


def _nearly_empty(page, m: dict) -> str | None:
    """Not blank, but so sparse the user may want it gone. Only ever a question:
    one line can be a sign-off, a signature or a notice."""
    if not page.get_text("text").strip():
        if page.get_images() and m["ink"] < NEARLY_EMPTY_INK:
            return f"nearly empty scan: only a small mark ({m['ink'] * 100:.1f}% of the page inked)"
        return None
    if page.get_images() or page.get_drawings():
        return None
    h = page.rect.height
    body = [w for w in page.get_text("words") if BAND * h < (w[1] + w[3]) / 2 < (1 - BAND) * h]
    lines = {(w[5], w[6]) for w in body}
    if 0 < len(lines) <= NEARLY_EMPTY_LINES:
        snippet = " ".join(w[4] for w in body)
        snippet = snippet if len(snippet) <= 80 else snippet[:77] + "..."
        return f'nearly empty: {len(lines)} line{"s" if len(lines) > 1 else ""} of text: "{snippet}"'
    return None


def _blank_pages(doc) -> tuple[list[dict], list[dict], list[dict]]:
    found, sparse, measures = [], [], []
    for i, page in enumerate(doc):
        m = _measure(page)
        measures.append(m)
        c = _classify_blank(page, m)
        if c:
            found.append({"page": i + 1, **c})
        elif reason := _nearly_empty(page, m):
            sparse.append({"page": i + 1, "reason": reason, "needs_confirmation": True})
    return found, sparse, measures


def find_blank_pages(file_id: str) -> dict:
    meta, doc = open_pdf(file_id)
    with doc:
        found, sparse, _ = _blank_pages(doc)
        sure = [b["page"] for b in found if not b["needs_confirmation"]]
        ask = [b["page"] for b in found if b["needs_confirmation"]] + [s["page"] for s in sparse]
        return {"file_id": file_id, "pages": doc.page_count, "blank_pages": found,
                "nearly_empty_pages": sparse,
                "safe_to_remove": page_ranges(sure), "ask_user_first": page_ranges(ask),
                "next_step": NEXT_STEP}


def _thumb(im):
    return list(im.resize((40, 56)).getdata())


def _thumb_diff(a, b) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _ocr_text(page) -> str:
    tp = page.get_textpage_ocr(dpi=150, full=True, tessdata=pymupdf.get_tessdata())
    return _norm(page.get_text("text", textpage=tp))


def find_duplicate_pages(file_id: str) -> dict:
    meta, doc = open_pdf(file_id)
    with doc:
        blanks, _, measures = _blank_pages(doc)
        blank = {b["page"] for b in blanks}
        pages = []
        for i, page in enumerate(doc):
            n = i + 1
            if n in blank:
                continue
            im = measures[i]["image"]
            pages.append({"n": n, "page": page, "hash": hashlib.sha256(im.tobytes()).hexdigest(),
                          "text": _norm(page.get_text("text")), "thumb": _thumb(im)})

        ocr: dict[int, str] = {}
        found = []
        for j, p in enumerate(pages):
            for q in pages[:j]:
                if any(f["page"] == q["n"] for f in found):
                    continue                      # compare against originals only
                if p["hash"] == q["hash"]:
                    found.append({"page": p["n"], "duplicate_of": q["n"], "how": "identical",
                                  "needs_confirmation": False})
                    break
                if _thumb_diff(p["thumb"], q["thumb"]) > SIMILAR_THUMB:
                    continue
                if p["text"] or q["text"]:
                    if p["text"] == q["text"]:
                        found.append({"page": p["n"], "duplicate_of": q["n"],
                                      "how": "same text and layout", "needs_confirmation": False})
                        break
                    continue
                # Both scans with no text layer and similar layout: confirm with OCR,
                # because two different text pages also look alike at thumbnail size.
                for x in (p, q):
                    if x["n"] not in ocr:
                        ocr[x["n"]] = _ocr_text(x["page"])
                a, b = ocr[p["n"]], ocr[q["n"]]
                if a and b and difflib.SequenceMatcher(None, a, b).ratio() >= SAME_OCR_TEXT:
                    found.append({"page": p["n"], "duplicate_of": q["n"],
                                  "how": "re-scan of the same page (matched by OCR)",
                                  "needs_confirmation": True})
                    break

        sure = [f["page"] for f in found if not f["needs_confirmation"]]
        ask = [f["page"] for f in found if f["needs_confirmation"]]
        return {"file_id": file_id, "pages": doc.page_count, "duplicate_pages": found,
                "safe_to_remove": page_ranges(sure), "ask_user_first": page_ranges(ask),
                "note": "blank pages are excluded here; see find_blank_pages", "next_step": NEXT_STEP}


METADATA_KEY = re.compile(r"/(Author|Creator|Producer|Subject|Keywords|CreationDate|ModDate)\b")


def _strip(doc, keep_title: bool) -> tuple[list[str], str | None]:
    """Strip metadata in place. Returns (what was removed, the title kept)."""
    before = {k: v for k, v in (doc.metadata or {}).items() if v and k != "format"}
    custom = [k for k in info_keys(doc) if k not in STANDARD_INFO_KEYS]
    had_xmp = bool(doc.get_xml_metadata())
    title = before.get("title") if keep_title else None
    cat_info = doc.xref_get_key(doc.pdf_catalog(), "Info")[0] != "null"
    removed = sorted(set(before) - ({"title"} if title else set())) + custom + (["XMP metadata"] if had_xmp else [])
    if not removed and not cat_info:
        return [], title
    doc.del_xml_metadata()
    # Rewrite the Info dictionary outright: setting keys to null leaves "/Author null" behind.
    kind, val = doc.xref_get_key(-1, "Info")
    if kind == "xref":
        doc.update_object(int(val.split()[0]), f"<</Title{pymupdf.get_pdf_str(title)}>>" if title else "<<>>")
    # Documents created by MuPDF carry a non-standard /Info inside the catalog too.
    if cat_info:
        cat = doc.pdf_catalog()
        doc.xref_set_key(cat, "Info", "null")
        doc.update_object(cat, re.sub(r"/Info\s*null", "", doc.xref_object(cat, compressed=True)))
    return removed or ["MuPDF producer stamp"], title


def _verify_no_metadata(file_id: str) -> None:
    _, check = open_pdf(file_id)
    with check:
        left = {k: v for k, v in (check.metadata or {}).items() if v and k not in ("format", "title")}
        leaked = [x for x in range(1, check.xref_length())
                  if METADATA_KEY.search(check.xref_object(x, compressed=True) or "")]
        if left or check.get_xml_metadata() or leaked:
            raise ToolError(f"verification failed: metadata still present {left or ''} "
                            f"{'XMP' if check.get_xml_metadata() else ''} objects {leaked}")


def strip_metadata(file_id: str, keep_title: bool = True) -> dict:
    meta, doc = open_pdf(file_id)
    with doc:
        removed, title = _strip(doc, keep_title)
        if not removed:
            return {"nothing_to_remove": True, "kept": ["title"] if title else [],
                    "note": "no metadata to strip; no new file was written"}
        out = save_new(doc, derived_name(meta["name"], "no-metadata"), [file_id], "strip_metadata",
                       page_origin=origin_of(meta))
    _verify_no_metadata(out["id"])
    return {"new_file_id": out["id"], "removed": removed, "kept": ["title"] if title else [],
            "verified": "reopened the new file: no metadata fields, custom fields or XMP left",
            "files_created": [out]}


def clean_pdf(file_id: str, remove_blank: bool = False, remove_duplicates: bool = False,
              strip_metadata: bool = False, remove_pages: list[int] | None = None,
              numbering: str = "this_file", keep_title: bool = True) -> dict:
    """One-click cleanup: detect, remove the safe pages, strip metadata, ONE new file.

    Detection runs inside, so what the reply says was checked is what the code ran: given
    separate find/apply tools, the model once skipped the finders and still reported
    "no blank pages found". One call also avoids two half-cleaned files from parallel calls.
    """
    if not (remove_blank or remove_duplicates or strip_metadata or remove_pages):
        raise ToolError("nothing to do: set remove_blank, remove_duplicates, strip_metadata or remove_pages")
    removed, ask = [], []
    if remove_blank:
        b = find_blank_pages(file_id)
        removed += [(x["page"], x["reason"]) for x in b["blank_pages"] if not x["needs_confirmation"]]
        ask += [(x["page"], x["reason"]) for x in b["blank_pages"] + b["nearly_empty_pages"] if x["needs_confirmation"]]
    if remove_duplicates:
        d = find_duplicate_pages(file_id)
        for x in d["duplicate_pages"]:
            item = (x["page"], f"{x['how']} of page {x['duplicate_of']}")
            (ask if x["needs_confirmation"] else removed).append(item)
    meta, doc = open_pdf(file_id)
    with doc:
        n, origin = doc.page_count, origin_of(meta)
        given = []
        if remove_pages:
            given = pages_from_original(meta, remove_pages) if numbering == "original" else check_pages(remove_pages, n)
        drop = sorted({p for p, _ in removed} | set(given))
        if len(drop) == n:
            raise ToolError("that would remove every page; nothing would be left")
        removed_md, title = _strip(doc, keep_title) if strip_metadata else ([], None)
        findings = {"ask_user_first": [{"page": p, "reason": r} for p, r in sorted(ask)],
                    "checked": [k for k, on in (("blank pages", remove_blank), ("duplicate pages", remove_duplicates),
                                                ("metadata", strip_metadata)) if on]}
        if not drop and not removed_md:
            return {"nothing_to_do": True, **findings,
                    "note": "nothing safe to remove and no metadata to strip; no new file was written"}
        keep = [i for i in range(n) if i + 1 not in drop]
        if drop:
            doc.select(keep)
        out = save_new(doc, derived_name(meta["name"], "cleaned"), [file_id], "clean_pdf",
                       page_origin=[origin[i] for i in keep])
    if out["pages"] != n - len(drop):
        raise ToolError(f"verification failed: expected {n - len(drop)} pages, got {out['pages']}")
    if removed_md:
        _verify_no_metadata(out["id"])
    reasons = dict(removed)
    # Page numbers of the file given. A merged file's "original" page numbers would point into
    # different uploads, so they are only added when the file has a single source.
    single_source = len({root for root, _ in origin}) == 1
    result = {"new_file_id": out["id"], "pages_before": n, "pages_after": out["pages"],
              "removed_pages": [{"page": p, **({"original_page": origin[p - 1][1]} if single_source else {}),
                                 "reason": reasons.get(p, "asked by the user")} for p in drop],
              "metadata_removed": removed_md or ("nothing to remove" if strip_metadata else "not asked"),
              **findings, "verified": "page count and metadata checked on the new file", "files_created": [out]}
    if ask:
        result["next_step"] = ("first finish every other step the user asked for (e.g. compressing) on this new file; "
                               "then, at the end, ask about each ask_user_first page. Remove confirmed ones with "
                               "clean_pdf(remove_pages=[...], numbering='original') on the latest file.")
    return result
