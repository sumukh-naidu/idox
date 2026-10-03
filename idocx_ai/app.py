"""iDocx AI backend: chat with a local model that works on uploaded PDFs.

    POST /files                      upload a PDF (optionally into a session) -> file metadata
    GET  /files/{id}                 download
    POST /sessions                   -> session_id
    POST /sessions/{id}/messages     -> NDJSON stream of events, one JSON object per line:
                                        text, tool_started, tool_finished, file_created, error, done
    GET  /                           the test page
"""

import json
from pathlib import Path

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from core import agent, store

MAX_UPLOAD_MB = 100
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="iDocx AI backend", version="0.1")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    m = agent.model_name()
    return {"status": "ok" if m else "model unreachable", "model": m, "llama_endpoint": agent.LLAMA}


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
    return FileResponse(path, media_type="application/pdf", filename=meta["name"])


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
