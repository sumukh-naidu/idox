"""Check the FR-AI-01 tools against samples/fr_ai_01_workflow/ground_truth.json, without the model.

    .venv/bin/python tests/checks/fr_ai_01_workflow.py

Files the tools write, and report.txt, go to tests/output/fr_ai_01_workflow/ (cleared each run).
"""

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from core import pages, store  # noqa: E402
from core.pdfutil import ToolError  # noqa: E402
from features.fr_ai_01_workflow import operations as ops, plan, reading  # noqa: E402
from features.fr_prd_104_cleanup import cleanup  # noqa: E402

INPUT = ROOT / "samples" / "fr_ai_01_workflow"
OUT = ROOT / "tests" / "output" / "fr_ai_01_workflow"
shutil.rmtree(OUT, ignore_errors=True)
store.set_root(OUT / "files")

truth = json.loads((INPUT / "ground_truth.json").read_text())
failures, report = 0, []


def log(line=""):
    print(line)
    report.append(line)


def check(label, ok, detail=""):
    global failures
    failures += not ok
    log(f"  {'PASS' if ok else 'FAIL'}  {label}{'  -- ' + detail if detail else ''}")


def must_fail(label, fn, expect):
    try:
        fn()
        check(label, False, "it was not refused")
    except ToolError as e:
        check(label, expect in str(e), str(e))


def upload(name):
    return store.save_file((INPUT / name).read_bytes(), name, truth[name]["pages"], source="test")["id"]


def markers(fid):
    """The [marker X] line of every page, or None for a page without one."""
    _, path = store.get_file(fid)
    with pymupdf.open(path) as d:
        return [(m.group(1) if (m := re.search(r"\[marker (\w+)\]", p.get_text())) else None) for p in d]


def size(fid):
    return store.get_file(fid)[0]["size"]


log("merge -> remove blank -> compress (doc example 1)")
a, b = upload("merge_a.pdf"), upload("merge_b.pdf")
m = ops.merge_pdfs([a, b])
check("merged A then B, in order", markers(m["new_file_id"]) == ["A1", "A2", "A3", "B1", None, "B3"],
      f"{m['pages']} pages: {m['order'][0]['pages_in_merged_file']} from A, {m['order'][1]['pages_in_merged_file']} from B")
blank = cleanup.find_blank_pages(m["new_file_id"])
check("blank page found in the merged file", [x["page"] for x in blank["blank_pages"]] == [5])
r = pages.remove_pages(m["new_file_id"], [5])
c = ops.compress_pdf(r["new_file_id"])
check("compressed and verified", c.get("saved_percent", 0) >= 30 and markers(c["new_file_id"]) == ["A1", "A2", "A3", "B1", "B3"],
      f"{c.get('size_kb_before')} KB -> {c.get('size_kb_after')} KB ({c.get('saved_percent')}% saved)")
check("compressing an already-compressed file writes nothing",
      ops.compress_pdf(c["new_file_id"]).get("nothing_to_do") is True)

log("split, extract, rotate, reorder, rename (contract_12p.pdf)")
con = upload("contract_12p.pdf")
s = ops.split_pdf(con, [{"pages": "1-3", "name": "Definitions"}, {"pages": "4-6", "name": "Payment Terms"},
                        {"pages": "7-end", "name": "Schedules"}])
got = [(p["name"], markers(p["new_file_id"])) for p in s["parts"]]
check("split into three named parts with the right pages",
      got == [("Definitions.pdf", ["C1", "C2", "C3"]), ("Payment Terms.pdf", ["C4", "C5", "C6"]),
              ("Schedules.pdf", [f"C{i}" for i in range(7, 13)])] and s["pages_not_in_any_part"] == "none",
      ", ".join(f"{n} ({len(mk)}p)" for n, mk in got))
s2 = ops.split_pdf(con, [{"pages": "1-3"}, {"pages": "7-9"}])
check("split reports pages left out", s2["pages_not_in_any_part"] == "4-6, 10-12", s2["pages_not_in_any_part"])
e = ops.extract_pages(con, "6, 2-3")
check("extract keeps the order asked for", markers(e["new_file_id"]) == ["C6", "C2", "C3"])
rot = ops.rotate_pages(e["new_file_id"], "1", 90)
_, path = store.get_file(rot["new_file_id"])
with pymupdf.open(path) as d:
    check("rotate turns only the chosen page", [p.rotation for p in d] == [90, 0, 0])
ro = ops.reorder_pages(con, "12, 1-11")
check("reorder moves the last page to the front", markers(ro["new_file_id"])[:3] == ["C12", "C1", "C2"])
must_fail("reorder must list every page once", lambda: ops.reorder_pages(con, "1-5, 5, 7-12"), "missing 6")
rn = ops.rename_file(con, "Service Agreement 2026")
check("rename makes a copy under the new name", rn["new_name"] == "Service Agreement 2026.pdf"
      and size(rn["new_file_id"]) == size(con))
must_fail("page 13 of a 12-page file is refused", lambda: ops.extract_pages(con, "10-13"), "do not exist")
must_fail("merging one file is refused", lambda: ops.merge_pdfs([con]), "at least two")

log("reading and task verification")
memo = upload("project_memo.pdf")
rd = reading.read_document(memo)
text = " ".join(p["text"] for p in rd["pages"])
check("read_document returns the memo's text layer",
      all(dl in text for dl in truth["project_memo.pdf"]["deadlines"]) and rd["pages"][0]["source"] == "text layer")
scan = upload("scanned_notice.pdf")
rs = reading.read_document(scan)
check("read_document OCRs the scanned notice",
      rs["pages"][0]["source"] == "OCR" and all(dl in rs["pages"][0]["text"] for dl in truth["scanned_notice.pdf"]["deadlines"]),
      f"source {rs['pages'][0]['source']}, {len(rs['pages'][0]['text'])} characters")
v = reading.verify_tasks(memo, [
    {"task": "Complete vendor onboarding", "deadline": "15 October 2026",
     "quote": "Vendor onboarding must be completed by 15 October 2026.", "page": 1},
    {"task": "Book the hotel", "deadline": "1 November 2026",
     "quote": "Book the hotel for the offsite by 1 November 2026.", "page": 1},
    {"task": "Send the NDA", "deadline": "2026-11-05",
     "quote": "Please send the signed NDA to the legal team no later than 5 November 2026.", "page": 2},
])
check("verify_tasks keeps a real quote", [x["task"] for x in v["verified"]] == ["Complete vendor onboarding"])
check("verify_tasks drops an invented quote and a reformatted deadline",
      [x["reason"][:20] for x in v["rejected"]] == ["quote not found in t", "the deadline does no"],
      "; ".join(x["reason"] for x in v["rejected"]))
v2 = reading.verify_tasks(scan, [{"task": "Clear the storage room", "deadline": "18 October 2026",
                                   "quote": "All teams must clear the storage room by 18 October 2026.", "page": 1}])
check("verify_tasks works on OCR text", len(v2["verified"]) == 1, v2["rejected"][0]["reason"] if v2["rejected"] else "")

log("planning (fake planner: the model is not used)")
fa = store.save_file((INPUT / "merge_a.pdf").read_bytes(), "merge_a.pdf", 2, source="upload")["id"]
fb = store.save_file((INPUT / "merge_b.pdf").read_bytes(), "merge_b.pdf", 2, source="upload")["id"]
n_files = len(list(store.FILES.glob("*.pdf")))
def planner_of(*answers):
    seen = []
    queue = list(answers)
    def ask(instruction, files):
        seen.append(instruction)
        return queue.pop(0)
    ask.seen = seen
    return ask
MERGE = {"operation": "merge", "files": [fa, fb], "settings": {}, "reason": "Join the two files."}
COMPRESS = {"operation": "compress", "files": ["@1"], "settings": {}, "reason": "Make the merged file smaller."}
r = plan.plan_workflow([fa, fb], "Merge these and compress the result", planner_of({"unclear": False, "steps": [MERGE, COMPRESS]}))
check("a clear instruction gives an ordered plan", r["status"] == "plan" and [s["operation"] for s in r["steps"]] == ["merge", "compress"])
check("each step has files, settings, a reason and a confidence",
      all({"files", "settings", "reason", "confidence", "low_confidence"} <= set(s) for s in r["steps"]))
check("steps named in the instruction are 95% and not low", all(s["confidence"] == 95 and not s["low_confidence"] for s in r["steps"]))
check("nothing is run: no file was written", len(list(store.FILES.glob("*.pdf"))) == n_files and "Nothing has been run" in r["runs_nothing"])
r = plan.plan_workflow([fa, fb], "Merge these", planner_of({"unclear": False, "steps": [MERGE, COMPRESS]}))
check("a step the user did not ask for is 70% and marked low confidence",
      r["steps"][1]["confidence"] == 70 and r["steps"][1]["low_confidence"] and r["confidence"] == 70 and r["low_confidence"])

BAD_OP = {"operation": "email it", "files": [fa], "settings": {}, "reason": "Send it."}
p = planner_of({"unclear": False, "steps": [BAD_OP]}, {"unclear": False, "steps": [MERGE]})
r = plan.plan_workflow([fa, fb], "Merge these", p)
check("an operation outside the list is sent back once, then the repaired plan is used",
      r["status"] == "plan" and "not an allowed operation" in p.seen[1], r.get("error", {}).get("problems", ""))
r = plan.plan_workflow([fa, fb], "Merge these", planner_of(*[{"unclear": False, "steps": [BAD_OP]}] * 2))
check("a plan that stays invalid returns an error and no steps", r["status"] == "error" and r["steps"] == [] and r["error"]["code"] == "plan_not_valid")

for label, step, expect in [
    ("a file that was not selected", {**MERGE, "files": [fa, "f_000000000000"]}, "was not selected"),
    ("a step pointing forward", {**COMPRESS, "files": ["@2"]}, "earlier step"),
    ("a translation into an unsupported language", {"operation": "translate", "files": [fa], "settings": {"target_language": "Japanese"}, "reason": "x"}, "not supported"),
    ("the same source and target language", {"operation": "translate", "files": [fa], "settings": {"target_language": "Hindi", "source_language": "Hindi"}, "reason": "x"}, "must differ"),
    ("a translation with no target", {"operation": "translate", "files": [fa], "settings": {}, "reason": "x"}, "needs the setting 'target_language'"),
    ("a setting the operation does not have", {**COMPRESS, "files": [fa], "settings": {"colour": "red"}}, "no setting"),
    ("a merge of a single file", {**MERGE, "files": [fa]}, "needs at least 2"),
    ("a step with no reason", {**MERGE, "reason": ""}, "no reason"),
]:
    r = plan.plan_workflow([fa, fb], "Merge these", planner_of(*[{"unclear": False, "steps": [step]}] * 2))
    check(f"rejected: {label}", r["status"] == "error" and any(expect in x for x in r["error"]["problems"]), "; ".join(r.get("error", {}).get("problems", [])))

r = plan.plan_workflow([fa], "make it nice", planner_of({"unclear": True, "steps": []}))
check("an unclear instruction returns an error with 2-3 examples and no steps",
      r["status"] == "error" and r["steps"] == [] and 2 <= len(r["error"]["examples"]) <= 3 and r["error"]["code"] == "instruction_not_understood")
r = plan.plan_workflow([fa], "   ", planner_of())
check("an empty instruction is not understood", r["error"]["code"] == "instruction_not_understood")
r = plan.plan_workflow([fa], "Compress other_file.pdf", planner_of())
check("a file named but not selected gets the question 'Which file?', no steps",
      r["status"] == "question" and r["question"] == "Which file?" and r["steps"] == [])
r = plan.plan_workflow([fa], "Compress it", planner_of({"unclear": False, "missing_file": "the old contract", "steps": []}))
check("the model reporting a missing file also gets the question", r["status"] == "question")
r = plan.plan_workflow([], "Compress it", planner_of())
check("no file selected: 'Which file?'", r["status"] == "question")
r = plan.plan_workflow([fa], "merge " * 500, planner_of())
check("a very long instruction returns an error with the limit", r["error"]["code"] == "instruction_too_long" and r["error"]["limit"] == plan.MAX_INSTRUCTION_CHARS)
r = plan.plan_workflow([fa] * 50, "Compress them", planner_of())
check("50 files returns an error with the limit", r["error"]["code"] == "too_many_files" and r["error"]["limit"] == plan.MAX_FILES)

r = plan.plan_workflow([fa], "Split this into pages 1-4 and 5-12, then translate it to Japanese.", planner_of())
check("translating into an unsupported language names the supported languages and plans nothing",
      r["error"]["code"] == "unsupported_language" and r["steps"] == [] and "Hindi" in r["error"]["message"] and r["error"]["supported"] == plan.LANGUAGES)
r = plan.plan_workflow([fa], "Translate this to Hindi", planner_of({"unclear": False, "steps": [
    {"operation": "translate", "files": [fa], "settings": {"target_language": "Hindi"}, "reason": "Translate it."}]}))
check("a setting the user wrote keeps 95%", r["steps"][0]["confidence"] == 95 and "chosen_by_ai" not in r["steps"][0])
r = plan.plan_workflow([fa], "Summarise this and make tasks of the deadlines", planner_of({"unclear": False, "steps": [
    {"operation": "summarise", "files": [fa], "settings": {"type": "key milestone and obligation dates"}, "reason": "Find dates."},
    {"operation": "create task", "files": ["@1"], "settings": {"title": "Project Deadlines"}, "reason": "Make a task."}]}))
check("settings the AI chose are listed, at 70% and low confidence",
      [s["chosen_by_ai"] for s in r["steps"]] == [["type"], ["title"]] and all(s["confidence"] == 70 and s["low_confidence"] for s in r["steps"]))
r = plan.plan_workflow([fa], "Split this into pages 1-4 and 5-12", planner_of({"unclear": False, "steps": [
    {"operation": "split", "files": [fa], "settings": {"parts": [{"pages": "1-4"}, {"pages": "5-12"}]}, "reason": "Split."}]}))
check("page ranges the user wrote count as given", r["steps"][0]["confidence"] == 95, str(r["steps"][0]))

log(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
sys.exit(1 if failures else 0)
