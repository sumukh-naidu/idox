"""FR-AI-04 translation with layout preservation: English -> Hindi or Spanish, PDF in, PDF out.

Each text block is translated and written back into the SAME box: the original text is
removed (images and table lines stay), the translation is laid out in its place and
shrunk to fit when it runs longer. Blocks with no letters (amounts, dates alone) are left
untouched. Every number, date digit, email, URL and code in a block must survive its
translation; a block that loses one is retried once, then flagged.

Hindi is shaped by MuPDF (HarfBuzz) with its built-in Noto Devanagari font, so it LOOKS
right; but copying or searching Devanagari in the output is unreliable (joined letters
extract as stray symbols), a common PDF limitation for Indic scripts. Latin text, digits,
emails and IDs inside it extract correctly, which is what the checks rely on.
"""

import html
import json
import re

import pymupdf

from core import llm, store
from core.text import textpage as read_textpage
from core.pdfutil import ToolError, derived_name, open_pdf, origin_of, page_ranges, parse_page_spec, save_new

LANGUAGES = {"hi": "Hindi", "es": "Spanish"}
PROTECTED = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+|https?://\S+|\b(?=[A-Z0-9\-/]*\d)[A-Z0-9][A-Z0-9\-/]{3,}\b"
                       r"|\b[A-Z]{2,}\b|\d[\d,]*(?:\.\d+)?%?")   # emails, URLs, codes, ACRONYMS, numbers

PROMPT = ("Translate each text block below from English to {lang}. It is a business or legal document: keep the "
          "meaning exact and the tone formal. Keep every number, amount, percentage, email address, web address, "
          "ID or code, abbreviation in capitals (such as GST, INR, PAN) and the names of people and companies "
          "exactly as written, in Latin letters and Western digits; do not translate, localise or transliterate "
          "them. Translate month names and other words. Return exactly one translation per block, in the same "
          "order.{extra}\n\nBlocks (JSON):\n{blocks}")


def language_code(language: str) -> str:
    key = str(language).strip().lower()
    for code, name in LANGUAGES.items():
        if key in (code, name.lower()):
            return code
    raise ToolError(f"language must be one of {', '.join(f'{n} ({c})' for c, n in LANGUAGES.items())}")


def protected(text: str) -> list[str]:
    return sorted(set(m.group(0).rstrip(".,") for m in PROTECTED.finditer(text)))


def present(value: str, text: str) -> bool:
    """A number must appear as a whole number: "1" inside "1.5%" does not count."""
    if re.fullmatch(r"[\d,.%]+", value):
        return re.search(rf"(?<![\d.,]){re.escape(value)}(?![\d])", text) is not None
    return value in text


def model_translate(texts: list[str], lang: str, keep: list[str] | None = None) -> list[str]:
    n = len(texts)
    schema = {"type": "object", "properties": {"translations": {"type": "array", "items": {"type": "string"},
                                                                "minItems": n, "maxItems": n}},
              "required": ["translations"]}
    r = llm.chat([{"role": "user", "content": PROMPT.format(lang=LANGUAGES[lang], extra=(f" These values MUST appear unchanged in the "
                                                                      f"translation: {', '.join(keep)}." if keep else ""),
                                                           blocks=json.dumps(texts,
                                                                                               ensure_ascii=False))}],
                 temperature=0, max_tokens=300 + 6 * sum(len(t) for t in texts),
                 response_format={"type": "json_schema", "json_schema": {"name": "t", "schema": schema}})
    out = json.loads(r["choices"][0]["message"].get("content") or "{}").get("translations", [])
    if len(out) != n:
        raise ToolError(f"the model returned {len(out)} translations for {n} blocks")
    return out


# ------------------------------------------------------------------ reading the layout

COLUMN_GAP = 1.5     # a gap wider than this many font sizes between two pieces of a line separates cells


def _segments(line) -> list[list[dict]]:
    """Split a line where a column-sized gap separates its spans (table cells on one baseline)."""
    spans = [s for s in line["spans"] if s["text"].strip()]
    segs = []
    for s in spans:
        if segs and s["bbox"][0] - segs[-1][-1]["bbox"][2] <= COLUMN_GAP * s["size"]:
            segs[-1].append(s)
        else:
            segs.append([s])
    return segs


def _unit(spans: list[dict], text: str) -> dict:
    main = max(spans, key=lambda s: len(s["text"]))
    rect = pymupdf.Rect(spans[0]["bbox"])
    for s in spans[1:]:
        rect |= pymupdf.Rect(s["bbox"])
    text = re.sub(r"\s+", " ", text).strip()
    return {"rect": rect, "text": text, "size": round(main["size"], 1),
            "bold": bool(main["flags"] & 16) or "bold" in main["font"].lower(),
            "serif": bool(main["flags"] & 4) or any(k in main["font"].lower() for k in ("times", "serif")),
            "color": f"#{main['color']:06x}", "translate": bool(re.search(r"[A-Za-z]{2,}", text))}


def _blocks(page, tp=None) -> list[dict]:
    """Units to translate: a paragraph block as one unit; a block whose lines hold several
    cells (a table row extracted as one block) split into one unit per cell, so the table
    keeps its columns and number-only cells stay untouched. tp: an OCR text page for scans."""
    out = []
    for b in page.get_text("dict", textpage=tp)["blocks"]:
        if b["type"] != 0:
            continue
        raw = [line for line in b["lines"] if any(s["text"].strip() for s in line["spans"])]
        lines = [_segments(line) for line in raw]
        if not lines:
            continue
        # Lines side by side (overlapping vertically) are table cells, not a paragraph's stacked lines.
        side_by_side = any(min(a["bbox"][3], c["bbox"][3]) - max(a["bbox"][1], c["bbox"][1])
                           > 0.5 * (a["bbox"][3] - a["bbox"][1]) for a, c in zip(raw, raw[1:]))
        if all(len(segs) == 1 for segs in lines) and not side_by_side:
            spans = [s for segs in lines for s in segs[0]]
            out.append(_unit(spans, " ".join(" ".join(s["text"] for s in segs[0]) for segs in lines)))
        else:
            for segs in lines:
                for seg in segs:
                    out.append(_unit(seg, " ".join(s["text"] for s in seg)))
    return out


def _obstacles(page, blocks: list[dict]) -> list:
    rects = [b["rect"] for b in blocks]
    for xref, *_ in page.get_images(full=True):
        rects += page.get_image_rects(xref)
    rects += [d["rect"] for d in page.get_drawings()]
    return rects


def _room(block: dict, obstacles: list, page) -> pymupdf.Rect:
    """The space a longer translation may use at full size: right to the next thing on the same
    rows (or the margin), down to the next thing below (or the bottom margin)."""
    r, margin = block["rect"], 36
    others = [o for o in obstacles if o != r and not (o.contains(r) and o.width > r.width + 20)]
    right = [o.x0 for o in others if o.x0 >= r.x1 - 1 and o.y0 < r.y1 and o.y1 > r.y0]
    x1 = max(r.x1, min(right, default=page.rect.x1 - margin) - 4)
    below = [o.y0 for o in others if o.y0 >= r.y1 - 1 and o.x0 < x1 and o.x1 > r.x0]
    y1 = max(r.y1, min(below, default=page.rect.y1 - 30) - 3)
    return pymupdf.Rect(r.x0, r.y0, x1, y1)


def _place(page, room, block, text) -> float:
    style = (f"font-family: {'serif' if block['serif'] else 'sans-serif'}; font-size: {block['size']}px; "
             f"color: {block['color']}; font-weight: {'bold' if block['bold'] else 'normal'}; margin: 0;")
    body = f'<p style="{style}">{html.escape(text)}</p>'
    spare, scale = page.insert_htmlbox(room, body, scale_low=1)      # original size if it fits
    if spare < 0:
        spare, scale = page.insert_htmlbox(room, body, scale_low=0)  # otherwise shrink to fit
    return scale


# ------------------------------------------------------------------ the tool

def _reading_order(units: list[dict]) -> list[dict]:
    return sorted(units, key=lambda u: (round(u["rect"].y0 / 6), u["rect"].x0))


def translate_document(file_id: str, language: str, pages: str | None = None, translator=None) -> dict:
    lang = language_code(language)
    translator = translator or model_translate
    meta, doc = open_pdf(file_id)
    with doc:
        wanted = parse_page_spec(pages, doc.page_count) if pages else list(range(1, doc.page_count + 1))
        flagged, shrunk, done, kept, scans, placed, text_copy = [], [], 0, 0, [], [], []
        for pno in wanted:
            page = doc[pno - 1]
            tp = None
            if not page.get_text("text").strip() and page.get_images():
                tp, _ = read_textpage(page)                # a scan: read it by OCR
                scans.append(pno)
            blocks = _blocks(page, tp)
            if not blocks:
                continue
            todo = [b for b in blocks if b["translate"]]
            kept += len(blocks) - len(todo)
            if todo:
                texts = translator([b["text"] for b in todo], lang)
                for b, t in zip(todo, texts):
                    missing = [v for v in protected(b["text"]) if not present(v, t)]
                    if missing:                    # one retry, naming the values that must appear
                        # The reminder goes in the instructions: put inside the text, it got translated as content.
                        t = translator([b["text"]], lang, keep=missing)[0]
                        missing = [v for v in protected(b["text"]) if not present(v, t)]
                        if missing:
                            flagged.append({"page": pno, "block": b["text"][:60], "missing": missing})
                    b["translation"] = t
            text_copy.append((pno, [b.get("translation", b["text"]) for b in _reading_order(blocks)]))

            if pno in scans:
                # A scan becomes a clean page: the image goes, and every unit (translated or a
                # number-only one kept as is) is written where its text was, read by OCR.
                place = blocks
                obstacles = [b["rect"] for b in blocks]
                page.add_redact_annot(page.rect)
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
                                      graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                                      text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            else:
                place = todo
                obstacles = _obstacles(page, blocks)
                for b in todo:
                    page.add_redact_annot(b["rect"])
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                      graphics=pymupdf.PDF_REDACT_LINE_ART_NONE, text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            rooms = [_room(b, obstacles, page) for b in place]       # measured from the original layout
            for b, room in zip(place, rooms):
                scale = _place(page, room, b, b.get("translation", b["text"]))
                if b["translate"]:
                    done += 1
                    placed.append((pno, b))
                if scale < 0.8:
                    shrunk.append({"page": pno, "block": b["text"][:40], "scale": round(scale, 2)})
        if not done:
            raise ToolError("no text found to translate")
        doc.xref_set_key(doc.pdf_catalog(), "Lang", pymupdf.get_pdf_str(lang))
        new = save_new(doc, derived_name(meta["name"], lang), [file_id], "translate_document",
                       page_origin=origin_of(meta))

    # Verify the new file: same pages, no original English left, protected values present.
    _, check = open_pdf(new["id"])
    with check:
        if check.page_count != meta["pages"]:
            raise ToolError(f"verification failed: {check.page_count} pages, expected {meta['pages']}")
        page_text = {p: " ".join(check[p - 1].get_text().split()) for p in wanted}
    already_flagged = {(f["page"], f["block"]) for f in flagged}
    problems = []
    for pno, b in placed:
        # A translation may legitimately repeat its source (a heading that is just a name);
        # only an original that survives WITHOUT being in its translation means removal failed.
        if b["text"] in page_text[pno] and b["text"] not in b["translation"]:
            problems.append(f"p{pno}: original text still present: {b['text'][:40]!r}")
        if (pno, b["text"][:60]) not in already_flagged:
            lost = [v for v in protected(b["text"]) if not present(v, page_text[pno])]
            if lost:
                problems.append(f"p{pno}: {lost} missing from the output")
    if problems:
        raise ToolError("verification failed: " + "; ".join(problems[:5]))

    # The same translation as plain text: unlike Devanagari in a PDF, it copies and searches correctly.
    txt = "\n\n".join(f"--- Page {p} ---\n\n" + "\n\n".join(units) for p, units in text_copy)
    copy = store.save_file(txt.encode("utf-8"), derived_name(meta["name"], lang).replace(".pdf", ".txt"),
                           pages=0, source="translate_document", parents=[file_id], ext="txt")

    result = {"new_file_id": new["id"], "text_copy_file_id": copy["id"], "language": LANGUAGES[lang],
              "pages_translated": page_ranges(wanted), "blocks_translated": done, "blocks_kept_as_is": kept,
              "values_checked": "every number, date digit, email, URL, code and capitalised abbreviation in each "
                                "block was checked in its translation",
              "files_created": [new, copy]}
    if flagged:
        result["check_these"] = flagged
    if shrunk:
        result["shrunk_to_fit"] = shrunk
    if scans:
        result["scanned_pages"] = (f"pages {page_ranges(scans)} were scans: their text was read by OCR and the "
                                   f"translation placed on clean pages (the scanned image is not included); check "
                                   f"names and numbers against the original")
    if lang == "hi":
        result["note"] = ("Hindi displays correctly in the PDF, but copying or searching Hindi there is unreliable "
                          "(a common PDF limitation for Devanagari); use the text copy for that.")
    return result
