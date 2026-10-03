"""The agent loop: model plans with tool calls, tools do the work, repeat until it answers.

Yields events for the frontend: text, tool_started, tool_finished, file_created,
error, done.
"""

import json
import os
import threading
import time
import urllib.request

from core import store, tools

LLAMA = os.environ.get("LLAMA_ENDPOINT", "http://localhost:8080")
MAX_STEPS = 8
MAX_TOKENS = 1024
TOOL_RESULT_CHARS = 6000

# One model request at a time: llama-server runs a single slot, shared with IDP.
_model_lock = threading.Lock()

SYSTEM = """You are the iDocx assistant. The user uploads PDF files and asks you to work on them.

Rules:
- Use the tools to learn anything about a file. Never guess page counts, contents or file IDs.
- Refer to files by their file_id.
- If a request needs an operation no tool provides, say plainly that it is not available yet. Never claim to have done something a tool did not do.
- Only PDF files are supported.
- Keep replies short and factual.
- When a step creates a new file, later steps work on that new file's ID.
- When the user later confirms more pages to remove, remove them from the LATEST file, passing the page numbers you showed them with numbering='original'."""


def _system_prompt(session: dict) -> str:
    lines = []
    for fid in session["files"]:
        try:
            meta, _ = store.get_file(fid)
            lines.append(f'- {fid}  "{meta["name"]}"  ({meta["pages"]} pages)')
        except store.NotFound:
            continue
    files = "\n".join(lines) if lines else "(none uploaded yet)"
    features = "\n\n".join(tools.PROMPTS)
    return f"{SYSTEM}\n\n{features}\n\nFiles in this session:\n{files}"


def _call_model(messages: list[dict]) -> dict:
    body = json.dumps({
        "messages": messages,
        "tools": tools.schemas(),
        "tool_choice": "auto",
        "temperature": 0,          # same request, same plan: test runs must be reproducible
        "max_tokens": MAX_TOKENS,
    }).encode()
    req = urllib.request.Request(f"{LLAMA}/v1/chat/completions", body,
                                 {"Content-Type": "application/json"})
    with _model_lock, urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def model_name() -> str | None:
    try:
        with urllib.request.urlopen(f"{LLAMA}/v1/models", timeout=3) as r:
            return json.load(r)["data"][0]["id"].split("/")[-1].removesuffix(".gguf")
    except Exception:
        return None


def run_turn(session: dict, user_text: str):
    session["messages"].append({"role": "user", "content": user_text})
    stats = {"model_calls": 0, "model_seconds": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
             "repeated_calls": 0}
    started = time.perf_counter()
    seen: dict[str, dict] = {}

    for _ in range(MAX_STEPS):
        messages = [{"role": "system", "content": _system_prompt(session)}] + session["messages"]
        t = time.perf_counter()
        try:
            resp = _call_model(messages)
        except Exception as e:
            yield {"type": "error", "message": f"model server unreachable or failed: {e}"}
            break
        stats["model_calls"] += 1
        stats["model_seconds"] += time.perf_counter() - t
        usage = resp.get("usage") or {}
        stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
        stats["completion_tokens"] += usage.get("completion_tokens", 0)

        msg = resp["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls") or []

        entry = {"role": "assistant", "content": content}
        if calls:
            entry["tool_calls"] = calls
        session["messages"].append(entry)

        if content:
            yield {"type": "text", "text": content}
        if not calls:
            break

        for call in calls:
            name = call["function"]["name"]
            raw = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = None
            yield {"type": "tool_started", "tool": name, "args": args if args is not None else raw}

            # Greedy decoding can re-issue an identical call instead of answering.
            key = f"{name}:{json.dumps(args, sort_keys=True)}"
            t = time.perf_counter()
            if key in seen:
                stats["repeated_calls"] += 1
                out = {**seen[key], "note": "You already made this exact call in this turn. "
                                            "Use this result and do not call it again."}
            else:
                out = tools.run(name, args) if args is not None else \
                    {"ok": False, "error": f"arguments were not valid JSON: {raw[:200]}"}
                seen[key] = out
            secs = round(time.perf_counter() - t, 2)

            for f in (out.get("result") or {}).get("files_created", []) if out["ok"] else []:
                store.attach(session, f["id"])
                yield {"type": "file_created", "file": f}
            yield {"type": "tool_finished", "tool": name, "ok": out["ok"], "seconds": secs,
                   "result": out.get("result"), "error": out.get("error")}

            session["messages"].append({"role": "tool", "tool_call_id": call.get("id", ""),
                                        "content": json.dumps(out)[:TOOL_RESULT_CHARS]})
    else:
        yield {"type": "error", "message": f"stopped after {MAX_STEPS} model steps without a final answer"}

    stats["model_seconds"] = round(stats["model_seconds"], 1)
    stats["total_seconds"] = round(time.perf_counter() - started, 1)
    yield {"type": "done", "stats": stats}
