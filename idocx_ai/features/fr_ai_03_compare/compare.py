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

from core.pdfutil import ToolError, open_pdf
from core.text import textpage

from .redline import build_redline

BAND = 0.06            # top/bottom 6% of a page: running headers, footers, page numbers
MIN_CLAUSES = 3        # fewer numbered headings than this: compare paragraph by paragraph
PAIR_RATIO = 0.5       # word similarity for two units to count as one modified unit
MAX_TEXT = 400         # characters of clause text returned per change
MAX_CHANGES = 40

CLAUSE = re.compile(r"^\s*(?:(?:clause|section|article)\s+(\d+(?:\.\d+)*)[.):]?|(\d+(?:\.\d+)*)[.)])\s+(.*)",
                    re.I | re.S)
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
OBLIGATION = {"shall", "must", "may", "not", "never", "only", "will", "required", "prohibited", "unless"}

# comparison_id -> changes, so explanations can be checked against the real list
COMPARISONS: dict[str, dict] = {}


def _clean(text: str) -> str:
    text = re.sub(r"-\n(?=[a-z])", "", text)          # rejoin words hyphenated across lines
    return re.sub(r"\s+", " ", text).strip()


def _blocks(doc) -> tuple[list[dict], list[int]]:
    out, ocr = [], []
    for page in doc:
        tp, source = textpage(page)
        if not tp:
            continue
        if source == "OCR":
            ocr.append(page.number + 1)
        h = page.rect.height
        for x0, y0, x1, y1, text, _, kind in page.get_text("blocks", textpage=tp, sort=True):
            if kind != 0 or y1 < BAND * h or y0 > (1 - BAND) * h:
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


def _units(blocks: list[dict]) -> list[dict]:
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
        u["letters"] = re.sub(r"[\W_]+", "", u["key"])
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


def compare_documents(old_file_id: str, new_file_id: str) -> dict:
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
    with old_doc, new_doc:
        ob, old_ocr = _blocks(old_doc)
        nb, new_ocr = _blocks(new_doc)
        old, new = _units(ob), _units(nb)
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
                  "pages": f"old p{o['page']} -> new p{n['page']}", "_new": j, "_unit": n}
            if reworded:
                ch["diff"], removed, added = _word_diff(o["body"], n["body"])
                ch["old_text"], ch["new_text"] = _clip(o["body"]), _clip(n["body"])
                ch["signals"] = _signals(removed, added)
                ch["_added_words"] = added
            if moved:
                before = [new[k] for k in range(j) if k in paired_n]
                ch["moved_note"] = f"now placed after {_name(before[-1])}" if before else "now placed first"
            changes.append(ch)
        for i, o in enumerate(old):
            if i not in paired_o:
                after = [j for (k, j) in pairs if k > i]
                changes.append({"type": "removed", "where": _name(o), "pages": f"old p{o['page']}",
                                "old_text": _clip(o["text"]), "signals": ["a whole clause or paragraph"],
                                "_new": min(after) - 0.5 if after else len(new), "_unit": None})
        for j, n in enumerate(new):
            if j not in paired_n:
                changes.append({"type": "added", "where": _name(n), "pages": f"new p{n['page']}",
                                "new_text": _clip(n["text"]), "signals": ["a whole clause or paragraph"],
                                "_new": j, "_unit": n})

        changes.sort(key=lambda c: c["_new"])
        for k, c in enumerate(changes, 1):
            c["id"] = f"C{k}"
        redline = build_redline(new_doc, new_meta, changes, new) if changes else None

    cid = f"cmp_{uuid.uuid4().hex[:10]}"
    COMPARISONS[cid] = {c["id"]: c for c in changes}
    public = [{k: v for k, v in c.items() if not k.startswith("_")} for c in changes]
    counts = {t: sum(c["type"] == t for c in changes) for t in ("added", "removed", "modified", "moved")}
    counts["moved"] += sum(c["type"] == "moved and modified" for c in changes)
    counts["unchanged"] = unchanged
    result = {
        "comparison_id": cid, "old": old_meta["name"], "new": new_meta["name"],
        "compared_by": "clause" if any(u["label"] for u in new + old) else "paragraph",
        "summary": counts,
        "ignored": f"running headers and footers, page numbers, spacing and punctuation-only edits"
                   + (f", and {renumbered} clause(s) only renumbered" if renumbered else ""),
        "changes": public[:MAX_CHANGES],
        "next_step": ("explain only these changes, citing their ids (C1, C2...). If the user wants the meaningful "
                      "changes summarised or explained, first call check_change_explanations with one item per "
                      "change (material or minor) and use only the verified explanations. The marked-up copy of "
                      "the new version is in files_created." if changes else
                      "the two versions have the same text; say so"),
    }
    if len(public) > MAX_CHANGES:
        result["more"] = f"{len(public) - MAX_CHANGES} more changes are marked in the marked-up copy"
    if old_ocr or new_ocr:
        result["warning"] = (f"read by OCR: {'old pages ' + str(old_ocr) if old_ocr else ''} "
                             f"{'new pages ' + str(new_ocr) if new_ocr else ''}. Some small wording differences may "
                             f"be OCR misreadings, not real edits; say so.").replace("  ", " ")
    if redline:
        result["files_created"] = [redline]
    return result
