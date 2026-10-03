"""Run the requirement doc's FR-PRD-104 example requests through the model and
check what actually ends up in the output file. Needs the model server running.

    .venv/bin/python features/fr_prd_104_cleanup/check_agent.py

Files the agent creates, and report.txt, go to output/check_agent/ (cleared each run).
"""

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

import pymupdf  # noqa: E402

from core import agent, store  # noqa: E402

INPUT = HERE / "input"
OUT = HERE / "output" / "check_agent"
shutil.rmtree(OUT, ignore_errors=True)
store.set_root(OUT / "files")

truth = json.loads((INPUT / "ground_truth.json").read_text())

# (file, conversation turns, original pages the final file should hold)
CASES = [
    ("cleanup_digital.pdf", ["Clean this PDF by removing blank pages and duplicate pages."],
     [1, 2, 4, 6, 9, 11, 12]),
    ("cleanup_scanned.pdf", ["Clean up this scanned document and remove unnecessary metadata where supported.",
                             "Yes, remove pages 4 and 6 as well."],
     [1, 3]),
    ("cleanup_control.pdf", ["Automatically identify and apply the available document cleanup actions."],
     [1, 2, 3]),
    ("cleanup_digital.pdf", ["Automatically identify and apply the available document cleanup actions."],
     [1, 2, 4, 6, 9, 11, 12]),
]

report = []


def log(line=""):
    print(line)
    report.append(line)


passed = 0
for name, turns, want_pages in CASES:
    gt = truth[name]
    session = store.get_session(store.new_session())
    fid = store.save_file((INPUT / name).read_bytes(), name, gt["pages"], source="test")["id"]
    store.attach(session, fid)
    log("=" * 78)
    log(name)
    latest = None
    for turn in turns:
        log(f"> {turn}")
        plan, reply, stats = [], "", {}
        for ev in agent.run_turn(session, turn):
            if ev["type"] == "tool_started":
                args = ", ".join(f"{k}={v}" for k, v in (ev["args"] or {}).items() if k != "file_id")
                plan.append(f"{ev['tool']}({args})")
            elif ev["type"] == "tool_finished" and not ev["ok"]:
                plan.append(f"  ! {ev['tool']} failed: {ev['error']}")
            elif ev["type"] == "file_created":
                latest = ev["file"]
            elif ev["type"] == "text":
                reply = ev["text"]
            elif ev["type"] == "error":
                log(f"  ERROR {ev['message']}")
            elif ev["type"] == "done":
                stats = ev["stats"]
        log("  plan: " + "\n        ".join(plan or ["(no tools called)"]))
        log(f"  reply: {' '.join(reply.split())[:300]}")
        log(f"  {stats.get('model_calls')} model calls, {stats.get('total_seconds')}s")

    if latest:
        meta, path = store.get_file(latest["id"])
        held = [p for _, p in meta["page_origin"]]
        with pymupdf.open(path) as out:
            md = {k: v for k, v in out.metadata.items() if v and k not in ("format", "title")}
    else:
        held, md = list(range(1, gt["pages"] + 1)), None
    ok = held == want_pages
    passed += ok
    log(f"  {'PASS' if ok else 'FAIL'}  final file holds original pages {held}, expected {want_pages}"
        f"; metadata left: {md if md is not None else 'n/a (no new file)'}")

log(f"\n{passed}/{len(CASES)} conversations ended with the expected pages")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
