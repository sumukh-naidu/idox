"""FR-AI-06 Document Q&A: hand the model the document's own text, page by page, to answer from.

The tool only fetches text. The chat model writes the answer, from these pages only.
"""

import re

from core.pdfutil import ToolError, open_pdf, page_ranges
from core.text import page_text

# The agent keeps only the first 36,000 characters of a tool result (core/agent.py), so the text must fit well
# inside that. 30,000 characters is about 8,000-10,000 tokens, leaving room in the 32k context for the chat.
TEXT_BUDGET = 30000

_STOP = set("the and for are was were what which who whom whose when where why how does did this that these those "
            "with from into about have has had not you your any all can could would should will shall than then "
            "there their they them its is it of in on at to by an a or as be do if so no me my we our us".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1 and w not in _STOP}


def _parse_pages(spec: str | None, count: int) -> list[int]:
    if not spec:
        return list(range(1, count + 1))
    out: set[int] = set()
    for part in str(spec).replace(" ", "").split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ToolError(f"pages must look like '1-3' or '2,5', got '{spec}'")
        lo, hi = int(m[1]), int(m[2] or m[1])
        out.update(range(lo, hi + 1))
    bad = sorted(p for p in out if not 1 <= p <= count)
    if bad:
        raise ToolError(f"the file has {count} pages; page(s) {bad} do not exist")
    return sorted(out)


def ask_document(file_id: str, question: str, pages: str | None = None) -> dict:
    if not str(question or "").strip():
        raise ToolError("question is empty")
    meta, doc = open_pdf(file_id)
    with doc:
        wanted = _parse_pages(pages, doc.page_count)
        texts = {n: page_text(doc, file_id, n) for n in wanted}
        total = doc.page_count

    # Whole text fits: return everything. Otherwise keep the pages that share the most words with the question.
    if sum(len(t) for t, _ in texts.values()) <= TEXT_BUDGET:
        chosen = list(wanted)
    else:
        q = _words(question)
        ranked = sorted(wanted, key=lambda n: (-len(q & _words(texts[n][0])), n))
        chosen, used = [], 0
        for n in ranked:
            size = len(texts[n][0])
            if used + size <= TEXT_BUDGET or not chosen:
                chosen.append(n)
                used += size
        chosen.sort()

    out = []
    for n in chosen:
        text, source = texts[n]
        if len(text) > TEXT_BUDGET:
            text = text[:TEXT_BUDGET] + " [page text cut here]"
        out.append({"page": n, "read_by": source, "text": text})
    left_out = [n for n in wanted if n not in chosen]
    result = {
        "file_id": file_id, "name": meta["name"], "pages_in_file": total,
        "pages_returned": page_ranges(chosen),
        "complete": not left_out,
        "pages": out,
        "how_to_answer": "Answer only from the page text above and say which page(s) it came from. If the text does "
                         f"not contain the answer, reply exactly: {_REFUSAL} Never fill the gap from general "
                         "knowledge, never guess, and do not use details from nearby text that is about something else.",
    }
    if left_out:
        result["pages_left_out"] = page_ranges(left_out)
        result["note"] = ("The document is too long to return whole: these are the pages that best match the "
                          "question. If they do not contain the answer, say it was not found in the pages searched "
                          "and offer to look at pages_left_out (call again with pages).")
    return result


# ---- Checking the model's final answer against the text it was given -------------------------------------

_DOESNT_SAY = re.compile(r"\b(?:does not|doesn't|do not|don't|did not|didn't)\s+(?:specify|say|mention|state|contain|"
                         r"include|provide|cover|address|list|give|offer|explain|describe|elaborate)|\bnot\s+(?:mentioned|specified|stated|found|included|"
                         r"provided)|\bno\s+(?:information|mention)\b", re.I)
_PAGE_CITE = re.compile(r"\b(?:pages?|pp?\.)\s*(\d+)(?:\s*(?:-|–|to|and|&|,)\s*(\d+))?", re.I)
_NUMBER = re.compile(r"\d[\d,.]*\d|\d")


def _numbers_in(text: str) -> set[str]:
    """Whole numbers in a text ('45' is not found inside 'SR-2026-0457'); commas dropped, so 25,000 = 25000."""
    return {n.rstrip(".,").replace(",", "") for n in _NUMBER.findall(text)}


def _cited_pages(answer: str) -> tuple[set[int], str]:
    pages = set()
    for m in _PAGE_CITE.finditer(answer):
        lo, hi = int(m[1]), int(m[2] or m[1])
        pages.update(range(lo, min(hi, lo + 50) + 1))
    return pages, _PAGE_CITE.sub(" ", answer)


_REFUSAL = "The document does not contain this information."
_FILLER = set("according document documents states stated state mentions mentioned mention specifies specified "
              "described describes describe following information provided provides provide includes included "
              "include notes noted explains explained page pages text section sections called".split())
_ASKING = set("many much does document report say says said tell what which who whom when where about according "
              "mentioned mention state stated give list name".split())


def _stems(text: str, min_len: int = 5) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= min_len}


def _is_refusal(answer: str) -> bool:
    return bool(_DOESNT_SAY.search(answer or "")) or "does not contain this information" in (answer or "").lower()


def _in_text(term: str, words: set[str]) -> bool:
    """Does the text have this word, allowing endings (leader/leaders, publish/published)? 'transactional' is not
    'transformational': the shared start must cover all but the last two letters."""
    prefix = term[: max(4, len(term) - 2)]
    return any(w.startswith(prefix) for w in words)


def _check_refusal(answer: str, question: str, pages: dict[int, str], left_out: bool) -> dict:
    """The model says the document does not have it: is that plausible given what was read?"""
    terms = [w for w in re.findall(r"[a-z0-9]+", question.lower())
             if len(w) > 2 and w not in _STOP and w not in _ASKING and w not in _FILLER]
    words = set(re.findall(r"[a-z0-9]+", " ".join(pages.values()).lower()))
    absent = [w for w in terms if not _in_text(w, words)]
    problems, retry = [], None
    if terms and not absent:
        where = sorted(n for n, t in pages.items() if any(w in t.lower() for w in terms))
        problems.append("every key word of the question appears in the document" +
                        (f" (page {', '.join(map(str, where))})" if where else "") + ", so it may answer this")
    claims = [w for w in absent if w in answer.lower()] if _PAGE_CITE.search(answer) else []
    if claims:   # "it mentions X on page 3" about something that is nowhere in the document
        problems.append(f"the answer cites a page for '{', '.join(claims)}', which never appears in the document")
        retry = ("[Check] '" + ", ".join(claims) + "' does not appear anywhere in the document, so you cannot cite a "
                 "page for it. Reply exactly: " + _REFUSAL)
    if left_out:
        problems.append("only some pages were searched, so this is not proof the document lacks it")
    note = (f"not in the document: '{', '.join(absent)}' never appears" if absent
            else "refusal could not be confirmed from the text")
    verdict = {"validation": {"status": "check" if problems else "verified", "problems": problems, "note": note}}
    if retry:
        verdict.update(retry=retry, problems=problems)
    return verdict


def _unsupported_words(answer: str, question: str, haystack: str) -> list[str]:
    """Words of the answer that neither the question nor the pages contain: likely brought in from outside."""
    have = _stems(haystack) | _stems(question)
    out = []
    for w in dict.fromkeys(re.findall(r"[a-z]{6,}", answer.lower())):
        if w not in _FILLER and w[:5] not in have:
            out.append(w)
    return out


def check_answer(answer: str, calls: list, has_files: bool, question: str = "", earlier: list | None = None) -> dict | None:
    """Check one final answer: was the document read, did the cited pages come back, do its numbers appear in the text?"""
    from . import summary   # summary imports from this module, so import it late

    if any(name == "compare_documents" for name, _, _ in calls):
        return None          # a comparison's summary is checked against its own changes, not as a document answer

    if any(name == "summarize_document" for name, _, _ in calls):
        return summary.check_summary(answer, calls)
    if has_files and summary.wants_summary(question) and not any(n == "ask_document" for n, _, _ in calls):
        return {"retry": "[Check] The user asked for a summary but you have not read the document. Call "
                         "summarize_document, then write the summary from what it returns.",
                "problems": ["a summary was asked for but the document was not read"]}
    reads = [out["result"] for name, _, out in calls if name == "ask_document" and out.get("ok")]
    from_history = False
    if not reads and has_files and not _is_refusal(answer) and not calls:
        # A follow-up answered from pages already in the chat: check against those pages, if they were the whole
        # document; if they were only some pages, read again so nothing is judged on a part.
        past = [out["result"] for name, _, out in (earlier or []) if name == "ask_document" and out.get("ok")]
        if past:
            last = past[-1]
            seen = {p["page"]: p["text"] for p in last["pages"]}
            if not last["complete"] or len(seen) < last["pages_in_file"]:
                if _PAGE_CITE.search(answer) or _words(question) & _words(" ".join(seen.values())):
                    return {"retry": "[Check] Only some pages of the document were read earlier. Call ask_document "
                                     "again for this question and answer from the page text it returns.",
                            "problems": ["answered from a part of the document read earlier"]}
            elif _PAGE_CITE.search(answer) or _words(question) & _words(" ".join(seen.values())):
                reads, from_history = [last], True
    if not reads:
        if has_files and _is_refusal(answer):
            return {"retry": "[Check] You said the document does not contain this, but you have not read it. Call "
                             "ask_document with the user's question, then answer from the page text it returns.",
                    "problems": ["answered 'not in the document' without reading the document"]}
        return None
    pages = {p["page"]: p["text"] for r in reads for p in r["pages"]}
    haystack = " ".join(pages.values()).lower().replace(",", "")
    if _is_refusal(answer):
        return _check_refusal(answer, question, pages, any(r.get("pages_left_out") for r in reads))
    problems = []

    cited, rest = _cited_pages(answer)
    if not cited:
        problems.append("the answer does not name a page")
    elif cited - set(pages):
        problems.append(f"cites page(s) {sorted(cited - set(pages))}, which were not read")

    in_pages = _numbers_in(" ".join(pages.values()))
    missing = sorted(n for n in _numbers_in(rest) if len(n) > 1 and n not in in_pages)
    if missing:
        problems.append(f"{', '.join(missing)} not found in the pages read")

    outside = _unsupported_words(rest, question, haystack)
    if len(outside) >= 4 and len(outside) / max(1, len(set(re.findall(r"[a-z]{6,}", rest.lower())))) > 0.3:
        problems.append("words that are not in the document: " + ", ".join(outside[:6]) +
                        " - the answer may come from outside it")

    left_out = {p for r in reads for p in (r.get("pages_left_out") or [])}
    status = "check" if problems else "verified"
    invented = [x for x in problems if "not found in the pages" in x or "not in the document" in x]
    note = ("pages read: " + ", ".join(str(n) for n in sorted(pages)) +
            ("; the document was too long to read whole, so other pages were not searched" if left_out else "") +
            ("; pages read for an earlier question" if from_history else ""))
    verdict = {"validation": {"status": status, "problems": problems, "note": note}}
    if invented:   # details that are not in the pages: send the answer back once to stay inside the document
        verdict["retry"] = ("[Check] Your answer contains details that are not in the document (" + "; ".join(invented) +
                            "). Answer again using only the page text, or reply exactly: " + _REFUSAL)
        verdict["problems"] = problems
    return verdict
