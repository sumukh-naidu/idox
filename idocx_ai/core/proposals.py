"""Proposals: changes a tool prepares but only the user can approve (redaction now,
form filling later).

The model is never given a tool that applies a proposal. Approval comes from the user
through the API, so review before finalising is enforced in code, not by instruction.
Private data (raw values, positions) stays here and is never sent to the model.
"""

import time
import uuid

PROPOSALS: dict[str, dict] = {}


class NotFound(Exception):
    pass


def create(kind: str, file_id: str, items: list[dict], private: dict, preview: dict | None = None,
           summary: dict | None = None) -> dict:
    pid = f"p_{uuid.uuid4().hex[:10]}"
    PROPOSALS[pid] = {"id": pid, "kind": kind, "file_id": file_id, "items": items, "private": private,
                      "preview": preview, "summary": summary or {}, "status": "pending", "created": time.time()}
    return public(pid)


def get(pid: str) -> dict:
    if pid not in PROPOSALS:
        raise NotFound(f"no proposal with id {pid}")
    return PROPOSALS[pid]


def public(pid: str) -> dict:
    return {k: v for k, v in get(pid).items() if k != "private"}


# kind -> fn(proposal, chosen item ids, user edits) -> result with a "note" for the chat.
APPLIERS: dict[str, object] = {}


def register_applier(kind: str, fn) -> None:
    APPLIERS[kind] = fn


def approve(pid: str, item_ids: list[str], edits: dict | None = None) -> dict:
    """Called only from the API, on the user's click. Raises ValueError for a bad request.
    edits: values the user changed in the review card, by item id."""
    p = get(pid)
    if p["status"] != "pending":
        raise ValueError(f"this proposal was already {p['status']}")
    unknown = sorted(set(item_ids) - set(p["private"]))
    if unknown:
        raise ValueError(f"not in this proposal: {unknown}")
    if not item_ids:
        raise ValueError("select at least one item, or reject the proposal")
    result = APPLIERS[p["kind"]](p, list(dict.fromkeys(item_ids)), edits or {})
    p["status"], p["result"] = "applied", result
    return result


def reject(pid: str) -> None:
    p = get(pid)
    if p["status"] != "pending":
        raise ValueError(f"this proposal was already {p['status']}")
    p["status"] = "rejected"
