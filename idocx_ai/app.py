"""iDocx AI backend: chat with a local model that works on uploaded PDFs.

    POST /files                      upload a PDF (optionally into a session) -> file metadata
    GET  /files/{id}                 download
    POST /sessions                   -> session_id
    POST /sessions/{id}/messages     -> NDJSON stream of events, one JSON object per line:
                                        text, tool_started, tool_finished, file_created, proposal, error, done
    GET  /proposals/{id}             a pending change, e.g. a redaction, for the user to review
    POST /proposals/{id}/approve     the user's approval: applies the chosen items (the model cannot)
    POST /proposals/{id}/reject
    GET  /                           the test page
"""

import json
from pathlib import Path

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from core import agent, catalog, llm, proposals, store

MAX_UPLOAD_MB = 100
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="iDocx AI backend", version="0.1")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    m = llm.model_name()
    return {"status": "ok" if m else "model unreachable", "model": m, "llama_endpoint": llm.LLAMA}


@app.get("/features")
def features():
    """Every in-scope feature, in build order, for the test page's sidebar."""
    return catalog.features()


@app.get("/features/{feature_id}/samples/{name}")
def feature_sample(feature_id: str, name: str):
    path = catalog.sample_path(feature_id, name)
    if not path:
        raise HTTPException(404, f"no sample '{name}' for {feature_id}")
    return FileResponse(path, media_type="application/pdf", filename=name)


@app.post("/files")
async def upload(file: UploadFile = File(...), session_id: str | None = Form(None)):
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"file larger than {MAX_UPLOAD_MB} MB")
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if not doc.is_pdf:
                raise ValueError("not a PDF")
            if doc.needs_pass:
                raise HTTPException(400, "password-protected PDFs are not supported yet")
            pages = doc.page_count
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, f"'{file.filename}' is not a readable PDF")

    meta = store.save_file(data, file.filename, pages, source="upload")
    if session_id:
        try:
            store.attach(store.get_session(session_id), meta["id"])
        except store.NotFound as e:
            raise HTTPException(404, str(e))
    return meta


@app.get("/files/{fid}")
def download(fid: str):
    try:
        meta, path = store.get_file(fid)
    except store.NotFound as e:
        raise HTTPException(404, str(e))
    return FileResponse(path, media_type=store.EXTENSIONS[meta.get("ext", "pdf")], filename=meta["name"])


@app.post("/sessions")
def create_session():
    return {"session_id": store.new_session()}


class Message(BaseModel):
    text: str


@app.post("/sessions/{sid}/messages")
def send_message(sid: str, msg: Message):
    try:
        session = store.get_session(sid)
    except store.NotFound as e:
        raise HTTPException(404, str(e))
    if not msg.text.strip():
        raise HTTPException(400, "empty message")

    def stream():
        for event in agent.run_turn(session, msg.text.strip()):
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


class Approval(BaseModel):
    item_ids: list[str]
    edits: dict | None = None      # values the user changed in the review card, by item id
    session_id: str | None = None


@app.get("/proposals/{pid}")
def get_proposal(pid: str):
    try:
        return proposals.public(pid)
    except proposals.NotFound as e:
        raise HTTPException(404, str(e))


@app.post("/proposals/{pid}/approve")
def approve_proposal(pid: str, body: Approval):
    """The user's approval from the review card. No tool can reach this."""
    try:
        result = proposals.approve(pid, body.item_ids, body.edits)
    except proposals.NotFound as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    session = store.SESSIONS.get(body.session_id or "")
    if session is not None:
        f = result["file"]
        store.attach(session, f["id"])
        session.setdefault("notes", []).append(f"The user approved proposal {pid} in the review card: {result['note']}")
    return result


@app.post("/proposals/{pid}/reject")
def reject_proposal(pid: str):
    try:
        proposals.reject(pid)
    except proposals.NotFound as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "rejected"}
