"""Check FR-AI-06's document tool without the model.

    uv run python tests/checks/fr_ai_06_qa.py

It checks that the right page text reaches the model, so an answer can be found and cited. Whether the
model then answers well is judged by running real questions through it (not done here).
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from core import store  # noqa: E402
from core.pdfutil import ToolError  # noqa: E402
from features.fr_ai_06_qa import qa, summary  # noqa: E402

INPUT = ROOT / "samples" / "fr_ai_06_qa"
OUT = ROOT / "tests" / "output" / "fr_ai_06_qa"
shutil.rmtree(OUT, ignore_errors=True)
store.set_root(OUT / "files")

truth = json.loads((INPUT / "ground_truth.json").read_text())["service_report.pdf"]
failures = 0


def check(label, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  -- ' + detail if detail else ''}")


src = store.save_file((INPUT / "service_report.pdf").read_bytes(), "service_report.pdf", 3, source="test")["id"]

print("a short document comes back whole")
r = qa.ask_document(src, "What is the contract number?")
alltext = " ".join(p["text"] for p in r["pages"])
check("all 3 pages returned, marked complete", r["complete"] and [p["page"] for p in r["pages"]] == [1, 2, 3])
check("every fact's answer is in the text of its page",
      all(f["answer"] in next(p["text"] for p in r["pages"] if p["page"] == f["page"]) for f in truth["facts"]))
check("the tool tells the model to answer only from the text", "only from the page text" in r["how_to_answer"])

print("choosing pages")
r = qa.ask_document(src, "When is the next review meeting?", pages="3")
check("pages='3' returns only page 3", [p["page"] for p in r["pages"]] == [3] and r["complete"])
for bad in ("9", "abc"):
    try:
        qa.ask_document(src, "x?", pages=bad)
        check(f"pages='{bad}' is refused", False)
    except ToolError:
        check(f"pages='{bad}' is refused", True)
try:
    qa.ask_document(src, "  ")
    check("an empty question is refused", False)
except ToolError:
    check("an empty question is refused", True)

print("a long document is narrowed to the matching pages")
long = pymupdf.open()
for n in range(1, 101):
    pg = long.new_page()
    body = f"Page {n} filler about routine matters. " * 12
    if n == 41:
        body = "The warranty period for the pump is 36 months from delivery. " + body
    pg.insert_textbox(pymupdf.Rect(50, 50, 545, 790), body, fontsize=10)
lid = store.save_file(long.tobytes(), "long.pdf", 100, source="test")["id"]
r = qa.ask_document(lid, "How long is the warranty period for the pump?")
size = sum(len(p["text"]) for p in r["pages"])
check("the page with the answer is returned", 41 in [p["page"] for p in r["pages"]])
check("the text stays inside the budget", size <= qa.TEXT_BUDGET, f"{size} chars")
check("the result says it is incomplete and which pages were left out",
      not r["complete"] and r["pages_left_out"] and "note" in r)
r2 = qa.ask_document(lid, "How long is the warranty period for the pump?", pages="41")
check("a follow-up on a named page works", [p["page"] for p in r2["pages"]] == [41])

print("summary: a short document goes whole, a long one is read in parts")
def no_model(text, pages):
    raise AssertionError("the model must not be called for a short document")
r = summary.summarize_document(src, notes_writer=no_model)
check("short document: whole text, all 3 pages, no model call",
      r["mode"] == "whole_text" and r["complete"] and [p["page"] for p in r["pages"]] == [1, 2, 3])
seen = []
def fake_notes(text, pages):
    seen.append((pages, text))
    return f"NOTES for pages {pages}"
r = summary.summarize_document(lid, notes_writer=fake_notes)
covered = [n for pages, _ in seen for n in range(int(pages.split("-")[0]), int(pages.split("-")[-1]) + 1)]
check("long document: every page is in exactly one part", sorted(covered) == list(range(1, 101)), f"{len(seen)} parts")
check("every part is under the part size", all(len(t) <= summary.PART_CHARS + 2000 for _, t in seen))
check("the result is complete and carries the notes of every part",
      r["mode"] == "notes" and r["complete"] and len(r["parts"]) == len(seen) and all(p["notes"] for p in r["parts"]))
r = summary.summarize_document(lid, pages="1-10", notes_writer=fake_notes)
check("a page range is summarised on its own", r["pages_summarised"] == "1-10")
r = summary.summarize_document(lid, notes_writer=lambda t, p: "")
check("a part with no notes makes the result incomplete", not r["complete"])
try:
    summary.PART_CHARS, old = 300, summary.PART_CHARS
    summary.summarize_document(lid, notes_writer=fake_notes)
    check("a document with too many parts is refused", False)
except ToolError:
    check("a document with too many parts is refused", True)
finally:
    summary.PART_CHARS = old

print("summary: checking the finished summary (fake answers)")
whole = summary.summarize_document(src)
sc = [("summarize_document", {}, {"ok": True, "result": whole})]
v = lambda a, calls=sc, q="Summarise this": qa.check_answer(a, calls, True, q)
good = v("Service report SR-2026-0457 for October 2026. The desk closed 412 tickets, the response target is 30 "
         "minutes and the Joint Review Board approves changes; next review 12 December 2026.")
check("a faithful summary is verified", good["validation"]["status"] == "verified", str(good["validation"]))
bad = v("The desk closed 999 tickets in October.")
check("a number that is not in the document is flagged", bad["validation"]["status"] == "check" and "999" in bad["validation"]["problems"][0])
r = v("The desk is staffed 9 am to 6 pm.", calls=[], q="Can you summarise the document?")
check("a summary asked for but never read is sent back", r and "retry" in r)
check("no summary asked: a plain reply is left alone", v("Done.", calls=[], q="Remove page 3") is None)

print("checking the model's final answer (fake answers)")
full = qa.ask_document(src, "x?")
read = [("ask_document", {}, {"ok": True, "result": full})]
v = lambda a, calls=read, files=True: qa.check_answer(a, calls, files)
good = v("The contract number is SR-2026-0457 (page 1).")
check("a correct answer with a read page is verified", good["validation"]["status"] == "verified", str(good["validation"]))
bad = v("412 tickets were closed (page 2) and 999 were opened.")
check("a number that is not in the pages is flagged", bad["validation"]["status"] == "check" and "999" in bad["validation"]["problems"][0])
check("a page that was never read is flagged", v("It is 412 (page 7).")["validation"]["status"] == "check")
check("an answer with no page is flagged", v("The contract number is SR-2026-0457.")["validation"]["status"] == "check")
check("a date in another order is still verified", v("The next review is on 12 December 2026 (page 3).")["validation"]["status"] == "verified")
r = v("The document does not specify this.", calls=[])
check("'not in the document' with no reading is sent back to the model", r and "retry" in r)
check("the same words after reading are accepted", v("The document does not say (page 2).")["validation"]["status"] in ("verified", "check"))
check("a non-document reply is left alone", v("Done, I removed page 3.", calls=[], files=True) is None)
check("no files uploaded: nothing to retry", v("The document does not specify this.", calls=[], files=False) is None)

print("refusals and answers brought in from outside (fake answers)")
REF = "The document does not contain this information."
for q in truth["not_in_document"]:
    r = qa.check_answer(REF, read, True, q)
    check(f"a refusal is confirmed: {q}", r["validation"]["status"] == "verified", r["validation"]["note"])
r = qa.check_answer(REF, read, True, "What is the contract number?")
check("a refusal to a question the document answers is flagged",
      r["validation"]["status"] == "check" and "may answer" in r["validation"]["problems"][0])
r = qa.check_answer("The CEO of ACME Test Ltd is Rajesh Kumar, who founded the company and expanded it across Asia.",
                    read, True, "Who is the CEO of ACME Test Ltd?")
check("an answer made up from outside the document is flagged and sent back once",
      r["validation"]["status"] == "check" and "retry" in r and "does not contain this information" in r["retry"],
      "; ".join(r["validation"]["problems"]))
r = qa.check_answer("According to the document (page 3), any change to the service levels must be approved by the "
                    "Joint Review Board, and the next review meeting is on 12 December 2026.", read, True,
                    "Who must approve a change to the service levels?")
check("a faithful longer answer is not flagged", r["validation"]["status"] == "verified", "; ".join(r["validation"]["problems"]))
partial = [("ask_document", {}, {"ok": True, "result": {**full, "pages_left_out": "5-9"}})]
r = qa.check_answer(REF, partial, True, "What is the cancellation fee?")
check("a refusal after a partial read says so", r["validation"]["status"] == "check" and "some pages" in r["validation"]["problems"][0])
r = qa.check_answer("The document mentions probation on page 3, but it does not give details of it.", read, True,
                    "What does the document say about probation?")
check("a page cited for something that is not in the document is flagged and sent back",
      r["validation"]["status"] == "check" and "retry" in r and "probation" in r["validation"]["problems"][0])
r = qa.check_answer("The document does not contain the publication date.", read, True, "When was this report published?")
check("a refusal in the model's own words is still confirmed", r["validation"]["status"] == "verified", r["validation"]["note"])

print("follow-ups answered from pages already in the chat")
hist = [("ask_document", {}, {"ok": True, "result": full})]
h = lambda a, q, earlier=hist, calls=(): qa.check_answer(a, list(calls), True, q, earlier)
r = h("The target for first response is 30 minutes for priority 1 tickets (page 3).", "What is the first response target?")
check("a follow-up is checked against the earlier pages", r["validation"]["status"] == "verified" and "earlier question" in r["validation"]["note"], r["validation"]["note"])
r = h("The target for first response is 45 minutes (page 3).", "What is the first response target?")
check("a wrong number in a follow-up is caught", r["validation"]["status"] == "check" and "retry" in r)
part = [("ask_document", {}, {"ok": True, "result": {**full, "complete": False, "pages_left_out": "3"}})]
r = h("The review is on 12 December 2026 (page 3).", "When is the next review meeting?", earlier=part)
check("pages that were only part of the document are read again", r and "retry" in r)
check("small talk is left alone", h("You're welcome!", "thanks") is None)
check("no earlier pages: nothing to check", h("It is 30 minutes (page 3).", "What is the target?", earlier=[]) is None)
r = h("The document does not contain this information.", "What is the CEO's name?", calls=[])
check("a refusal from memory is sent back to read the document", r and "retry" in r)

print("the agent loop: pages stay in the chat, and every answer is checked")
from core import agent  # noqa: E402
script = []
def fake_model(messages):
    return script.pop(0)
def reply(text=None, call=None):
    msg = {"content": text or ""}
    if call:
        msg["tool_calls"] = [{"id": "c1", "function": {"name": call[0], "arguments": json.dumps(call[1])}}]
    return {"choices": [{"message": msg}], "usage": {}}
agent._call_model = fake_model
sess = store.get_session(store.new_session()); store.attach(sess, src)
def turn(text):
    return [e for e in agent.run_turn(sess, text)]
script[:] = [reply(call=("ask_document", {"file_id": src, "question": "q"})),
             reply("The contract number is SR-2026-0457 (page 1).")]
ev = turn("What is the contract number?")
check("turn 1: read, answered, verified", [e["status"] for e in ev if e["type"] == "validation"] == ["verified"])
script[:] = [reply("The desk closed 412 tickets (page 2).")]
ev = turn("How many tickets were closed?")
v = [e for e in ev if e["type"] == "validation"]
check("turn 2: answered from the chat, still verified", v and v[0]["status"] == "verified" and "earlier question" in v[0]["note"], str(v))
script[:] = [reply("The document does not contain this information."),
             reply(call=("ask_document", {"file_id": src, "question": "q"})),
             reply("The document does not contain this information.")]
ev = turn("Who is the CEO?")
st = [e["status"] for e in ev if e["type"] == "validation"]
check("turn 3: a refusal without reading is sent back, then confirmed", st == ["retry", "verified"], str(st))
check("the wrong first answer never reached the user",
      sum(1 for e in ev if e["type"] == "text") == 1)

cmp_call = [("compare_documents", {}, {"ok": True, "result": {}})]
check("a comparison's answer is not checked as a document summary",
      qa.check_answer("Here is a summary of the changes: 6 changes.", cmp_call, True, "Summarise what changed") is None)

print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
