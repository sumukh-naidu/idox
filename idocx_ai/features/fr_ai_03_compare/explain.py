"""FR-AI-03 grounding check: an explanation counts only if it cites a real change id, and
every number it states appears in that change's own old or new text. The model cannot
describe a change the comparison did not find, or misstate a figure in one it did."""

import json
import re

from core import llm, rules
from core.pdfutil import ToolError

from .compare import COMPARISONS, NUMBER

SIGNIFICANCE = ("material", "minor")


def check_change_explanations(comparison_id: str, items: list[dict]) -> dict:
    changes = COMPARISONS.get(comparison_id)
    if changes is None:
        raise ToolError(f"unknown comparison_id '{comparison_id}'; run compare_documents first")
    if not isinstance(items, list) or not items:
        raise ToolError('items must be a list like [{"change_id": "C1", "significance": "material", '
                        '"explanation": "..."}]')
    verified, rejected = [], []
    for item in items:
        cid = item.get("change_id") if isinstance(item, dict) else None
        if cid not in changes:
            rejected.append({"item": item, "reason": f"no change with id {cid!r}; cite only ids from the comparison"})
            continue
        if item.get("significance") not in SIGNIFICANCE:
            rejected.append({**item, "reason": "significance must be 'material' or 'minor'"})
            continue
        text = str(item.get("explanation") or "")
        if not text.strip():
            rejected.append({**item, "reason": "explanation is empty"})
            continue
        c = changes[cid]
        source = " ".join(str(c.get(k, "")) for k in ("where", "old_text", "new_text", "diff", "pages"))
        allowed = set(NUMBER.findall(source))
        stated = set(NUMBER.findall(re.sub(r"\bC\d+\b", "", text)))
        wrong = sorted(stated - allowed)
        if wrong:
            rejected.append({**item, "reason": f"states {', '.join(wrong)}, which is not in change {cid}'s text"})
            continue
        verified.append({**item, "type": c["type"], "where": c["where"]})
    explained = {v["change_id"] for v in verified}
    return {"verified": verified, "rejected": rejected,
            "not_explained": [k for k in changes if k not in explained],
            "note": "present only the verified explanations; fix and resend rejected ones, and do not leave "
                    "changes unexplained"}


# ------------------------------------------------------------------ the 3-5 key changes of a comparison

KEY_MIN, KEY_MAX = 3, 5
EXPLAIN_PROMPT = ("Below are the changes found between two versions of a document. For each one give its significance "
                  "(material: amounts, dates, obligations, parties, a whole clause added or removed; minor: wording "
                  "with the same meaning, or a clause only moved) and one plain sentence saying what changed. Use only "
                  "the old and new text shown, and write every number exactly as shown. Reply with JSON only.\n\n")
_EXPLAIN_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "change_id": {"type": "string"}, "significance": {"type": "string", "enum": list(SIGNIFICANCE)},
    "explanation": {"type": "string"}}, "required": ["change_id", "significance", "explanation"]}}}, "required": ["items"]}


def _ask_model(changes: list[dict]) -> list[dict]:
    shown = [{"change_id": c["id"], "kind": c["type"], "where": c["where"], "old": (c.get("old_text") or "")[:300],
              "new": (c.get("new_text") or "")[:300], "signals": c.get("signals", [])} for c in changes]
    r = llm.chat([{"role": "user", "content": EXPLAIN_PROMPT + json.dumps(shown, ensure_ascii=False)}], temperature=0,
                 max_tokens=150 + 90 * len(changes),
                 response_format={"type": "json_schema", "json_schema": {"name": "explain", "schema": _EXPLAIN_SCHEMA}})
    return json.loads(r["choices"][0]["message"].get("content") or "{}").get("items", [])


def key_changes(comparison_id: str, changes: list[dict], explainer=None) -> tuple[list[dict], bool]:
    """(the 3-5 key changes with a verified plain-language explanation, whether explanations were unavailable).

    The model rates and explains the changes; check_change_explanations then drops any explanation that cites a change
    that does not exist or states a number that is not in it. A failing model never fails the comparison."""
    try:
        items = (explainer or _ask_model)(changes)
        verified = check_change_explanations(comparison_id, items)["verified"] if items else []
    except Exception:
        return [], True
    by_id = {c["id"]: c for c in changes}
    order = {c["id"]: k for k, c in enumerate(changes)}
    material = sorted((v for v in verified if v["significance"] == "material"),
                      key=lambda v: (-_rank(by_id[v["change_id"]]), order[v["change_id"]]))
    minor = sorted((v for v in verified if v["significance"] == "minor"),
                   key=lambda v: (-_rank(by_id[v["change_id"]]), order[v["change_id"]]))
    chosen = (material + minor)[:KEY_MAX] if len(material) < KEY_MIN else material[:KEY_MAX]
    chosen.sort(key=lambda v: order[v["change_id"]])        # shown in document order
    out = []
    for v in chosen:
        c = by_id[v["change_id"]]
        out.append({"change_id": v["change_id"], "type": "Added" if c["type"] == "added" else "Removed" if c["type"] == "removed"
                    else "Modified", "where": c["where"], "pages": c["pages"], "significance": v["significance"],
                    "explanation": v["explanation"], **{k: c["_public"][k] for k in ("confidence", "low_confidence")}})
    return out, False


def _rank(c: dict) -> int:
    from .compare import _score
    return _score(c)
