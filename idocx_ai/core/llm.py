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


def chat(messages: list[dict], timeout: int = 600, **options) -> dict:
    body = json.dumps({"messages": messages, **options}).encode()
    req = urllib.request.Request(f"{LLAMA}/v1/chat/completions", body, {"Content-Type": "application/json"})
    with _lock, urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


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
