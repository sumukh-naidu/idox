"""The one connection to the model server, shared by the agent loop and by tools
that call the model themselves (e.g. describing an image)."""

import base64
import json
import os
import threading
import urllib.request

LLAMA = os.environ.get("LLAMA_ENDPOINT", "http://localhost:8080")

# One request at a time: llama-server runs a single slot, shared with IDP. Tools run
# between the agent's model calls, never inside one, so a tool may take the lock too.
_lock = threading.Lock()


# While the agent runs a tool, the model calls the tool itself makes are added here, so usage (C6) counts them.
TOOL_USAGE: dict | None = None


def chat(messages: list[dict], timeout: int = 600, **options) -> dict:
    body = json.dumps({"messages": messages, **options}).encode()
    req = urllib.request.Request(f"{LLAMA}/v1/chat/completions", body, {"Content-Type": "application/json"})
    with _lock, urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    record_usage(resp)
    return resp


def record_usage(resp: dict) -> None:
    """Add one response's usage to the running tool's total, if a tool is running (see TOOL_USAGE)."""
    if TOOL_USAGE is not None:
        u = resp.get("usage") or {}
        TOOL_USAGE["model_calls"] += 1
        TOOL_USAGE["prompt_tokens"] += u.get("prompt_tokens", 0)
        TOOL_USAGE["completion_tokens"] += u.get("completion_tokens", 0)


def ask_about_image(prompt: str, png: bytes, max_tokens: int = 150) -> str:
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    r = chat([{"role": "user", "content": [{"type": "text", "text": prompt},
                                           {"type": "image_url", "image_url": {"url": url}}]}],
             temperature=0, max_tokens=max_tokens)
    return (r["choices"][0]["message"].get("content") or "").strip()


def model_name() -> str | None:
    try:
        with urllib.request.urlopen(f"{LLAMA}/v1/models", timeout=3) as r:
            return json.load(r)["data"][0]["id"].split("/")[-1].removesuffix(".gguf")
    except Exception:
        return None
