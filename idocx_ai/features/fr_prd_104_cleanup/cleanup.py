"""FR-PRD-104 One-click Document Cleanup: blank pages, duplicate pages, metadata.

Blank and duplicate detection judge the RENDERED page, not just the text layer:
a page holding only a vector chart has no text but is not blank, and a scanned
blank page has an image but nothing on it.
"""

import difflib
import hashlib
import io
import re

import pymupdf
from PIL import Image

from core.pages import pages_from_original
from core.pdfutil import (
    STANDARD_INFO_KEYS,
    ToolError,
    check_pages,
    derived_name,
    info_keys,
    open_pdf,
    origin_of,
    page_ranges,
    render_gray,
    save_new,
)

DPI = 60
BORDER = 0.03        # ignore the outer 3%: scanner edge shadows live there
BAND = 0.08          # top/bottom 8% counts as header/footer
INK_DELTA = 60       # a pixel this much darker than the paper is ink
BLANK_INK = 0.0003   # below 0.03% inked pixels, nothing is visibly on the page
NEARLY_EMPTY_LINES = 2     # a page with this few lines of text is shown to the user, never auto-removed
NEARLY_EMPTY_INK = 0.005   # a scan under 0.5% inked is nearly empty (scanned text pages measure ~1.7-1.9%)
SIMILAR_THUMB = 6.0  # mean abs grey difference (0-255) between page thumbnails
SIMILAR_TEXT_MIN = 0.3   # difflib ratio above which two different pages are worth flagging as "similar"
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
    score = round(100 - min(m["ink"], 1) * 100, 2)   # 100 = nothing visibly inked, 0 = a full page of ink

    if not text:
        if m["ink"] < BLANK_INK:
            if has_images:
                return {"reason": "scanned page with nothing on it", "needs_confirmation": False, "blank_score": score}
            if raw:
                return {"reason": "text layer holds only whitespace", "needs_confirmation": False, "blank_score": score}
            return {"reason": "empty page", "needs_confirmation": False, "blank_score": score}
        return None

    h = page.rect.height
    body = [w[4] for w in page.get_text("words")
            if BAND * h < (w[1] + w[3]) / 2 < (1 - BAND) * h]
    if not body and m["body_ink"] < BLANK_INK:
        body_score = round(100 - min(m["body_ink"], 1) * 100, 2)
        return {"reason": "only a header and/or footer", "needs_confirmation": True, "blank_score": body_score}
    if not has_images and NOTICE.match(_norm(" ".join(body))):
        return {"reason": "'intentionally left blank' notice only", "needs_confirmation": False, "blank_score": score}
    return None


def _nearly_empty(page, m: dict) -> tuple[str, float] | None:
    """Not blank, but so sparse the user may want it gone. Only ever a question:
    one line can be a sign-off, a signature or a notice. Returns (reason, blank_score)."""
    score = round(100 - min(m["ink"], 1) * 100, 2)
    if not page.get_text("text").strip():
        if page.get_images() and m["ink"] < NEARLY_EMPTY_INK:
            return f"nearly empty scan: only a small mark ({m['ink'] * 100:.1f}% of the page inked)", score
        return None
    if page.get_images() or page.get_drawings():
        return None
    h = page.rect.height
    body = [w for w in page.get_text("words") if BAND * h < (w[1] + w[3]) / 2 < (1 - BAND) * h]
    lines = {(w[5], w[6]) for w in body}
    if 0 < len(lines) <= NEARLY_EMPTY_LINES:
        snippet = " ".join(w[4] for w in body)
        snippet = snippet if len(snippet) <= 80 else snippet[:77] + "..."
        return f'nearly empty: {len(lines)} line{"s" if len(lines) > 1 else ""} of text: "{snippet}"', score
    return None


def _blank_pages(doc) -> tuple[list[dict], list[dict], list[dict]]:
    found, sparse, measures = [], [], []
    for i, page in enumerate(doc):
        m = _measure(page)
        measures.append(m)
        c = _classify_blank(page, m)
        if c:
            found.append({"page": i + 1, **c})
        elif r := _nearly_empty(page, m):
            reason, score = r
            sparse.append({"page": i + 1, "reason": reason, "needs_confirmation": True, "blank_score": score})
    return found, sparse, measures


def _size_estimate(meta: dict, doc, candidate_pages: int) -> int:
    """A page-count proportion of the file's size: good enough to tell the user roughly what
    removing these pages would save, without re-encoding the file just to find out."""
    if not doc.page_count:
        return 0
    return round(meta["size"] * candidate_pages / doc.page_count)


def find_blank_pages(file_id: str) -> dict:
    meta, doc = open_pdf(file_id)
    with doc:
        found, sparse, _ = _blank_pages(doc)
        sure = [b["page"] for b in found if not b["needs_confirmation"]]
        ask = [b["page"] for b in found if b["needs_confirmation"]] + [s["page"] for s in sparse]
        return {"file_id": file_id, "pages": doc.page_count, "blank_pages": found,
                "nearly_empty_pages": sparse,
                "safe_to_remove": page_ranges(sure), "ask_user_first": page_ranges(ask),
                "estimated_size_saved_if_all_removed_kb": round(
                    _size_estimate(meta, doc, len(found) + len(sparse)) / 1024, 1),
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
        found, similar = [], []
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
                    ratio = difflib.SequenceMatcher(None, p["text"], q["text"]).ratio()
                    if ratio >= SIMILAR_TEXT_MIN and not any(s["page"] == p["n"] for s in similar):
                        similar.append({"page": p["n"], "like_page": q["n"], "match_percent": round(ratio * 100, 1),
                                        "note": "similar layout and some shared text, but not the same content"})
                    continue
                # Both scans with no text layer and similar layout: confirm with OCR,
                # because two different text pages also look alike at thumbnail size.
                for x in (p, q):
                    if x["n"] not in ocr:
                        ocr[x["n"]] = _ocr_text(x["page"])
                a, b = ocr[p["n"]], ocr[q["n"]]
                if not (a and b):
                    continue
                ratio = difflib.SequenceMatcher(None, a, b).ratio()
                if ratio >= SAME_OCR_TEXT:
                    found.append({"page": p["n"], "duplicate_of": q["n"],
                                  "how": "re-scan of the same page (matched by OCR)",
                                  "needs_confirmation": True})
                    break
                if ratio >= SIMILAR_TEXT_MIN and not any(s["page"] == p["n"] for s in similar):
                    similar.append({"page": p["n"], "like_page": q["n"], "match_percent": round(ratio * 100, 1),
                                    "note": "similar layout, read by OCR, but not a close enough text match"})

        # A page checked as "similar" to an earlier page before later matching a different page as an
        # exact duplicate is a confirmed duplicate, not merely similar: drop it from this list.
        similar = [s for s in similar if not any(f["page"] == s["page"] for f in found)]
        sure = [f["page"] for f in found if not f["needs_confirmation"]]
        ask = [f["page"] for f in found if f["needs_confirmation"]]
        return {"file_id": file_id, "pages": doc.page_count, "duplicate_pages": found, "similar_pages": similar,
                "safe_to_remove": page_ranges(sure), "ask_user_first": page_ranges(ask),
                "estimated_size_saved_if_all_removed_kb": round(_size_estimate(meta, doc, len(found)) / 1024, 1),
                "note": "blank pages are excluded here; see find_blank_pages. similar_pages are NOT duplicates "
                       "and are never removed automatically.",
                "next_step": NEXT_STEP}


METADATA_KEY = re.compile(r"/(Author|Creator|Producer|Subject|Keywords|CreationDate|ModDate)\b")
# Which category a standard /Info field belongs to, for the categorised report the brief asks for
# (GPS, device, software, author): PDF /Info itself never carries GPS -- that only ever shows up
# inside an embedded photo's EXIF, found separately by _image_exif below.
METADATA_CATEGORY = {"author": "author", "creator": "device_or_software", "producer": "device_or_software",
                     "subject": "other", "keywords": "other", "creationDate": "other", "modDate": "other"}
GPS_IFD, MAKE_TAG, MODEL_TAG = 0x8825, 0x010F, 0x0110


def _image_exif(doc) -> dict[int, dict]:
    """GPS location and camera/scanner make+model baked into an embedded photo's EXIF, by image xref."""
    found: dict[int, dict] = {}
    for page in doc:
        for xref, *_ in page.get_images(full=True):
            if xref in found:
                continue
            raw = doc.extract_image(xref)
            if raw.get("ext") not in ("jpg", "jpeg"):
                continue
            try:
                exif = Image.open(io.BytesIO(raw["image"])).getexif()
            except OSError:                             # not a readable JPEG: nothing to find
                continue
            gps = exif.get_ifd(GPS_IFD) if GPS_IFD in exif else {}
            make, model = exif.get(MAKE_TAG), exif.get(MODEL_TAG)
            if gps or make or model:
                found[xref] = {"gps": bool(gps), "make": make, "model": model}
    return found


def _strip_image_exif(doc, exif: dict[int, dict]) -> None:
    """Re-encode each flagged image without its EXIF block (Pillow drops it by default on save).
    Page.replace_image needs a page the image appears on; any one of them will do."""
    pending = set(exif)
    for page in doc:
        if not pending:
            break
        for xref, *_ in page.get_images(full=True):
            if xref in pending:
                raw = doc.extract_image(xref)
                buf = io.BytesIO()
                Image.open(io.BytesIO(raw["image"])).convert("RGB").save(buf, "JPEG", quality=90)
                page.replace_image(xref, stream=buf.getvalue())
                pending.discard(xref)


def _strip(doc, keep_title: bool) -> tuple[list[str], str | None, dict[str, list[str]]]:
    """Strip metadata (Info dict, XMP, embedded-photo EXIF) in place.
    Returns (what was removed as a flat list, the title kept, the same removals by category)."""
    before = {k: v for k, v in (doc.metadata or {}).items() if v and k != "format"}
    custom = [k for k in info_keys(doc) if k not in STANDARD_INFO_KEYS]
    had_xmp = bool(doc.get_xml_metadata())
    exif = _image_exif(doc)
    title = before.get("title") if keep_title else None
    cat_info = doc.xref_get_key(doc.pdf_catalog(), "Info")[0] != "null"

    by_category: dict[str, list[str]] = {}
    for k in sorted(set(before) - ({"title"} if title else set())):
        by_category.setdefault(METADATA_CATEGORY.get(k, "other"), []).append(k)
    if custom:
        by_category.setdefault("other", []).extend(f"{k} (custom)" for k in custom)
    if had_xmp:
        by_category.setdefault("other", []).append("XMP metadata")
    if any(e["gps"] for e in exif.values()):
        by_category["gps"] = [f"GPS location in {sum(1 for e in exif.values() if e['gps'])} image(s)"]
    if any(e["make"] or e["model"] for e in exif.values()):
        by_category.setdefault("device_or_software", []).extend(
            f"image EXIF: {e['make'] or ''} {e['model'] or ''}".strip() for e in exif.values()
            if e["make"] or e["model"])
    removed = [item for items in by_category.values() for item in items]

    if not removed and not cat_info:
        return [], title, {}
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
    if exif:
        _strip_image_exif(doc, exif)
    return removed or ["MuPDF producer stamp"], title, by_category


def _verify_no_metadata(file_id: str) -> None:
    _, check = open_pdf(file_id)
    with check:
        left = {k: v for k, v in (check.metadata or {}).items() if v and k not in ("format", "title")}
        leaked = [x for x in range(1, check.xref_length())
                  if METADATA_KEY.search(check.xref_object(x, compressed=True) or "")]
        exif_left = _image_exif(check)
        if left or check.get_xml_metadata() or leaked or exif_left:
            raise ToolError(f"verification failed: metadata still present {left or ''} "
                            f"{'XMP' if check.get_xml_metadata() else ''} objects {leaked} "
                            f"{'image EXIF ' + str(list(exif_left)) if exif_left else ''}")


def strip_metadata(file_id: str, keep_title: bool = True) -> dict:
    meta, doc = open_pdf(file_id)
    with doc:
        removed, title, by_category = _strip(doc, keep_title)
        if not removed:
            return {"nothing_to_remove": True, "kept": ["title"] if title else [],
                    "note": "no metadata to strip; no new file was written"}
        out = save_new(doc, derived_name(meta["name"], "no-metadata"), [file_id], "strip_metadata",
                       page_origin=origin_of(meta))
    _verify_no_metadata(out["id"])
    return {"new_file_id": out["id"], "removed": removed, "removed_by_category": by_category,
            "kept": ["title"] if title else [],
            "size_before_kb": round(meta["size"] / 1024, 1), "size_after_kb": round(out["size"] / 1024, 1),
            "bytes_saved": meta["size"] - out["size"],
            "verified": "reopened the new file: no metadata fields, custom fields, XMP or image EXIF left",
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
        removed_md, _, removed_md_by_category = _strip(doc, keep_title) if strip_metadata else ([], None, {})
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
              "metadata_removed_by_category": removed_md_by_category,
              "size_before_kb": round(meta["size"] / 1024, 1), "size_after_kb": round(out["size"] / 1024, 1),
              "bytes_saved": meta["size"] - out["size"],
              **findings, "verified": "page count and metadata checked on the new file", "files_created": [out]}
    if ask:
        result["next_step"] = ("first finish every other step the user asked for (e.g. compressing) on this new file; "
                               "then, at the end, ask about each ask_user_first page. Remove confirmed ones with "
                               "clean_pdf(remove_pages=[...], numbering='original') on the latest file.")
    return result
