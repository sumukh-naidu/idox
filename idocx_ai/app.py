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
from pydantic import BaseModel, Field

from core import agent, catalog, llm, proposals, schemas, store

MAX_UPLOAD_MB = 100
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="iDocx AI backend", version="0.1",
    description="Chat with an AI agent that works on uploaded PDFs. Typical flow: POST /sessions, POST /files "
                "(with session_id), POST /sessions/{id}/messages (streamed events), GET /files/{id} for results, "
                "and GET/POST /proposals/{id} when a change needs the user's approval. No authentication yet. "
                "Full guide with examples: idocx_ai/API.md.")


def _err(description: str) -> dict:
    return {"model": schemas.Error, "description": description}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health", tags=["service"], responses={200: {"model": schemas.Health}})
def health():
    """Is the service up, and can it reach the model server? Always 200; check `status`."""
    m = llm.model_name()
    return {"status": "ok" if m else "model unreachable", "model": m, "llama_endpoint": llm.LLAMA}


@app.get("/features", tags=["service"], responses={200: {"model": list[schemas.Feature]}})
def features():
    """Every in-scope feature, in build order, for the test page's sidebar."""
    return catalog.features()


@app.get("/features/{feature_id}/samples/{name}", tags=["service"],
         responses={200: {"description": "the sample PDF"}, 404: _err("not a sample of this feature")})
def feature_sample(feature_id: str, name: str):
    """A made-up test PDF listed in a feature's `samples`."""
    path = catalog.sample_path(feature_id, name)
    if not path:
        raise HTTPException(404, f"no sample '{name}' for {feature_id}")
    return FileResponse(path, media_type="application/pdf", filename=name)


@app.post("/files", tags=["files"],
          responses={200: {"model": schemas.FileRecord}, 400: _err("not a readable PDF, or password-protected"),
                     404: _err("unknown session_id"), 413: _err("larger than 100 MB")})
async def upload(file: UploadFile = File(..., description="a PDF, up to 100 MB"),
                 session_id: str | None = Form(None, description="attach the file to this chat session")):
    """Upload one PDF. Returns its file record; `id` is the file ID used everywhere else."""
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


@app.get("/files/{fid}", tags=["files"],
         responses={200: {"description": "the file itself, with its name in Content-Disposition"},
                    404: _err("unknown or malformed file ID")})
def download(fid: str):
    """Download an upload or any file a tool or an approval created."""
    try:
        meta, path = store.get_file(fid)
    except store.NotFound as e:
        raise HTTPException(404, str(e))
    return FileResponse(path, media_type=store.EXTENSIONS[meta.get("ext", "pdf")], filename=meta["name"])


@app.post("/sessions", tags=["chat"], responses={200: {"model": schemas.SessionCreated}})
def create_session():
    """Start a chat session. Sessions live in memory: a restart of the service ends them."""
    return {"session_id": store.new_session()}


class Message(BaseModel):
    text: str = Field(examples=["Merge these PDF files, remove blank pages, and compress the final document."])


@app.post("/sessions/{sid}/messages", tags=["chat"],
          responses={200: {"model": schemas.ChatEvent,
                           "description": "application/x-ndjson: one JSON event per line, as the work happens; "
                                          "the last line is always a `done` event"},
                     400: _err("empty message"), 404: _err("unknown session")})
def send_message(sid: str, msg: Message):
    """Send one user message; the reply streams back as events, one JSON object per line.

    Event types: `text` (the reply), `tool_started` / `tool_finished` (progress), `file_created`
    (a new file to offer for download), `proposal` (a change for the user to review and approve),
    `error`, and finally `done` (with timing stats). Within one tool call the order is
    tool_started, file_created*, proposal?, tool_finished. Problems after the stream has
    started arrive as `error` events, not HTTP errors.

    Usually 10-30 s; the model serves one request at a time. Don't use short timeouts.
    """
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
    item_ids: list[str] = Field(description="the items the user kept ticked (at least one)",
                                examples=[["S1", "S2", "S5"]])
    edits: dict | None = Field(None, description="form fill only: values the user changed, by item id",
                               examples=[{"plan_type": "Family Floater"}])
    session_id: str | None = Field(None, description="attach the new file to this chat and tell the assistant",
                                   examples=["s_3f9a1c2b7d4e"])


@app.get("/proposals/{pid}", tags=["proposals"],
         responses={200: {"model": schemas.Proposal}, 404: _err("unknown proposal")})
def get_proposal(pid: str):
    """A change (redaction or form fill) waiting for the user's decision, as announced by a `proposal` event."""
    try:
        return proposals.public(pid)
    except proposals.NotFound as e:
        raise HTTPException(404, str(e))


@app.post("/proposals/{pid}/approve", tags=["proposals"],
          responses={200: {"model": schemas.RedactionResult | schemas.FormFillResult,
                           "description": "the new file (download with GET /files/{file.id}), already re-read "
                                          "and verified"},
                     400: _err("already applied or rejected, unknown item, nothing selected, invalid edit, "
                               "or verification failed"),
                     404: _err("unknown proposal")})
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


@app.post("/proposals/{pid}/reject", tags=["proposals"],
          responses={200: {"model": schemas.Rejected}, 400: _err("not pending"), 404: _err("unknown proposal")})
def reject_proposal(pid: str):
    """The user declined the change; nothing is applied."""
    try:
        proposals.reject(pid)
    except proposals.NotFound as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "rejected"}


# FastAPI documents every 200 as application/json. Relabel the three that are not:
# the chat stream and the two file downloads. Only the /docs page changes.
BINARY = {"schema": {"type": "string", "format": "binary"}}
MEDIA = {("/sessions/{sid}/messages", "post"): "application/x-ndjson",
         ("/files/{fid}", "get"): {"application/pdf": BINARY, "text/plain": BINARY},
         ("/features/{feature_id}/samples/{name}", "get"): {"application/pdf": BINARY}}


def _openapi():
    if app.openapi_schema is None:
        spec = FastAPI.openapi(app)          # FastAPI's own build; it caches it in app.openapi_schema
        for (path, method), media in MEDIA.items():
            ok = spec["paths"][path][method]["responses"]["200"]
            if isinstance(media, str):
                ok["content"] = {media: ok["content"]["application/json"]}
            else:
                ok["content"] = media
    return app.openapi_schema


app.openapi = _openapi
