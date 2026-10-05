# iDocx AI Service: API guide for integrators

**Version:** 0.1 (proof of concept) · **Last checked against the code:** 2026-10-05 · **Code:** `idocx_ai/app.py`

This service is the AI half of iDocx. A user uploads PDFs, chats in plain language ("merge these and compress the result", "find sensitive information"), and an AI agent does the work with built-in tools. Some changes, such as redaction and form filling, wait for the user's approval before they are applied.

File conversions (PDF → Word, and so on) are a **separate service** with its own API at `http://10.0.3.66:8200/docs`. They are not covered here.

---

## 1. Basics

| | |
|---|---|
| Base URL | `http://10.0.3.66:8100` (office network only) |
| Interactive docs (Swagger) | `http://10.0.3.66:8100/docs` · raw spec: `/openapi.json`. Shows every request body, response shape and error, grouped as service / files / chat / proposals |
| Authentication | **None yet.** No API key or token header is needed. |
| CORS | **Not enabled yet.** A browser app served from another address that calls this API directly will be blocked by the browser. Calls from a backend server are fine. Send us the frontend's origin (e.g. `http://10.0.3.40:3000`) and we will allow it. |
| Request bodies | JSON (`Content-Type: application/json`), except uploads, which are `multipart/form-data` |
| File types | PDF only, up to **100 MB**. Password-protected PDFs are refused. |
| Test page | `http://10.0.3.66:8100/`: a reference client built on this same API |

### IDs

| Prefix | What | Example |
|---|---|---|
| `s_` | chat session | `s_3f9a1c2b7d4e` |
| `f_` | file (uploads and every file a tool creates) | `f_8c21e0d94b7a` |
| `p_` | proposal (a change waiting for user approval) | `p_5d0e7a19c3` |

Files are never overwritten. Every operation creates a **new** file with a new ID, and the original stays downloadable.

### Errors

Every error is an HTTP status plus a JSON body:

```json
{ "detail": "no session with id s_000000000000" }
```

| Status | Meaning |
|---|---|
| 400 | Bad input: empty message, not a PDF, password-protected PDF, proposal already applied or rejected, invalid approval |
| 404 | Unknown session, file, proposal or sample ID |
| 413 | Upload larger than 100 MB |
| 422 | Request body has the wrong shape (FastAPI validation); `detail` lists the bad fields |

Once a chat stream has started, problems arrive as `error` **events** inside the stream, not as HTTP errors (see §4).

---

## 2. The typical flow

```
1. POST /sessions                       → session_id
2. POST /files   (file + session_id)    → file id        (repeat for each PDF)
3. POST /sessions/{id}/messages         → stream of events: text, tool progress,
                                          new files, proposals, done
4. GET  /files/{id}                     → download any file the stream announced
5. If a "proposal" event arrived (redaction, form fill):
     GET  /proposals/{id}               → show the user the items to review
     POST /proposals/{id}/approve       → applies the user's choice → new file
     or POST /proposals/{id}/reject
6. Keep chatting in the same session (step 3). The assistant remembers the
   conversation and the session's files.
```

The client sends only the **new** message text. The server keeps the conversation history.

---

## 3. Endpoints

### `GET /health`

Is the service up, and can it reach the AI model?

**Response 200**
```json
{ "status": "ok", "model": "Qwen3.6-35B-A3B-UD-IQ4_XS", "llama_endpoint": "http://localhost:8080" }
```
If the model server is down: `"status": "model unreachable"`, `"model": null`. The HTTP status is still 200.

---

### `GET /features`

The AI features, in build order, with example prompts and sample files. This is useful for a "what can I ask?" panel.

**Response 200**: a list:
```json
[
  {
    "id": "FR-AI-05",
    "title": "Smart Redaction",
    "status": "ready",
    "description": "Finds sensitive information: government IDs (checksum-validated), ...",
    "samples": ["redaction_form.pdf", "redaction_scanned.pdf"],
    "examples": [
      { "file": "redaction_form.pdf",
        "prompt": "Find sensitive information in this document and highlight what should be redacted.",
        "expect": "About 25 items over 2 pages: ..." }
    ]
  }
]
```
Feature IDs: `FR-PRD-104` cleanup, `FR-AI-01` workflow, `FR-PRD-105` accessibility, `FR-AI-03` comparison, `FR-AI-05` redaction, `FR-AI-02` forms, `FR-AI-04` translation.

### `GET /features/{feature_id}/samples/{name}`

Downloads a sample PDF listed in a feature's `samples`, made-up test data for trying the feature. Returns the PDF, or `404`.

---

### `POST /sessions`

Starts a chat session. No body.

**Response 200**
```json
{ "session_id": "s_3f9a1c2b7d4e" }
```

---

### `POST /files`

Uploads one PDF. Send `session_id` so the assistant can see the file in that chat.

**Request**: `multipart/form-data`

| Field | Required | Notes |
|---|---|---|
| `file` | yes | the PDF |
| `session_id` | recommended | attach the file to this chat session |

```bash
curl -F "file=@contract_v1.pdf" -F "session_id=s_3f9a1c2b7d4e" http://10.0.3.66:8100/files
```

**Response 200**: the file record (the same shape is used everywhere a file appears):
```json
{
  "id": "f_8c21e0d94b7a",
  "name": "contract_v1.pdf",
  "pages": 3,
  "size": 48213,
  "ext": "pdf",
  "source": "upload",
  "parents": [],
  "created": 1791180000.52,
  "page_origin": [["f_8c21e0d94b7a", 1], ["f_8c21e0d94b7a", 2], ["f_8c21e0d94b7a", 3]]
}
```

| Field | Meaning |
|---|---|
| `id` | file ID, used with `GET /files/{id}` |
| `name` | file name (cleaned of unsafe characters) |
| `pages` | page count |
| `size` | bytes |
| `ext` | `"pdf"`, or `"txt"` (a translation's plain-text copy) |
| `source` | `"upload"`, or the operation that made it (e.g. `"merge_pdfs"`, `"redaction"`, `"form_fill"`) |
| `parents` | IDs of the file(s) it was made from |
| `created` | Unix time in seconds |
| `page_origin` | internal: which original page each page came from. Clients can ignore it. |

**Errors:** `400` not a readable PDF / password-protected · `404` unknown `session_id` · `413` over 100 MB

---

### `GET /files/{file_id}`

Downloads a file: an upload or anything a tool created. The response is the file itself (`application/pdf` or `text/plain`), with a `Content-Disposition` header carrying the file name.

**Errors:** `404` unknown or malformed ID

---

### `POST /sessions/{session_id}/messages`: chat (streaming)

Sends one user message. The response **streams** while the assistant works.

**Request**
```json
{ "text": "Merge these PDF files, remove blank pages, and compress the final document." }
```

**Response 200**: `Content-Type: application/x-ndjson`: **one JSON object per line**, sent as things happen. Read it line by line (§4). The stream always ends with a `done` event.

**Errors before the stream starts:** `404` unknown session · `400` empty message

**How long it takes:** usually 10–30 s per message on the current machine (checked: 12–25 s for the example requests). A long document or a scanned page can take longer. **Don't set short timeouts.** Allow several minutes, and use the `tool_started`/`tool_finished` events to show progress. The model handles **one request at a time** across all users, so a second user's message waits for the first.

---

### `GET /proposals/{proposal_id}`

A change waiting for the user's decision. It arrives first in a `proposal` stream event; this endpoint fetches its current state.

**Response 200**
```json
{
  "id": "p_5d0e7a19c3",
  "kind": "redaction",
  "file_id": "f_8c21e0d94b7a",
  "items": [ ... ],
  "preview": { ...file record... },
  "summary": { ... },
  "status": "pending",
  "created": 1791180042.1
}
```

| Field | Meaning |
|---|---|
| `kind` | `"redaction"` or `"form_fill"`. Item shapes differ; see §5 |
| `file_id` | the file the change applies to (for form fill: the fillable form) |
| `items` | what the user reviews and selects |
| `preview` | redaction: a file record for a **highlighted copy** (download it to show the user what was found). Form fill: `null` |
| `summary` | counts per category (redaction) or per status (form fill) |
| `status` | `"pending"` → `"applied"` or `"rejected"` |
| `result` | present after approval: the same object the approve call returned |

**Errors:** `404`

---

### `POST /proposals/{proposal_id}/approve`

The user's approval. **Only this call applies a redaction or fills a form.** The AI cannot call it.

**Request**
```json
{
  "item_ids": ["S1", "S2", "S5"],
  "edits": null,
  "session_id": "s_3f9a1c2b7d4e"
}
```

| Field | Required | Meaning |
|---|---|---|
| `item_ids` | yes | items the user kept ticked (at least one) |
| `edits` | no | form fill only: values the user changed, `{ "field_name": "new value" }` |
| `session_id` | recommended | attaches the new file to the chat and tells the assistant what was approved, on the next message |

**Response 200** (redaction):
```json
{
  "file": { ...file record of the redacted PDF... },
  "redacted": 3,
  "left_unredacted": 20,
  "verified": "re-read the new file: every redacted value is gone from the text, and every box on a scanned page is black",
  "note": "3 item(s) redacted into f_9b... \"redaction_form-redacted.pdf\" (verified); 20 left unredacted by the user's choice."
}
```

**Response 200** (form fill):
```json
{
  "file": { ...file record of the filled form... },
  "filled": 7,
  "left_empty": ["phone", "email"],
  "user_only": ["declaration"],
  "verified": "re-read the new form: every filled field holds exactly the approved value, and the signature/declaration fields are untouched",
  "note": "7 field(s) filled into f_... \"application_form-filled.pdf\" (verified); left empty: phone, email; for the user to complete: declaration."
}
```

Download the result with `GET /files/{file.id}`. The service re-reads every result file before returning it: if the check fails, nothing is returned and you get a `400`.

**Errors:** `404` unknown proposal · `400` with `detail`, e.g.:
- `this proposal was already applied` / `already rejected`
- `not in this proposal: ['S99']`: an unknown item, or a form field only the user may complete (signature, declaration)
- `select at least one item, or reject the proposal`
- form fill: `'Gold' is not one of the options [...]`, `every chosen field is empty; nothing to fill`
- `verification failed: ... do not use it`

### `POST /proposals/{proposal_id}/reject`

No body. **Response 200** `{ "status": "rejected" }`. **Errors:** `404` · `400` if not pending.

---

## 4. Stream events (`POST /sessions/{id}/messages`)

Every line is one JSON object with a `type`:

| `type` | Fields | What to do with it |
|---|---|---|
| `text` | `text` (string, may contain Markdown) | Show it as the assistant's reply |
| `tool_started` | `tool` (name), `args` (object; a raw string if the model sent invalid JSON) | Show progress, e.g. "Working: merge_pdfs…" |
| `file_created` | `file` (file record, §3) | Offer a download link: `GET /files/{file.id}` |
| `proposal` | `proposal` (same as `GET /proposals/{id}`) | Show a review card: items with checkboxes (and editable values for form fill); Approve / Reject buttons |
| `tool_finished` | `tool`, `ok` (bool), `seconds`, `result` (object or null), `error` (string or null) | Update progress. `result` differs per tool and includes notes meant for the AI: treat it as informational, not as a stable contract |
| `error` | `message` | Show it. The stream still ends with `done` |
| `done` | `stats`: `model_calls`, `model_seconds`, `prompt_tokens`, `completion_tokens`, `repeated_calls`, `total_seconds` | The reply is complete; re-enable the input box |

**Order within one tool call:** `tool_started` → any `file_created` → `proposal` (if any) → `tool_finished`. A message can run several tools (at most 8 AI steps). `text` can appear between tools and at the end. `done` is always last.

**Example stream** (merge → remove blank pages → compress; trimmed):
```
{"type": "tool_started", "tool": "merge_pdfs", "args": {"file_ids": ["f_a1...", "f_b2..."]}}
{"type": "file_created", "file": {"id": "f_c3...", "name": "merged_document.pdf", "pages": 6, ...}}
{"type": "tool_finished", "tool": "merge_pdfs", "ok": true, "seconds": 0.08, "result": {...}, "error": null}
{"type": "tool_started", "tool": "clean_pdf", "args": {"file_id": "f_c3...", "remove_blank": true}}
{"type": "file_created", "file": {"id": "f_d4...", "name": "merged_document-cleaned.pdf", "pages": 5, ...}}
{"type": "tool_finished", "tool": "clean_pdf", "ok": true, "seconds": 0.21, "result": {...}, "error": null}
{"type": "tool_started", "tool": "compress_pdf", "args": {"file_id": "f_d4..."}}
{"type": "file_created", "file": {"id": "f_e5...", "name": "merged_document-cleaned-compressed.pdf", "pages": 5, ...}}
{"type": "tool_finished", "tool": "compress_pdf", "ok": true, "seconds": 0.35, "result": {...}, "error": null}
{"type": "text", "text": "Done. Merged 2 files (6 pages), removed 1 blank page, compressed 258 KB → 69 KB."}
{"type": "done", "stats": {"model_calls": 4, "model_seconds": 24.9, "total_seconds": 25.4, ...}}
```

**Reading the stream in a browser (JavaScript):**
```js
const res = await fetch(`${BASE}/sessions/${sessionId}/messages`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ text }),
});
if (!res.ok) throw new Error((await res.json()).detail);
const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
for (;;) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  let nl;
  while ((nl = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, nl).trim();
    buffer = buffer.slice(nl + 1);
    if (line) handleEvent(JSON.parse(line));   // switch on event.type
  }
}
```

**From a terminal:** `curl -N` prints events as they arrive:
```bash
curl -N -X POST http://10.0.3.66:8100/sessions/s_3f9a1c2b7d4e/messages \
  -H "Content-Type: application/json" -d '{"text": "How many pages does this file have?"}'
```

---

## 5. Proposal items, by kind

### `redaction`: from "find sensitive information"

Each item in `items`:
```json
{
  "id": "S3",
  "kind": "PAN",
  "category": "government_id",
  "masked": "ABC…234F",
  "page": 1,
  "basis": "checksum valid",
  "confidence": "high",
  "read_by": "text layer",
  "located": true
}
```

| Field | Values |
|---|---|
| `id` | `S1`, `S2`, … (send these in `item_ids`) |
| `kind` | what was found, e.g. `Aadhaar number`, `Aadhaar VID`, `PAN`, `passport number`, `GSTIN`, `bank account number`, `IFSC code`, `card number`, `UPI ID`, `phone number`, `email address`, `date of birth`, `person name`, `postal address`, `password or secret`, `API secret key` |
| `category` | `government_id`, `financial`, `contact`, `personal`, `secret` (keys, tokens, passwords) |
| `masked` | the value with most characters hidden, e.g. `XXXXXXXX9012` (long numbers), `ABC…234F`, `r…@example.com`, `M. T.` (names), `••••••••` (passwords). **The full value is never sent to the client or the AI.** |
| `page` | page number |
| `basis` | why it was flagged, e.g. `checksum valid`, `found by the model, checked as exact text` |
| `confidence` | `high` or `medium` |
| `read_by` | `text layer`, or `OCR` (a scanned page: numbers may be misread, so ask the user to check) |
| `located` | `false` = found in the text but not placed on the page; it cannot be redacted automatically |

`summary` = count per category, e.g. `{"government_id": 5, "contact": 3, "secret": 10}`.
`edits` is ignored for redaction. Approving **removes** the text under each box (and blacks out the pixels on scanned pages); it does not just draw a box over readable text.

### `form_fill`: from "fill this form from that document"

`file_id` is the fillable form. Each item is one form field:
```json
{
  "id": "policy_number",
  "label": "Policy number",
  "type": "text",
  "value": "POL/2026/004512",
  "status": "found",
  "page": 1,
  "read_by": "text layer"
}
```

| Field | Values |
|---|---|
| `id` | the form field's name (send these in `item_ids`; use them as keys in `edits`) |
| `label` | the field's label as printed on the form |
| `type` | `text`, `choice` (with `options`), `checkbox`, `signature`, `other` |
| `options` | choice fields only: the allowed values |
| `value` | the proposed value, or `null` |
| `status` | `found`: value confirmed in the source document · `check: read from the page image`: read from a scan by the AI, so check it · `unverified`: not confirmed (see `why`) · `empty`: the source doesn't state it · `user only`: signatures/declarations, which can't be approved here |
| `why` | for `unverified`: the reason |
| `page`, `read_by` | where in the source document the value was found |

`summary` = count per status, plus `source_file_id`.

**`edits`** example (sample application form): `{"plan_type": "Family Floater", "sum_insured": "5,00,000"}`. Choice values must be one of `options`. Checkboxes (e.g. `gst_registered` on the sample payment form) take `true`/`false` (or yes/no). Fields with `status: "user only"` can't be approved.

---

## 6. A complete example: redaction, from the terminal

```bash
BASE=http://10.0.3.66:8100
SID=$(curl -s -X POST $BASE/sessions | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
curl -s -F "file=@redaction_form.pdf" -F "session_id=$SID" $BASE/files
curl -N -X POST $BASE/sessions/$SID/messages -H "Content-Type: application/json" \
  -d '{"text": "Find sensitive information in this document and highlight what should be redacted."}'
# → the stream includes {"type": "proposal", "proposal": {"id": "p_...", ...}}
curl -s $BASE/proposals/p_5d0e7a19c3
curl -s -X POST $BASE/proposals/p_5d0e7a19c3/approve -H "Content-Type: application/json" \
  -d "{\"item_ids\": [\"S1\", \"S2\", \"S3\"], \"session_id\": \"$SID\"}"
# → {"file": {"id": "f_...", "name": "redaction_form-redacted.pdf", ...}, "redacted": 3, ...}
curl -s -o redacted.pdf $BASE/files/f_...
```

The sample PDF is available from `GET /features/FR-AI-05/samples/redaction_form.pdf`.

---

## 7. Current limitations (POC)

| Area | Today | Notes |
|---|---|---|
| Authentication | none | anyone on the office network can call it |
| CORS | not enabled | browser apps on another origin are blocked until we add their origin |
| Sessions and proposals | kept in memory | **lost when the service restarts**; files on disk remain downloadable by ID |
| File retention | files are kept on disk with no automatic cleanup | test with made-up documents only |
| Concurrency | one AI request at a time | other users queue |
| File types | PDF only | conversions are the separate service on port 8200 |
| Response schemas in Swagger | shown for every endpoint, including the 7 stream event types (documentation only: `core/schemas.py`) | the field tables and examples here go further |
| Versioning | none (`/` paths, version 0.1) | breaking changes are possible while this is a POC |
