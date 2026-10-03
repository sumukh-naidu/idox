"""Check the FR-AI-05 detectors against input/ground_truth.json, without the model.

    .venv/bin/python features/fr_ai_05_redaction/check_tools.py

Files the tools write, and report.txt, go to output/check_tools/ (cleared each run).
"""

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from core import store  # noqa: E402
import pymupdf  # noqa: E402

from core import proposals  # noqa: E402
from features.fr_ai_05_redaction.find import find_sensitive_info  # noqa: E402
from features.fr_ai_05_redaction.scan import scan_document  # noqa: E402

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


def norm(v):
    return re.sub(r"[\s\-]", "", v).lower()


def matches(expected, found_value):
    """Expected values are plain text, or a sha256 fingerprint for fake secrets."""
    if isinstance(expected, dict):
        return hashlib.sha256(norm(found_value).encode()).hexdigest() == expected["sha256"]
    return norm(expected) in norm(found_value)


for name in ("redaction_form.pdf", "redaction_scanned.pdf"):
    gt = truth[name]
    log(name)
    fid = store.save_file((INPUT / name).read_bytes(), name, gt["pages"], source="test")["id"]
    items, ocr, _ = scan_document(fid, use_model=False)
    for page, expected in gt["expected"].items():
        got = [i for i in items if i["page"] == int(page)]
        missing = [(k, v) for k, v in expected if not any(i["kind"] == k and matches(v, i["value"]) for i in got)]
        extra = [i for i in got if not any(i["kind"] == k and matches(v, i["value"]) for k, v in expected)]
        check(f"page {page}: every planted item found with the right type",
              not missing, f"{len(expected) - len(missing)}/{len(expected)} found"
              + (f"; missing {[k for k, _ in missing]}" if missing else ""))
        check(f"page {page}: nothing else flagged", not extra,
              "; ".join(f"{i['kind']} {i['masked']}" for i in extra) if extra else "")
    unlocated = [i["kind"] for i in items if not i["located"]]
    check("every item located on the page (needed to redact it)", not unlocated, f"unlocated: {unlocated}" if unlocated else "")
    flagged = " ".join(norm(i["value"]) for i in items)
    hit = [d for d in gt.get("must_not_flag", []) if norm(d) in flagged]
    if "must_not_flag" in gt:
        check("no decoy flagged (failed checksums, no context, plain words)", not hit, f"wrongly flagged: {hit}" if hit else
              f"{len(gt['must_not_flag'])} decoys ignored")
    leaks = [i["kind"] for i in items if norm(i["value"]) in norm(i["masked"])]
    check("masked values never reveal the full value", not leaks, f"leaks: {leaks}" if leaks else "")
    if gt.get("text_layer") is False:
        check("read by OCR", ocr == [1], f"OCR pages {ocr}")

log("proposal and preview (rules only; names/addresses need the model)")
fid = store.save_file((INPUT / "redaction_form.pdf").read_bytes(), "redaction_form.pdf", 2, source="test")["id"]
r = find_sensitive_info(fid, use_model=False)
p = proposals.get(r["proposal_id"])
check("a pending proposal holds every item; raw values only in its private part",
      p["status"] == "pending" and len(p["items"]) == r["found"] == 23
      and all("value" not in i for i in r["items"]) and set(p["private"]) == {i["id"] for i in p["items"]},
      f"{r['found']} items: {r['by_category']}")
_, path = store.get_file(r["files_created"][0]["id"])
with pymupdf.open(path) as d:
    marks = sum(1 for pg in d for a in pg.annots() if a.type[1] == "Highlight")
want = sum(len(p["private"][i]["rects"]) for i in p["private"])
check("preview highlights every located item", marks == want, f"{marks} highlights")
_, orig = store.get_file(fid)
with pymupdf.open(orig) as d:
    check("the original file is untouched", not any(True for pg in d for _ in pg.annots()))

log("applying an approved proposal")
from core.text import textpage  # noqa: E402
import features.fr_ai_05_redaction.tools  # noqa: E402,F401  (registers the redaction applier)


def squash(s):
    return re.sub(r"\s+", "", s).lower()


def text_of(fid, ocr=False):
    _, path = store.get_file(fid)
    with pymupdf.open(path) as d:
        out = []
        for pg in d:
            tp = pg.get_textpage_ocr(dpi=200, full=True, tessdata=pymupdf.get_tessdata()) if ocr else None
            out.append(pg.get_text(textpage=tp))
        return " ".join(out)


r = find_sensitive_info(fid, use_model=False)
p = proposals.get(r["proposal_id"])
res = proposals.approve(p["id"], [i["id"] for i in p["items"]])
after = squash(text_of(res["file"]["id"]))
left = [i for i in p["items"] if squash(p["private"][i["id"]]["value"]) in after]
kept = [d for d in truth["redaction_form.pdf"]["must_not_flag"] if squash(d) in after]
check("approve all: every value gone from the text, verified", not left and res["redacted"] == 23,
      res["verified"])
check("text next to the redactions is untouched (the decoys are all still there)",
      len(kept) == len(truth["redaction_form.pdf"]["must_not_flag"]), f"{len(kept)} decoys still readable")
check("same page count", res["file"]["pages"] == 2)
try:
    proposals.approve(p["id"], [p["items"][0]["id"]])
    check("approving twice is refused", False)
except ValueError as e:
    check("approving twice is refused", "already applied" in str(e), str(e))

r2 = find_sensitive_info(fid, use_model=False)
p2 = proposals.get(r2["proposal_id"])
contact = [i["id"] for i in p2["items"] if i["category"] == "contact"]
res2 = proposals.approve(p2["id"], contact)
after2 = squash(text_of(res2["file"]["id"]))
gone = all(squash(p2["private"][i]["value"]) not in after2 for i in contact)
others = all(squash(p2["private"][i["id"]]["value"]) in after2 for i in p2["items"]
             if i["id"] not in contact and i["kind"] != "private key")
check("approve some: only the chosen items are redacted", gone and others and res2["left_unredacted"] == 20,
      f"{res2['redacted']} redacted, {res2['left_unredacted']} left")

r3 = find_sensitive_info(fid, use_model=False)
proposals.reject(r3["proposal_id"])
try:
    proposals.approve(r3["proposal_id"], [r3["items"][0]["id"]])
    check("approving after a reject is refused", False)
except ValueError as e:
    check("approving after a reject is refused", "already rejected" in str(e), str(e))

sid = store.save_file((INPUT / "redaction_scanned.pdf").read_bytes(), "redaction_scanned.pdf", 1, source="test")["id"]
r4 = find_sensitive_info(sid, use_model=False)
p4 = proposals.get(r4["proposal_id"])
res4 = proposals.approve(p4["id"], [i["id"] for i in p4["items"]])
ocr_after = squash(text_of(res4["file"]["id"], ocr=True))
readable = [i["kind"] for i in p4["items"] if squash(p4["private"][i["id"]]["value"]) in ocr_after]
check("scan: every box blacked out, and OCR of the redacted page finds none of the values",
      not readable and res4["redacted"] == len(p4["items"]),
      f"{res4['redacted']} items; still readable by OCR: {readable or 'none'}")

log(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
sys.exit(1 if failures else 0)
