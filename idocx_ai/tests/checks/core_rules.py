"""Check the shared rules every AI feature follows (brief sections 1.1 and 3), without the model.

    uv run python tests/checks/core_rules.py
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core import agent, rules, store  # noqa: E402

OUT = ROOT / "tests" / "output" / "core_rules"
shutil.rmtree(OUT, ignore_errors=True)
store.set_root(OUT / "files")
rules.set_run_log(OUT / "runs.jsonl")
failures = 0


def check(label, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  -- ' + detail if detail else ''}")


print("C2 confidence, C3 not found")
check("84 is low confidence, 85 is not", rules.confidence(84)["low_confidence"] and not rules.confidence(85)["low_confidence"])
check("scores are whole numbers from 0 to 100", rules.confidence(91.6) == {"confidence": 92, "low_confidence": False})
for bad in (-1, 101, None, "90", True):
    try:
        rules.confidence(bad)
        check(f"confidence {bad!r} is refused", False)
    except ValueError:
        check(f"confidence {bad!r} is refused", True)
nf = rules.not_found()
check("a missing value is 'Not found' with confidence 0", nf["value"] == "Not found" and nf["confidence"] == 0 and nf["status"] == "not_found")

print("C6 usage, C7 run log")
pdf = (ROOT / "samples" / "fr_ai_06_qa" / "service_report.pdf").read_bytes()
a = store.save_file(pdf, "service_report.pdf", 3, source="upload")["id"]
b = store.save_file(pdf, "other.pdf", 3, source="upload")["id"]
check("pages processed: each file counted once", rules.pages_processed({"file_id": a, "x": [a, b]}) == 6)
check("pages processed: unknown ids and plain text count nothing", rules.pages_processed({"q": "f_000000000000", "t": "hello"}) == 0)
check("usage is pages, calls and tokens", rules.usage(2, 100, 20, 3) == {"pages": 3, "model_calls": 2, "tokens": 120})

script = []
agent._call_model = lambda messages: script.pop(0)
def reply(text=None, call=None):
    msg = {"content": text or ""}
    if call:
        msg["tool_calls"] = [{"id": "c1", "function": {"name": call[0], "arguments": json.dumps(call[1])}}]
    return {"choices": [{"message": msg}], "usage": {"prompt_tokens": 50, "completion_tokens": 10}}

sess = store.get_session(store.new_session("tester-7")); store.attach(sess, a)
script[:] = [reply(call=("ask_document", {"file_id": a, "question": "SECRET QUESTION about the contract number"})),
             reply("The contract number is SR-2026-0457 (page 1).")]
events = list(agent.run_turn(sess, "What is the contract number? SECRET QUESTION"))
done = events[-1]
check("the done event carries usage in pages, calls and tokens",
      done["usage"] == {"pages": 3, "model_calls": 2, "tokens": 120}, str(done["usage"]))
lines = [json.loads(x) for x in (OUT / "runs.jsonl").read_text().splitlines()]
check("one log line per run", len(lines) == 1)
line = lines[0]
check("the line says who, which feature, which document and when",
      line["who"] == "tester-7" and line["feature"] == "FR-AI-06" and line["documents"][0]["id"] == a and line["when"], str(line))
raw = (OUT / "runs.jsonl").read_text()
check("the log holds no question, answer or document text",
      "SECRET" not in raw and "SR-2026" not in raw and "contract" not in raw.lower())

print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
