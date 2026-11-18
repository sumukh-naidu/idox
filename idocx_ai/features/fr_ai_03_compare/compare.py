"""FR-AI-03 comparison. Plain code finds every difference; the model only explains them.

1. Read both versions block by block (text layer, or OCR for scans), dropping the
   running header/footer bands where version labels and page numbers live.
2. Group blocks into clauses when the document numbers them ("3. Payment"), else
   into paragraphs. Clause numbers are left out of the matching key, so removing
   clause 4 does not report every later clause as renumbered.
3. Pair old and new units: identical ones first (ignoring spacing and punctuation),
   then the most similar. Unpaired means added or removed.
4. A pair out of order is "moved": the fewest characters of text are marked moved
   (weighted longest increasing subsequence), the rest keep their place.
5. Changed pairs get a word-level diff and plain "signals" (numbers, obligation words).
"""

import difflib
import re
import uuid

from core import rules
from core.pdfutil import ToolError, open_pdf
from core.text import textpage

from .redline import build_redline

BAND = 0.06            # top/bottom 6% of a page: running headers, footers, page numbers
MIN_CLAUSES = 3        # fewer numbered headings than this: compare paragraph by paragraph
PAIR_RATIO = 0.5       # word similarity for two units to count as one modified unit
MAX_TEXT = 1200        # characters of clause text returned per change (flagged when cut)
MAX_CHANGES = 40
MAX_PAGES = 200        # brief section 4, Q4: 25 MB and 200 pages as a start (TBC)
MAX_BYTES = 25 * 1024 * 1024
TYPE_NAMES = {"added": "Added", "removed": "Removed", "modified": "Modified", "moved": "Modified",
              "moved and modified": "Modified"}   # brief: moved text is marked Modified
CONF_PAIRED_MIN = 60   # a modified pair's confidence runs from this (barely similar) up to 100 (nearly the same)
CONF_WHOLE_UNIT = 92   # a clause only in one version: added or removed
CONF_MOVED = 90
CONF_OCR = 70          # either side read by OCR: some differences may be misreadings

CLAUSE = re.compile(r"^\s*(?:(?:clause|section|article)\s+(\d+(?:\.\d+)*)[.):]?|(\d+(?:\.\d+)*)[.)])\s+(.*)",
                    re.I | re.S)
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
OBLIGATION = {"shall", "must", "may", "not", "never", "only", "will", "required", "prohibited", "unless"}

# comparison_id -> changes, so explanations can be checked against the real list
COMPARISONS: dict[str, dict] = {}


def _clean(text: str) -> str:
    text = re.sub(r"-\n(?=[a-z])", "", text)          # rejoin words hyphenated across lines
    return re.sub(r"\s+", " ", text).strip()


def _blocks(doc, strict: bool = False) -> tuple[list[dict], list[int]]:
    out, ocr = [], []
    for page in doc:
        tp, source = textpage(page)
        if not tp:
            continue
        if source == "OCR":
            ocr.append(page.number + 1)
        h = page.rect.height
        band = 0 if strict else BAND          # strict: headers, footers and page numbers count too
        for x0, y0, x1, y1, text, _, kind in page.get_text("blocks", textpage=tp, sort=True):
            if kind != 0 or y1 < band * h or y0 > (1 - band) * h:
                continue
            t = _clean(text)
            if t:
                out.append({"page": page.number + 1, "rect": (x0, y0, x1, y1), "text": t})
    merged = []                                        # a paragraph broken by a page break
    for b in out:
        prev = merged[-1] if merged else None
        if prev and prev["page"] != b["page"] and not re.search(r"[.:;!?\"')]$", prev["text"]) \
                and b["text"][:1].islower():
            prev["text"] += " " + b["text"]
        else:
            merged.append(dict(b))
    return merged, ocr


def _units(blocks: list[dict], strict: bool = False) -> list[dict]:
    heads = [i for i, b in enumerate(blocks) if CLAUSE.match(b["text"])]
    units = []
    if len(heads) >= MIN_CLAUSES:
        if heads[0] > 0:
            units.append({"label": None, "title": "Opening text", "blocks": blocks[:heads[0]]})
        for k, i in enumerate(heads):
            m = CLAUSE.match(blocks[i]["text"])
            title = " ".join(m.group(3).split()[:6]).rstrip(".:")
            end = heads[k + 1] if k + 1 < len(heads) else len(blocks)
            units.append({"label": m.group(1) or m.group(2), "title": title, "blocks": blocks[i:end],
                          "head_only": end - i > 1 and len(m.group(3).split()) <= 6})
    else:
        units = [{"label": None, "title": None, "blocks": [b]} for b in blocks]
    for u in units:
        u["text"] = " ".join(b["text"] for b in u["blocks"])
        m = CLAUSE.match(u["text"]) if u["label"] else None
        u["body"] = m.group(3) if m else u["text"]
        u["key"] = u["body"].lower()
        # strict: punctuation counts too, only spacing is ignored
        u["letters"] = " ".join(u["key"].split()) if strict else re.sub(r"[\W_]+", "", u["key"])
        u["page"] = u["blocks"][0]["page"]
    return units


def _pair(old: list[dict], new: list[dict]) -> list[tuple[int, int]]:
    pairs, used_o, used_n = [], set(), set()
    by_letters: dict[str, list[int]] = {}
    for j, u in enumerate(new):
        by_letters.setdefault(u["letters"], []).append(j)
    for i, u in enumerate(old):                        # identical apart from spacing/punctuation
        for j in by_letters.get(u["letters"], []):
            if j not in used_n:
                pairs.append((i, j))
                used_o.add(i)
                used_n.add(j)
                break
    candidates = []
    for i, u in enumerate(old):                        # then the most similar
        if i in used_o:
            continue
        for j, v in enumerate(new):
            if j in used_n:
                continue
            sm = difflib.SequenceMatcher(None, u["key"].split(), v["key"].split(), autojunk=False)
            same_title = bool(u["title"]) and u["title"].lower() == (v["title"] or "").lower()
            if not same_title and sm.real_quick_ratio() < PAIR_RATIO:
                continue
            r = max(sm.ratio(), 0.9 if same_title else 0)
            if r >= PAIR_RATIO:
                candidates.append((r, i, j))
    for _, i, j in sorted(candidates, reverse=True):
        if i not in used_o and j not in used_n:
            pairs.append((i, j))
            used_o.add(i)
            used_n.add(j)
    return sorted(pairs)


def _in_place(pairs: list[tuple[int, int]], old: list[dict]) -> set[tuple[int, int]]:
    """Pairs that keep their order: the heaviest increasing run of new positions,
    weighted by text length, so the least text possible is reported as moved."""
    n = len(pairs)
    best, prev = [0.0] * n, [-1] * n
    for k, (i, j) in enumerate(pairs):
        best[k] = len(old[i]["text"])
        for m in range(k):
            if pairs[m][1] < j and best[m] + len(old[i]["text"]) > best[k]:
                best[k], prev[k] = best[m] + len(old[i]["text"]), m
    keep, k = set(), max(range(n), key=best.__getitem__) if n else -1
    while k >= 0:
        keep.add(pairs[k])
        k = prev[k]
    return keep


def _word_diff(a: str, b: str, context: int = 6) -> tuple[str, list[str], list[str]]:
    aw, bw = a.split(), b.split()
    ops = difflib.SequenceMatcher(None, aw, bw, autojunk=False).get_opcodes()
    parts, removed, added = [], [], []
    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            seg = aw[i1:i2]
            first, last = k == 0, k == len(ops) - 1
            if first and len(seg) > context:
                seg = ["…"] + seg[-context:]
            elif last and len(seg) > context:
                seg = seg[:context] + ["…"]
            elif not first and not last and len(seg) > 2 * context:
                seg = seg[:context] + ["…"] + seg[-context:]
            parts.append(" ".join(seg))
            continue
        if i2 > i1:
            removed.append(" ".join(aw[i1:i2]))
            parts.append("[-" + removed[-1] + "-]")
        if j2 > j1:
            added.append(" ".join(bw[j1:j2]))
            parts.append("{+" + added[-1] + "+}")
    return " ".join(parts), removed, added


def _signals(removed: list[str], added: list[str]) -> list[str]:
    out = []
    old_n, new_n = NUMBER.findall(" ".join(removed)), NUMBER.findall(" ".join(added))
    if old_n or new_n:
        out.append(f"numbers changed: {', '.join(old_n) or 'none'} -> {', '.join(new_n) or 'none'}")
    words = {w.lower().strip(".,;:") for w in " ".join(removed + added).split()} & OBLIGATION
    if words:
        out.append(f"obligation wording changed: {', '.join(sorted(words))}")
    return out


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def _name(u: dict) -> str:
    if u["label"]:
        return f"clause {u['label']} ({u['title']})" if u["title"] else f"clause {u['label']}"
    return f"paragraph starting \"{' '.join(u['text'].split()[:8])}…\""


def _place(u: dict, text: str) -> dict:
    """Where a clause sits (page and box in PDF points, top-left origin) and its text, flagged when cut."""
    page = u["blocks"][0]["page"]
    rects = [b["rect"] for b in u["blocks"] if b["page"] == page]
    return {"page": page, "box": {"x0": round(min(r[0] for r in rects), 1), "y0": round(min(r[1] for r in rects), 1),
                                  "x1": round(max(r[2] for r in rects), 1), "y1": round(max(r[3] for r in rects), 1),
                                  "unit": "pt"},
            "text": _clip(text), "truncated": len(text) > MAX_TEXT}


# ------------------------------------------------------------------ language (brief: different languages -> confirm)

_STOP_WORDS = {
    "English": set("the and of to in is that for with shall be this are as by on or".split()),
    "Spanish": set("el la de que y en los las por con para una del se es al".split()),
    "German": set("der die und das nicht mit den von zu ist für auf ein eine sich dem".split()),
}


def detect_language(text: str) -> str | None:
    """English, Hindi, Tamil, Spanish or German, or None when it cannot tell."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 40:
        return None
    for name, lo, hi in (("Hindi", "\u0900", "\u097f"), ("Tamil", "\u0b80", "\u0bff")):
        if sum(lo <= c <= hi for c in letters) / len(letters) > 0.3:
            return name
    words = re.findall(r"[a-zA-ZäöüßñáéíóúÄÖÜ]+", text.lower())
    score = sorted(((sum(w in stop for w in words), name) for name, stop in _STOP_WORDS.items()), reverse=True)
    best, second = score[0], score[1]
    return best[1] if best[0] >= 8 and best[0] >= 1.5 * max(second[0], 1) else None


# ------------------------------------------------------------------ the comparison

def _sentence(counts: dict) -> str:
    n = counts["added"] + counts["removed"] + counts["modified"]
    if not n:
        return "No differences found."
    parts = [f"{counts[k]} {k}" for k in ("added", "removed", "modified") if counts[k]]
    moved = f" ({counts['moved']} of them moved)" if counts["moved"] else ""
    return f"{n} change{'s' if n != 1 else ''}: {', '.join(parts)}{moved}; {counts['unchanged']} unchanged."


def _score(c: dict) -> int:
    """Rough weight of a change, for picking the key ones: figures and obligations matter most."""
    sig = " ".join(c.get("signals", []))
    return (3 if "numbers changed" in sig else 0) + (2 if "obligation" in sig else 0) + \
        (2 if c["type"] in ("added", "removed") else 0) - (1 if c["type"] == "moved" else 0)


def compare_documents(old_file_id: str, new_file_id: str, include_ignored: bool = False,
                      confirm_language_difference: bool = False, explainer=None) -> dict:
    if old_file_id == new_file_id:
        raise ToolError("old_file_id and new_file_id are the same file")
    old_meta, old_doc = open_pdf(old_file_id)
    new_meta, new_doc = open_pdf(new_file_id)
    for a, b in ((old_meta, new_meta), (new_meta, old_meta)):
        if a["source"] == "compare_documents" and b["id"] in a["parents"]:
            old_doc.close()
            new_doc.close()
            raise ToolError(f"'{a['name']}' is the marked-up copy a previous comparison made from '{b['name']}', "
                            f"not another version. Compare the versions the user uploaded.")
    for meta, doc in ((old_meta, old_doc), (new_meta, new_doc)):
        if meta["size"] > MAX_BYTES or doc.page_count > MAX_PAGES:
            old_doc.close()
            new_doc.close()
            raise ToolError(f"'{meta['name']}' is too large to compare: the limit is {MAX_PAGES} pages and "
                            f"{MAX_BYTES // 1024 // 1024} MB (TBC). Compare a page range of each file instead.")
    with old_doc, new_doc:
        ob, old_ocr = _blocks(old_doc, include_ignored)
        nb, new_ocr = _blocks(new_doc, include_ignored)
        lang_old = detect_language(" ".join(b["text"] for b in ob)[:6000])
        lang_new = detect_language(" ".join(b["text"] for b in nb)[:6000])
        if lang_old and lang_new and lang_old != lang_new and not confirm_language_difference:
            return {"status": "confirm_language", "old": old_meta["name"], "new": new_meta["name"],
                    "languages": {"old": lang_old, "new": lang_new}, "changes": [],
                    "warning": f"The documents are in different languages ({lang_old} and {lang_new}). Compare only "
                               f"if the user confirms: ask them, then call again with confirm_language_difference true."}
        old, new = _units(ob, include_ignored), _units(nb, include_ignored)
        pairs = _pair(old, new)
        keep = _in_place(pairs, old)
        paired_o, paired_n = {i for i, _ in pairs}, {j for _, j in pairs}

        changes, unchanged, renumbered = [], 0, 0
        for i, j in pairs:
            o, n = old[i], new[j]
            moved = (i, j) not in keep
            reworded = o["letters"] != n["letters"]
            if not moved and not reworded:
                unchanged += 1
                renumbered += bool(o["label"] and o["label"] != n["label"])
                continue
            ch = {"type": "moved" if moved and not reworded else "moved and modified" if moved else "modified",
                  "where": _name(n) + (f", was {_name(o)}" if o["label"] != n["label"] else ""),
                  "pages": f"old p{o['page']} -> new p{n['page']}", "_new": j, "_unit": n,
                  "old": _place(o, o["body"] if reworded else o["text"]),
                  "new": _place(n, n["body"] if reworded else n["text"])}
            if reworded:
                ch["diff"], removed, added = _word_diff(o["body"], n["body"])
                ch["old_text"], ch["new_text"] = _clip(o["body"]), _clip(n["body"])
                ch["signals"] = _signals(removed, added)
                ch["_added_words"] = added
                sm = difflib.SequenceMatcher(None, o["key"].split(), n["key"].split(), autojunk=False)
                same_title = bool(o["title"]) and o["title"].lower() == (n["title"] or "").lower()
                ratio = max(sm.ratio(), 0.9 if same_title else 0)
                ch["_conf"] = round(CONF_PAIRED_MIN + (100 - CONF_PAIRED_MIN) * min(ratio, 0.95))
            else:
                ch["_conf"] = CONF_MOVED
            if moved:
                before = [new[k] for k in range(j) if k in paired_n]
                ch["moved_note"] = f"now placed after {_name(before[-1])}" if before else "now placed first"
            ch["_pages"] = (o["page"], n["page"])
            changes.append(ch)
        for i, o in enumerate(old):
            if i not in paired_o:
                after = [j for (k, j) in pairs if k > i]
                changes.append({"type": "removed", "where": _name(o), "pages": f"old p{o['page']}",
                                "old_text": _clip(o["text"]), "signals": ["a whole clause or paragraph"],
                                "old": _place(o, o["text"]), "new": None, "_conf": CONF_WHOLE_UNIT, "_pages": (o["page"], None),
                                "_new": min(after) - 0.5 if after else len(new), "_unit": None})
        for j, n in enumerate(new):
            if j not in paired_n:
                changes.append({"type": "added", "where": _name(n), "pages": f"new p{n['page']}",
                                "new_text": _clip(n["text"]), "signals": ["a whole clause or paragraph"],
                                "old": None, "new": _place(n, n["text"]), "_conf": CONF_WHOLE_UNIT, "_pages": (None, n["page"]),
                                "_new": j, "_unit": n})

        changes.sort(key=lambda c: c["_new"])
        for k, c in enumerate(changes, 1):
            c["id"] = f"C{k}"
            conf, reason = c["_conf"], None
            op, np_ = c["_pages"]
            # A clause only in one version is also less certain when the OTHER version was read by OCR: it may have
            # been missed there rather than really removed (or added).
            ocr_involved = (op in old_ocr) or (np_ in new_ocr) or (c["type"] == "removed" and bool(new_ocr)) \
                or (c["type"] == "added" and bool(old_ocr))
            if ocr_involved:
                conf, reason = min(conf, CONF_OCR), "read by OCR: this may be a misreading, not a real edit"
            c["_public"] = {**rules.confidence(conf), **({"reason": reason} if reason else {})}
        redline = build_redline(new_doc, new_meta, changes, new) if changes else None

    cid = f"cmp_{uuid.uuid4().hex[:10]}"
    COMPARISONS[cid] = {c["id"]: c for c in changes}
    public = []
    for c in changes:
        row = {k: v for k, v in c.items() if not k.startswith("_") and k != "type"}
        public.append({"id": c["id"], "type": TYPE_NAMES[c["type"]], "kind": c["type"], "moved": c["type"].startswith("moved"),
                       **{k: v for k, v in row.items() if k != "id"}, **c["_public"]})
    counts = {"added": sum(c["type"] == "added" for c in changes), "removed": sum(c["type"] == "removed" for c in changes),
              "modified": sum(c["type"] in ("modified", "moved", "moved and modified") for c in changes),
              "moved": sum(c["type"] in ("moved", "moved and modified") for c in changes)}
    counts["unchanged"] = unchanged

    ignored = ["spacing differences"] if include_ignored else \
        ["running headers and footers", "page numbers", "spacing differences", "punctuation-only edits"]
    if renumbered:
        ignored.append(f"{renumbered} clause(s) only renumbered")
    result = {
        "status": "ok" if changes else "no_differences", "comparison_id": cid, "old": old_meta["name"], "new": new_meta["name"],
        "languages": {"old": lang_old, "new": lang_new},
        "compared_by": "clause" if any(u["label"] for u in new + old) else "paragraph",
        "ignored": ignored, "changes": public[:MAX_CHANGES],
    }
    warnings = []
    if old_ocr or new_ocr:
        where = " and ".join(x for x in (f"old pages {old_ocr}" if old_ocr else "", f"new pages {new_ocr}" if new_ocr else "") if x)
        warnings.append(f"Read by OCR ({where}): results may be less exact, and some small differences may be misreadings.")
    if lang_old and lang_new and lang_old != lang_new:
        warnings.append(f"The documents are in different languages ({lang_old} and {lang_new}); compared because the user confirmed.")
    if len(public) > MAX_CHANGES:
        result["more"] = f"{len(public) - MAX_CHANGES} more changes are marked in the marked-up copy"
        warnings.append(f"Partial result: only the first {MAX_CHANGES} of {len(public)} changes are listed; all are marked in the copy.")
    result["warnings"] = warnings

    summary = {"counts": counts, "text": _sentence(counts), "key_changes": []}
    if changes:
        from .explain import key_changes
        summary["key_changes"], summary["explanations_unavailable"] = key_changes(cid, changes, explainer)
        result["files_created"] = [redline]
        result["next_step"] = ("present the summary text and the key changes (each with its change id and page), then the "
                               "full list if asked; use only these changes and their explanations; mention warnings; the "
                               "marked-up copy of the new version is in files_created")
    else:
        result["message"] = "No differences found"
        result["next_step"] = "say: No differences found"
    result["summary"] = summary
    return result
