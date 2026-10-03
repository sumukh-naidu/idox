"""FR-AI-03 grounding check: an explanation counts only if it cites a real change id, and
every number it states appears in that change's own old or new text. The model cannot
describe a change the comparison did not find, or misstate a figure in one it did."""

import re

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
