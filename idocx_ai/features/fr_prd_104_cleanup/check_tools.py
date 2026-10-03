"""Score the cleanup tools against input/ground_truth.json, without the model.

    .venv/bin/python features/fr_prd_104_cleanup/check_tools.py

Files the tools write, and report.txt, go to output/check_tools/ (cleared each run).
"""

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

import pymupdf  # noqa: E402

from core import pages, store  # noqa: E402
from core.pdfutil import ToolError  # noqa: E402
from features.fr_prd_104_cleanup import cleanup  # noqa: E402

INPUT = HERE / "input"
OUT = HERE / "output" / "check_tools"
shutil.rmtree(OUT, ignore_errors=True)
store.set_root(OUT / "files")

truth = json.loads((INPUT / "ground_truth.json").read_text())
failures = 0
report = []


def log(line=""):
    print(line)
    report.append(line)


def check(label, ok, detail=""):
    global failures
    failures += not ok
    log(f"  {'PASS' if ok else 'FAIL'}  {label}{'  -- ' + detail if detail else ''}")


for name, gt in truth.items():
    if name.startswith("_"):
        continue
    log(name)
    fid = store.save_file((INPUT / name).read_bytes(), name, gt["pages"], source="test")["id"]

    t = time.perf_counter()
    blank = cleanup.find_blank_pages(fid)
    dup = cleanup.find_duplicate_pages(fid)
    secs = time.perf_counter() - t

    want_blank = {p["page"] for p in gt["pages_detail"] if p.get("kind") == "blank"}
    want_dup = {p["page"]: p["duplicate_of"] for p in gt["pages_detail"] if p.get("kind") == "duplicate"}
    want_sparse = {p["page"] for p in gt["pages_detail"] if p.get("flag") == "nearly_empty"}
    must_keep = {p["page"] for p in gt["pages_detail"] if not p["remove"]}

    got_blank = {b["page"] for b in blank["blank_pages"]}
    got_dup = {d["page"]: d["duplicate_of"] for d in dup["duplicate_pages"]}
    got_sparse = {s["page"] for s in blank["nearly_empty_pages"]}

    check("blank pages found", got_blank == want_blank, f"want {sorted(want_blank)}, got {sorted(got_blank)}")
    check("duplicates found (and of the right page)", got_dup == want_dup, f"want {want_dup}, got {got_dup}")
    check("nearly empty pages flagged for the user", got_sparse == want_sparse,
          f"want {sorted(want_sparse)}, got {sorted(got_sparse)}")
    hit = (got_blank | set(got_dup)) & must_keep
    check("no real page proposed for removal", not hit, f"would wrongly remove {sorted(hit)}" if hit else "")

    removable = sorted(got_blank | set(got_dup))
    if removable:
        out = pages.remove_pages(fid, removable)
        check("remove_pages leaves the right count", out["pages_after"] == len(must_keep),
              f"{out['pages_before']} -> {out['pages_after']}, expected {len(must_keep)}")

    md = cleanup.strip_metadata(fid)
    if not gt["metadata_to_remove"]:
        check("nothing to strip: no new file written", md.get("nothing_to_remove") is True)
    else:
        _, path = store.get_file(md["new_file_id"])
        with pymupdf.open(path) as doc:
            left = {k: v for k, v in doc.metadata.items() if v and k not in ("format", "title")}
            check("metadata stripped and verified", not left and not doc.get_xml_metadata(),
                  f"removed {len(md['removed'])} item(s), kept {md['kept']}")
    log(f"  ({secs:.1f}s for blank + duplicate detection)")


def image_hashes(fid):
    _, path = store.get_file(fid)
    with pymupdf.open(path) as d:
        return [hashlib.sha256(d.extract_image(p.get_images()[0][0])["image"]).hexdigest() for p in d]


log("follow-up removal using original page numbers (cleanup_scanned.pdf)")
orig = store.save_file((INPUT / "cleanup_scanned.pdf").read_bytes(), "cleanup_scanned.pdf", 6, "test")["id"]
a = pages.remove_pages(orig, [2, 5])
a2 = cleanup.strip_metadata(a["new_file_id"])
b = pages.remove_pages(a2["new_file_id"], [4, 6], numbering="original")
src = image_hashes(orig)
check("after removing 2,5 then 'original' 4,6: original pages 1 and 3 remain",
      image_hashes(b["new_file_id"]) == [src[0], src[2]],
      f"new file holds original pages {b['new_file_holds_original_pages']}")
try:
    pages.remove_pages(a["new_file_id"], [5], numbering="original")
    check("asking for an already-removed page is refused", False, "it was not refused")
except ToolError as e:
    check("asking for an already-removed page is refused", "already removed" in str(e), str(e))

log(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
sys.exit(1 if failures else 0)
