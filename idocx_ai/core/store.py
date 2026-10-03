"""Files on disk, chat sessions in memory. No database for the POC.

Every upload and every tool output gets its own ID; nothing is ever overwritten.
"""

import json
import re
import time
import uuid
from pathlib import Path

FILES = Path(__file__).resolve().parent.parent / "data" / "files"
FILES.mkdir(parents=True, exist_ok=True)


def set_root(path: Path) -> None:
    """Point the store somewhere else, e.g. a feature's output/ folder during its checks."""
    global FILES
    FILES = Path(path)
    FILES.mkdir(parents=True, exist_ok=True)

FILE_ID = re.compile(r"^f_[0-9a-f]{12}$")

SESSIONS: dict[str, dict] = {}


class NotFound(Exception):
    pass


def _safe_name(name: str) -> str:
    name = Path(name or "file.pdf").name
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip() or "file.pdf"
    return name[:120]


EXTENSIONS = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8"}


def save_file(data: bytes, name: str, pages: int, source: str, parents=None, page_origin=None,
              ext: str = "pdf") -> dict:
    """page_origin[i] = [uploaded file id, page number] that page i+1 came from, so a
    later request can say "original page 6" after other pages were removed.
    ext: "pdf", or "txt" for text outputs such as a translation's searchable copy."""
    if ext not in EXTENSIONS:
        raise ValueError(f"unsupported file type: {ext}")
    fid = f"f_{uuid.uuid4().hex[:12]}"
    (FILES / f"{fid}.{ext}").write_bytes(data)
    meta = {"id": fid, "name": _safe_name(name), "pages": pages, "size": len(data), "ext": ext,
            "source": source, "parents": parents or [], "created": time.time(),
            "page_origin": page_origin or [[fid, n] for n in range(1, pages + 1)]}
    (FILES / f"{fid}.json").write_text(json.dumps(meta))
    return meta


def get_file(fid: str) -> tuple[dict, Path]:
    if not isinstance(fid, str) or not FILE_ID.match(fid):
        raise NotFound(f"not a valid file id: {fid!r}")
    meta_path = FILES / f"{fid}.json"
    if not meta_path.exists():
        raise NotFound(f"no file with id {fid}")
    meta = json.loads(meta_path.read_text())
    return meta, FILES / f"{fid}.{meta.get('ext', 'pdf')}"


def new_session() -> str:
    sid = f"s_{uuid.uuid4().hex[:12]}"
    SESSIONS[sid] = {"id": sid, "messages": [], "files": [], "created": time.time()}
    return sid


def get_session(sid: str) -> dict:
    if sid not in SESSIONS:
        raise NotFound(f"no session with id {sid}")
    return SESSIONS[sid]


def attach(session: dict, fid: str) -> None:
    if fid not in session["files"]:
        session["files"].append(fid)
