"""The agent loop: model plans with tool calls, tools do the work, repeat until it answers.

Yields events for the frontend: text, tool_started, tool_finished, file_created,
error, done.
"""

import json
import time

from core import llm, store, tools

MAX_STEPS = 8
MAX_TOKENS = 1024
TOOL_RESULT_CHARS = 12000   # read_document returns up to 10,000 characters of text

SYSTEM = """You are the iDocx assistant. The user uploads PDF files and asks you to work on them.

Rules:
- Use the tools to learn anything about a file. Never guess page counts, contents or file IDs.
- Refer to files by their file_id.
- If a request needs an operation no tool provides, say plainly that it is not available yet. Never claim to have done something a tool did not do.
- Only PDF files are supported.
- Keep replies short and factual.
- Never show tool names (like fix_accessibility) to the user; describe what you did or can do in plain words.
- When a step creates a new file, later steps work on that new file's ID.
- When the user later confirms more pages to remove, remove them from the LATEST file, passing the page numbers you showed them with numbering='original'."""


def _system_prompt(session: dict) -> str:
    lines, names = [], {}
    for fid in session["files"]:
        try:
            meta, _ = store.get_file(fid)
        except store.NotFound:
            continue
        names[fid] = meta["name"]
        # Say where each file came from: otherwise "the latest version" can mean a copy a tool made.
        if meta["source"] in ("upload", "test"):
            origin = "uploaded by the user"
        else:
            parents = ", ".join(f'"{names.get(p, p)}"' for p in meta["parents"])
            origin = f"made by {meta['source']} from {parents}"
        lines.append(f'- {fid}  "{meta["name"]}"  ({meta["pages"]} pages, {origin})')
    files = "\n".join(lines) if lines else "(none uploaded yet)"
    features = "\n\n".join(tools.PROMPTS)
    return f"{SYSTEM}\n\n{features}\n\nFiles in this session:\n{files}"


def _call_model(messages: list[dict]) -> dict:
    # temperature 0 keeps plans as repeatable as the server allows; runs still vary a little.
    return llm.chat(messages, tools=tools.schemas(), tool_choice="auto", temperature=0, max_tokens=MAX_TOKENS)


def run_turn(session: dict, user_text: str):
    # Things that happened outside the chat (e.g. the user approved a proposal in the
    # review card) reach the model as a note on their next message.
    notes = session.pop("notes", [])
    content = ("[" + " ".join(notes) + "]\n\n" + user_text) if notes else user_text
    session["messages"].append({"role": "user", "content": content})
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
            if out["ok"] and isinstance(out.get("result"), dict) and out["result"].get("proposal"):
                yield {"type": "proposal", "proposal": out["result"]["proposal"]}
            yield {"type": "tool_finished", "tool": name, "ok": out["ok"], "seconds": secs,
                   "result": out.get("result"), "error": out.get("error")}

            session["messages"].append({"role": "tool", "tool_call_id": call.get("id", ""),
                                        "content": json.dumps(out)[:TOOL_RESULT_CHARS]})
    else:
        yield {"type": "error", "message": f"stopped after {MAX_STEPS} model steps without a final answer"}

    stats["model_seconds"] = round(stats["model_seconds"], 1)
    stats["total_seconds"] = round(time.perf_counter() - started, 1)
    yield {"type": "done", "stats": stats}
