"""Check the FR-AI-01 tools against input/ground_truth.json, without the model.

    .venv/bin/python features/fr_ai_01_workflow/check_tools.py

Files the tools write, and report.txt, go to output/check_tools/ (cleared each run).
"""

import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

import pymupdf  # noqa: E402

from core import pages, store  # noqa: E402
from core.pdfutil import ToolError  # noqa: E402
from features.fr_ai_01_workflow import operations as ops, reading  # noqa: E402
from features.fr_prd_104_cleanup import cleanup  # noqa: E402

INPUT = HERE / "input"
OUT = HERE / "output" / "check_tools"
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

log(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
sys.exit(1 if failures else 0)
