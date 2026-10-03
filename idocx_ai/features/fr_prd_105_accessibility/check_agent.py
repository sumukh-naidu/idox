"""FR-PRD-105 with the model: score describe_images against the labels drawn in the
test images, then run the sidebar examples through the agent. Needs the model server.

    .venv/bin/python features/fr_prd_105_accessibility/check_agent.py

Files the agent creates, and report.txt, go to output/check_agent/ (cleared each run).
"""

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from core import agent, store  # noqa: E402
from features.fr_prd_105_accessibility import checks, images  # noqa: E402
from features.fr_prd_105_accessibility.tools import INFO  # noqa: E402

INPUT = HERE / "input"
OUT = HERE / "output" / "check_agent"
shutil.rmtree(OUT, ignore_errors=True)
store.set_root(OUT / "files")

truth = json.loads((INPUT / "ground_truth.json").read_text())
report, results = [], []


def log(line=""):
    print(line)
    report.append(line)


def upload(name):
    return store.save_file((INPUT / name).read_bytes(), name, truth[name]["pages"], source="test")["id"]


def alt_text_ok(want, text):
    text = (text or "").lower()
    return (all(k.lower() in text for k in want["all_of"])
            and (not want["any_of"] or any(k.lower() in text for k in want["any_of"])))


# Optional filter: check_agent.py identify  -> only examples whose request contains "identify"
only = [a.lower() for a in sys.argv[1:]]

if not only:
    log("describe_images on access_report.pdf (direct tool call)")
    desc = images.describe_images(upload("access_report.pdf"))
    for want, got in zip(truth["access_report.pdf"]["image_keywords"], desc["images"]):
        ok = alt_text_ok(want, got["suggested_alt_text"])
        results.append(ok)
        log(f"  {'PASS' if ok else 'FAIL'}  page {got['page']}: {got['suggested_alt_text']}")

for ex in INFO["examples"]:
    if only and not any(o in ex["prompt"].lower() for o in only):
        continue
    session = store.get_session(store.new_session())
    fid = upload(ex["file"])
    store.attach(session, fid)
    log("=" * 78)
    log(f"> {ex['prompt']}  ({ex['file']})")
    plan, created, reply, stats, errors, drafts = [], [], "", {}, [], []
    for ev in agent.run_turn(session, ex["prompt"]):
        if ev["type"] == "tool_started":
            plan.append(ev["tool"])
        elif ev["type"] == "tool_finished" and not ev["ok"]:
            errors.append(f"{ev['tool']}: {ev['error']}")
        elif ev["type"] == "tool_finished" and ev["tool"] in ("check_accessibility", "describe_images"):
            imgs = (ev["result"].get("suggested_alt_text") or {}).get("images") or ev["result"].get("images") or []
            drafts += [i["suggested_alt_text"] for i in imgs if i.get("suggested_alt_text")]
        elif ev["type"] == "file_created":
            created.append(ev["file"])
        elif ev["type"] == "text":
            reply = ev["text"]
        elif ev["type"] == "done":
            stats = ev["stats"]
    log(f"  plan: {' -> '.join(plan) or '(no tools)'}")
    for e in errors:
        log(f"  tool error: {e}")
    log(f"  reply: {' '.join(reply.split())[:320]}")
    log(f"  {stats.get('model_calls')} model calls, {stats.get('total_seconds')}s")

    p = ex["prompt"]
    if p.startswith("Check"):
        ok = plan[:1] == ["check_accessibility"] and not created
        detail = "checked without changing the file"
    elif p.startswith("Identify"):
        wants = truth["access_report.pdf"]["image_keywords"]
        ok = ("check_accessibility" in plan and len(drafts) == len(wants) and not created
              and all(alt_text_ok(w, d) for w, d in zip(wants, drafts)))
        detail = f"checked and drafted {len(drafts)} alt text(s) without changing the file"
    elif p.startswith("Help improve"):
        last = created[-1] if created else None
        after = {c["id"]: c["status"] for c in checks.check_accessibility(last["id"])["checks"]} if last else {}
        want = truth[ex["file"]]["after_fix"]
        ok = bool(last) and all(after.get(k) == v for k, v in want.items()) and "fixed tags" not in reply.lower()
        detail = f"new file {last['name']}: " + ", ".join(f"{k} {after.get(k)}" for k in want) if last else "no new file"
    else:
        ok, detail = False, "no scoring rule"
    results.append(ok)
    log(f"  {'PASS' if ok else 'FAIL'}  {detail}")

log(f"\n{sum(results)}/{len(results)} passed")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
