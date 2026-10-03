"""Check the FR-AI-03 comparison against input/ground_truth.json, without the model.

    .venv/bin/python features/fr_ai_03_compare/check_tools.py

Files the tools write, and report.txt, go to output/check_tools/ (cleared each run).
"""

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(HERE.parents[1]))

import pymupdf  # noqa: E402

from core import store  # noqa: E402
from core.pdfutil import ToolError  # noqa: E402
from features.fr_ai_03_compare import compare, explain  # noqa: E402

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


def upload(name):
    return store.save_file((INPUT / name).read_bytes(), name, truth[name]["pages"], source="test")["id"]


def title(change):
    return change["where"].split("(")[1].split(")")[0] if "(" in change["where"] else change["where"]


def found(result):
    return {(title(c), c["type"]) for c in result["changes"]}


want = {(c["clause"], c["type"]) for c in truth["changes"]}
v1, v2 = upload("contract_v1.pdf"), upload("contract_v2.pdf")

log("contract_v1 -> contract_v2")
r = compare.compare_documents(v1, v2)
check("exactly the planted changes, of the right type", found(r) == want,
      f"missing {sorted(want - found(r))}, extra {sorted(found(r) - want)}" if found(r) != want else
      ", ".join(f"{c['id']} {title(c)} {c['type']}" for c in r["changes"]))
sig = {title(c): c.get("signals", []) for c in r["changes"]}
check("number changes surfaced as signals",
      "numbers changed: 30 -> 45" in sig["Payment"] and "numbers changed: 12 -> 6" in sig["Liability"])
check("unchanged clauses and renumbering not reported",
      r["summary"]["unchanged"] == len(truth["unchanged"]) and "1 clause(s) only renumbered" in r["ignored"],
      r["ignored"])
check("headers and page numbers not reported",
      not any("Version" in c.get("diff", "") + c.get("old_text", "") + c.get("new_text", "") or
              "Page " in c.get("diff", "") for c in r["changes"]))

_, path = store.get_file(r["files_created"][0]["id"])
with pymupdf.open(path) as d:
    marks = [(p.number + 1, a.type[1], a.info["content"].split(":")[0],
              p.get_textbox(a.rect).strip()) for p in d for a in p.annots()]
ids = {m[2].split()[0] for m in marks}
check("marked-up copy carries a mark for every change", ids == {c["id"] for c in r["changes"]},
      f"{len(marks)} marks for {', '.join(sorted(ids))}")
check("no highlight on a clause number", not any(m[1] == "Highlight" and m[3][:2].rstrip(".").isdigit()
                                                 and m[3].rstrip(".").isdigit() for m in marks),
      "; ".join(f"p{m[0]} {m[2]}: '{m[3][:30]}'" for m in marks if m[1] == "Highlight"))

log("explanation checks")
by_title = {title(c): c["id"] for c in r["changes"]}
e = explain.check_change_explanations(r["comparison_id"], [
    {"change_id": by_title["Payment"], "significance": "material",
     "explanation": "Payment is now due within 45 days instead of 30."},
    {"change_id": "C99", "significance": "minor", "explanation": "Something changed."},
    {"change_id": by_title["Liability"], "significance": "material",
     "explanation": "The liability cap went from 12 months of fees down to 3 months."},
])
check("a correct explanation is verified", [v["change_id"] for v in e["verified"]] == [by_title["Payment"]])
check("unknown change id and a wrong figure are rejected",
      [x["reason"][:12] for x in e["rejected"]] == ["no change wi", "states 3, wh"],
      "; ".join(x["reason"] for x in e["rejected"]))
check("unexplained changes are listed", len(e["not_explained"]) == len(r["changes"]) - 1)
try:
    explain.check_change_explanations("cmp_nope", [{"change_id": "C1"}])
    check("an unknown comparison is refused", False)
except ToolError as err:
    check("an unknown comparison is refused", "run compare_documents first" in str(err))

log("identical files")
same = compare.compare_documents(v1, upload("contract_v1.pdf"))
check("no changes and no marked-up copy", not same["changes"] and "files_created" not in same,
      same["next_step"])

log("contract_v1 -> contract_v2_scanned (OCR)")
s = compare.compare_documents(v1, upload("contract_v2_scanned.pdf"))
material = {(c["clause"], c["type"]) for c in truth["changes"] if c["material"]}
check("OCR warning given", "OCR" in s.get("warning", ""), s.get("warning", ""))
check("the material changes are still found", material <= found(s),
      f"found {len(s['changes'])} changes; extra from OCR: {sorted(found(s) - want) or 'none'}")

log(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
sys.exit(1 if failures else 0)
