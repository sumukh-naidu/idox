"""Check the FR-PRD-105 checks and fixes against input/ground_truth.json, without the model.
(describe_images needs the model; check_agent.py covers it.)

    .venv/bin/python features/fr_prd_105_accessibility/check_tools.py

Files the tools write, and report.txt, go to output/check_tools/ (cleared each run).
"""

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

import pymupdf  # noqa: E402

from core import store  # noqa: E402
from features.fr_prd_105_accessibility import checks, fixes  # noqa: E402

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


def statuses(result):
    return {c["id"]: c["status"] for c in result["checks"]}


for name, gt in truth.items():
    if name.startswith("_"):
        continue
    log(name)
    fid = store.save_file((INPUT / name).read_bytes(), name, gt["pages"], source="test")["id"]
    got = statuses(checks.check_accessibility(fid))
    diff = {k: f"want {v}, got {got.get(k)}" for k, v in gt["before"].items() if got.get(k) != v}
    check("every check gives the expected status", not diff, "; ".join(f"{k}: {v}" for k, v in diff.items()))

    if "after_fix" not in gt:
        continue
    fixed = fixes.fix_accessibility(fid)
    after = statuses(checks.check_accessibility(fixed["new_file_id"]))
    diff = {k: f"want {v}, got {after.get(k)}" for k, v in gt["after_fix"].items() if after.get(k) != v}
    check("after the fix: improved checks pass, out-of-scope ones still fail", not diff,
          "; ".join(f"{k}: {v}" for k, v in diff.items()) or f"now passing: {', '.join(fixed['now_passing'])}")
    _, path = store.get_file(fixed["new_file_id"])
    with pymupdf.open(path) as d:
        title, lang = d.metadata.get("title"), checks.catalog_key(d, "Lang")[1]
        text = " ".join(p.get_text() for p in d)
    check("title and language set as expected", title == gt["expected_title"] and lang == gt["expected_language"],
          f"title '{title}', language '{lang}'")
    if "text_after_fix" in gt:
        check("OCR text layer is searchable", all(t in text for t in gt["text_after_fix"]),
              f"{len(text.split())} words now extractable")
        with pymupdf.open(INPUT / name) as o, pymupdf.open(path) as n:
            same = all(o.extract_image(o[i].get_images()[0][0])["image"] == n.extract_image(n[i].get_images()[0][0])["image"]
                       for i in range(o.page_count))
        check("the scanned images themselves are untouched", same)

log("language detection")
for text, want in [("The inspection of the warehouse took place and all of the exits were clear.", "en"),
                   ("यह एक परीक्षण दस्तावेज़ है और इसमें कोई वास्तविक जानकारी नहीं है", "hi"),
                   ("El informe de la empresa es para los clientes y las ventas del año", "es")]:
    got = (checks.detect_language(text) or {}).get("code")
    check(f"detects '{want}'", got == want, f"got {got}")
check("too little text gives no guess", checks.detect_language("Q1 Q2") is None)

log(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
sys.exit(1 if failures else 0)
