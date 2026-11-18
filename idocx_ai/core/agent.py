"""The agent loop: model plans with tool calls, tools do the work, repeat until it answers.

Yields events for the frontend: text, tool_started, tool_finished, file_created,
error, done.
"""

import json
import os
import time

from core import llm, rules, store, tools

MAX_STEPS = 8
MODEL_TIMEOUT = int(os.environ.get("MODEL_TIMEOUT_SECONDS", "180"))   # C5: every model call has a time limit (target TBC)
MAX_TOKENS = 1024
# A tool result is cut here before the model sees it. The model's context is 32k tokens (about 100,000
# characters) shared by the prompt, the whole chat and 1,024 reply tokens, so keep this well under that.
TOOL_RESULT_CHARS = 36000   # ask_document returns up to 30,000 characters of page text

SYSTEM = """You are the iDocx assistant. The user uploads PDF files and asks you to work on them.

Rules:
- Use the tools to learn anything about a file. Never guess page counts, contents or file IDs.
- Refer to files by their file_id.
- If a request needs an operation no tool provides, say plainly that it is not available yet. Never claim to have done something a tool did not do.
- Files are PDFs, except Excel workbooks (.xlsx), which can only be converted to Word.
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
        kind = f"{meta['pages']} pages" if meta.get("ext", "pdf") == "pdf" else f"{meta['ext']} file"
        lines.append(f'- {fid}  "{meta["name"]}"  ({kind}, {origin})')
    files = "\n".join(lines) if lines else "(none uploaded yet)"
    features = "\n\n".join(tools.PROMPTS)
    return f"{SYSTEM}\n\n{features}\n\nFiles in this session:\n{files}"


def _call_model(messages: list[dict]) -> dict:
    # temperature 0 keeps plans as repeatable as the server allows; runs still vary a little.
    return llm.chat(messages, tools=tools.schemas(), tool_choice="auto", temperature=0, max_tokens=MAX_TOKENS,
                    timeout=MODEL_TIMEOUT)


def run_turn(session: dict, user_text: str):
    # Things that happened outside the chat (e.g. the user approved a proposal in the
    # review card) reach the model as a note on their next message.
    notes = session.pop("notes", [])
    content = ("[" + " ".join(notes) + "]\n\n" + user_text) if notes else user_text
    session["messages"].append({"role": "user", "content": content})
    stats = {"model_calls": 0, "model_seconds": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
             "repeated_calls": 0, "pages_processed": 0}
    started = time.perf_counter()
    seen: dict[str, dict] = {}
    turn_calls: list[tuple] = []   # (tool, args, result) so far this turn, for answer checks
    earlier = list(session.setdefault("tool_log", []))   # the same, from earlier turns of this chat
    retried = False

    for _ in range(MAX_STEPS):
        messages = [{"role": "system", "content": _system_prompt(session)}] + session["messages"]
        t = time.perf_counter()
        try:
            resp = _call_model(messages)
        except Exception as e:
            reason = (f"the model did not answer within {MODEL_TIMEOUT} seconds"
                      if "timed out" in str(e).lower() else f"model server unreachable or failed: {e}")
            yield {"type": "error", "message": reason}
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

        if not calls:
            # A feature can check the final answer against what its tools returned: ask the model to try
            # once more (e.g. it never read the document), or attach a verdict for the user to see.
            verdict = tools.check_answer(content, turn_calls, bool(session["files"]), user_text, earlier)
            if verdict and verdict.get("retry") and not retried:
                retried = True
                session["messages"].pop()   # drop the unchecked answer; the model answers again
                session["messages"].append({"role": "user", "content": verdict["retry"]})
                yield {"type": "validation", "status": "retry", "problems": verdict.get("problems", [])}
                continue
            if content:
                yield {"type": "text", "text": content}
            if verdict and verdict.get("validation"):
                yield {"type": "validation", **verdict["validation"]}
            break
        if content:
            yield {"type": "text", "text": content}

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
                nested = llm.TOOL_USAGE = {"model_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
                try:
                    out = tools.run(name, args) if args is not None else \
                        {"ok": False, "error": f"arguments were not valid JSON: {raw[:200]}"}
                finally:
                    llm.TOOL_USAGE = None
                for k, v in nested.items():       # the tool's own model calls count too (C6)
                    stats[k] = stats.get(k, 0) + v
                seen[key] = out
                pages = rules.pages_processed(args) if args is not None else 0
                stats["pages_processed"] += pages
                rules.log_run(session.get("user", "unknown"), tools.FEATURE_OF.get(name, "unknown"), name, args,
                              out["ok"], time.perf_counter() - t)
            turn_calls.append((name, args, out))
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

    session["tool_log"] = (session["tool_log"] + turn_calls)[-20:]
    stats["model_seconds"] = round(stats["model_seconds"], 1)
    stats["total_seconds"] = round(time.perf_counter() - started, 1)
    yield {"type": "done", "stats": stats,
           "usage": rules.usage(stats["model_calls"], stats["prompt_tokens"], stats["completion_tokens"],
                                stats["pages_processed"])}
