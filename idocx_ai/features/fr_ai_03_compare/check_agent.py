"""Run the FR-AI-03 sidebar examples through the model and score them. Needs the model server.

    .venv/bin/python features/fr_ai_03_compare/check_agent.py [filter words]

Files the agent creates, and report.txt, go to output/check_agent/ (cleared each run).
"""

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from core import agent, store  # noqa: E402
from features.fr_ai_03_compare.tools import INFO  # noqa: E402

INPUT = HERE / "input"
OUT = HERE / "output" / "check_agent"
shutil.rmtree(OUT, ignore_errors=True)
store.set_root(OUT / "files")

truth = json.loads((INPUT / "ground_truth.json").read_text())
report, results = [], []
only = [a.lower() for a in sys.argv[1:]]


def log(line=""):
    print(line)
    report.append(line)


for ex in INFO["examples"]:
    if only and not any(o in ex["prompt"].lower() for o in only):
        continue
    session = store.get_session(store.new_session())
    ids = {}
    for name in [ex["file"], *ex.get("also", [])]:
        ids[name] = store.save_file((INPUT / name).read_bytes(), name, truth[name]["pages"], source="test")["id"]
        store.attach(session, ids[name])
    log("=" * 78)
    log(f"> {ex['prompt']}  ({' + '.join(ids)})")
    plan, compared, explained, created, reply, stats = [], None, None, [], "", {}
    for ev in agent.run_turn(session, ex["prompt"]):
        if ev["type"] == "tool_started":
            plan.append(ev["tool"])
            if ev["tool"] == "compare_documents":
                compared = ev["args"]
        elif ev["type"] == "tool_finished" and ev["ok"] and ev["tool"] == "check_change_explanations":
            explained = ev["result"]
        elif ev["type"] == "tool_finished" and not ev["ok"]:
            log(f"  tool error: {ev['tool']}: {ev['error']}")
        elif ev["type"] == "file_created":
            created.append(ev["file"])
        elif ev["type"] == "text":
            reply = ev["text"]
        elif ev["type"] == "done":
            stats = ev["stats"]
    log(f"  plan: {' -> '.join(plan) or '(no tools)'}")
    log(f"  reply: {' '.join(reply.split())[:420]}")
    log(f"  {stats.get('model_calls')} model calls, {stats.get('total_seconds')}s")

    newer = ex.get("also", [None])[0]
    order_ok = bool(compared) and compared.get("old_file_id") == ids[ex["file"]] and compared.get("new_file_id") == ids[newer]
    low = reply.lower()
    if "summary of the meaningful" in ex["prompt"]:
        v = {x["change_id"]: x["significance"] for x in (explained or {}).get("verified", [])}
        material = {x["where"].split("(")[1].rstrip(")").split(")")[0] for x in (explained or {}).get("verified", [])
                    if x["significance"] == "material"}
        ok = order_ok and len(v) == len(truth["changes"]) and {"Payment", "Liability"} <= material
        detail = f"{len(v)} explanations verified; material: {sorted(material)}"
    elif "scanned" in newer:
        ok = order_ok and "ocr" in low and "45" in reply
        detail = "older file first, OCR caveat given, payment change reported"
    else:
        named = [t for t in ("Payment", "Liability", "Exclusivity", "Data Protection", "Termination") if t.lower() in low]
        ok = order_ok and len(named) == 5 and bool(created)
        detail = f"older file first: {order_ok}; clauses named: {named}; marked-up copy: {bool(created)}"
    results.append(ok)
    log(f"  {'PASS' if ok else 'FAIL'}  {detail}")

log(f"\n{sum(results)}/{len(results)} examples produced the expected result")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
