"""The rules every AI feature follows (brief IMPACTO-IDOCX-FSD-2026-010, section 1.1), in one place.

C2 confidence 0-100, "Low confidence" below 85.   C3 "Not found", never a guess.
C6 usage returned with each response.             C7 every run logged: who, which document, which feature, when.
C8 structured output.

The log holds metadata only: never document text, questions or answers.
"""

import json
import time
from pathlib import Path

from core import store

LOW_CONFIDENCE_BELOW = 85
NOT_FOUND = "Not found"
NOT_FOUND_IN_DOCUMENT = "Not found in the document"
RUN_LOG = Path(__file__).resolve().parent.parent / "data" / "runs.jsonl"


def set_run_log(path: Path) -> None:
    """Point the run log somewhere else, e.g. tests/output/ during checks."""
    global RUN_LOG
    RUN_LOG = Path(path)


def confidence(score: float) -> dict:
    """C2: a whole 0-100 score and whether iDocx must show "Low confidence" and ask the user to check."""
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
        raise ValueError(f"confidence must be a number from 0 to 100, got {score!r}")
    n = round(score)
    return {"confidence": n, "low_confidence": n < LOW_CONFIDENCE_BELOW}


def not_found() -> dict:
    """C3: a missing value is reported as "Not found" with confidence 0, never filled in."""
    return {"value": NOT_FOUND, "status": "not_found", **confidence(0)}


def pages_processed(args) -> int:
    """C6: pages in the files a tool call worked on (each file once); the unit the brief suggests (Q5)."""
    ids: set[str] = set()

    def collect(value):
        if isinstance(value, str):
            if store.FILE_ID.match(value):
                ids.add(value)
        elif isinstance(value, (list, tuple)):
            for v in value:
                collect(v)
        elif isinstance(value, dict):
            for v in value.values():
                collect(v)

    collect(args)
    total = 0
    for fid in ids:
        try:
            total += int(store.get_file(fid)[0].get("pages") or 0)
        except store.NotFound:
            pass
    return total


def usage(model_calls: int, prompt_tokens: int, completion_tokens: int, pages: int) -> dict:
    """C6: what a response used, in pages, tokens and calls, so iDocx can count it against the quota."""
    return {"pages": pages, "model_calls": model_calls,
            "tokens": prompt_tokens + completion_tokens}


def log_run(user: str, feature: str, tool: str, args, ok: bool, seconds: float) -> None:
    """C7: one line per run. Document ids and names only; no text of any document, question or answer."""
    files = []
    def collect(value):
        if isinstance(value, str) and store.FILE_ID.match(value):
            try:
                files.append({"id": value, "name": store.get_file(value)[0]["name"]})
            except store.NotFound:
                files.append({"id": value})
        elif isinstance(value, (list, tuple)):
            for v in value:
                collect(v)
        elif isinstance(value, dict):
            for v in value.values():
                collect(v)
    collect(args)
    line = {"when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "who": user or "unknown", "feature": feature, "tool": tool,
            "documents": files, "ok": ok, "seconds": round(seconds, 2)}
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
