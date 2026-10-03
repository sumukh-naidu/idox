"""The feature list the test page's sidebar shows, in build order.

Built features describe themselves with INFO in their own tools.py. Features not
built yet get a placeholder here, which goes away once the feature has a folder.
"""

from pathlib import Path

from core.tools import FEATURES

ORDER = ["FR-PRD-104", "FR-AI-01", "FR-PRD-105", "FR-AI-03", "FR-AI-05", "FR-AI-02", "FR-AI-04"]

PLANNED = {
}


def _built() -> dict[str, object]:
    return {f.INFO["id"]: f for f in FEATURES if hasattr(f, "INFO")}


def features() -> list[dict]:
    built, out = _built(), []
    for fid in ORDER:
        if fid in built:
            info = built[fid].INFO
            out.append({**info, "examples": info.get("examples", []), "samples": info.get("samples", [])})
        else:
            p = PLANNED[fid]
            out.append({"id": fid, **p, "samples": [],
                        "examples": [{"file": None, "prompt": e,
                                      "expect": "Not built yet: the assistant should say it cannot do this."}
                                     for e in p["examples"]]})
    return out


def sample_path(feature_id: str, name: str) -> Path | None:
    """Only files a built feature lists as samples, from its own input/ folder."""
    module = _built().get(feature_id)
    if not module or name not in module.INFO.get("samples", []):
        return None
    path = Path(module.__file__).resolve().parent / "input" / name
    return path if path.is_file() else None
