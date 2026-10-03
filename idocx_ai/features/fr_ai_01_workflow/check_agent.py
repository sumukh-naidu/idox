"""Run the FR-AI-01 sidebar examples through the model and score what actually
comes out. Needs the model server running.

    .venv/bin/python features/fr_ai_01_workflow/check_agent.py

Files the agent creates, and report.txt, go to output/check_agent/ (cleared each run).
"""

import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

import pymupdf  # noqa: E402

from core import agent, store  # noqa: E402
from features.fr_ai_01_workflow.tools import INFO  # noqa: E402

INPUT = HERE / "input"
OUT = HERE / "output" / "check_agent"
shutil.rmtree(OUT, ignore_errors=True)
store.set_root(OUT / "files")

truth = json.loads((INPUT / "ground_truth.json").read_text())
report = []


def log(line=""):
    print(line)
    report.append(line)


def markers(fid):
    _, path = store.get_file(fid)
    with pymupdf.open(path) as d:
        return [(m.group(1) if (m := re.search(r"\[marker (\w+)\]", p.get_text())) else None) for p in d]


def rotations(fid):
    _, path = store.get_file(fid)
    with pymupdf.open(path) as d:
        return [p.rotation for p in d]


def run(example):
    session = store.get_session(store.new_session())
    uploaded = {}
    for name in [example["file"], *example.get("also", [])]:
        meta = store.save_file((INPUT / name).read_bytes(), name, truth[name]["pages"], source="test")
        store.attach(session, meta["id"])
        uploaded[name] = meta
    log("=" * 78)
    log(f"> {example['prompt']}")
    run = {"plan": [], "created": [], "verified": [], "errors": [], "reply": "", "stats": {}}
    for ev in agent.run_turn(session, example["prompt"]):
        if ev["type"] == "tool_started":
            run["plan"].append(ev["tool"])
        elif ev["type"] == "tool_finished":
            if not ev["ok"]:
                run["errors"].append(f"{ev['tool']}: {ev['error']}")
            elif ev["tool"] == "verify_tasks":
                run["verified"] = ev["result"]["verified"]
        elif ev["type"] == "file_created":
            run["created"].append(ev["file"])
        elif ev["type"] == "text":
            run["reply"] = ev["text"]
        elif ev["type"] == "done":
            run["stats"] = ev["stats"]
    log(f"  plan: {' -> '.join(run['plan']) or '(no tools)'}")
    for e in run["errors"]:
        log(f"  tool error (the model may have recovered): {e}")
    log(f"  reply: {' '.join(run['reply'].split())[:280]}")
    log(f"  {run['stats'].get('model_calls')} model calls, {run['stats'].get('total_seconds')}s")
    return uploaded, run


def deadlines(verified):
    return {" ".join(str(v.get("deadline") or "").split()) for v in verified}


def mentioned(date, got):
    """A verified deadline may keep its wording ("before 31 Oct 2026"); the date must be inside it."""
    return any(date in g for g in got)


# Optional filter: check_agent.py deadlines  -> only examples whose request contains "deadlines"
only = [a.lower() for a in sys.argv[1:]]
results = []
for ex in INFO["examples"]:
    if only and not any(o in ex["prompt"].lower() for o in only):
        continue
    uploaded, r = run(ex)
    created, last = r["created"], (r["created"][-1] if r["created"] else None)
    p = ex["prompt"]
    if p.startswith("Merge"):
        merged = next((f for f in created if f["parents"] == [uploaded["merge_a.pdf"]["id"], uploaded["merge_b.pdf"]["id"]]), None)
        ok = bool(last and merged and markers(last["id"]) == ["A1", "A2", "A3", "B1", "B3"]
                  and last["size"] < merged["size"] * 0.7)
        detail = (f"final {last['name']}: {markers(last['id'])}, {merged['size'] // 1024} KB merged -> "
                  f"{last['size'] // 1024} KB") if last and merged else "no merged/final file"
    elif p.startswith("Split"):
        got = sorted((f["name"], f["pages"]) for f in created)
        want = sorted([("Definitions.pdf", 3), ("Payment Terms.pdf", 3), ("Schedules.pdf", 6)])
        ok, detail = got == want, f"files {got}"
    elif "tasks and deadlines" in p or "deadlines are mentioned" in p:
        gt = truth["project_memo.pdf" if "tasks" in p else "scanned_notice.pdf"]
        got = deadlines(r["verified"])
        ok = (all(mentioned(d, got) for d in gt["deadlines"])
              and not any(mentioned(d, got) for d in gt["not_deadlines"]))
        detail = f"verified deadlines {sorted(got)}; expected {gt['deadlines']}, must not include {gt['not_deadlines']}"
    elif "summary" in p:
        ok = "read_document" in r["plan"] and len(r["reply"]) > 80 and not created
        detail = "read the document first and answered without creating files" if ok else f"plan {r['plan']}"
    elif p.startswith("Extract"):
        ok = bool(last and markers(last["id"]) == ["C4", "C5", "C6"] and rotations(last["id"]) == [90, 0, 0])
        detail = f"final {markers(last['id'])} rotations {rotations(last['id'])}" if last else "no file"
    else:
        ok, detail = False, "no scoring rule for this example"
    results.append(ok)
    log(f"  {'PASS' if ok else 'FAIL'}  {detail}")

log(f"\n{sum(results)}/{len(results)} examples produced the expected result")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
