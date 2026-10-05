"""Response shapes for the Swagger page (/docs). Documentation only.

app.py passes these through each route's `responses=`, which describes a response
without checking it; nothing here validates or reshapes what an endpoint returns.
Keep them in step with the code that builds each dict (store.save_file,
proposals.public, agent.run_turn, the appliers) and with API.md.
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class Error(BaseModel):
    detail: str = Field(examples=["no session with id s_000000000000"])


class Health(BaseModel):
    status: Literal["ok", "model unreachable"]
    model: str | None = Field(examples=["Qwen3.6-35B-A3B-UD-IQ4_XS"])
    llama_endpoint: str = Field(examples=["http://localhost:8080"])


class FeatureExample(BaseModel):
    file: str | None = Field(description="sample file the prompt is meant for", examples=["redaction_form.pdf"])
    prompt: str = Field(examples=["Find sensitive information in this document and highlight what should be redacted."])
    expect: str = Field(description="what a correct answer looks like")


class Feature(BaseModel):
    id: str = Field(examples=["FR-AI-05"])
    title: str = Field(examples=["Smart Redaction"])
    status: str = Field(examples=["ready"])
    description: str
    samples: list[str] = Field(description="download with GET /features/{id}/samples/{name}",
                               examples=[["redaction_form.pdf", "redaction_scanned.pdf"]])
    examples: list[FeatureExample]


class SessionCreated(BaseModel):
    session_id: str = Field(examples=["s_3f9a1c2b7d4e"])


class FileRecord(BaseModel):
    id: str = Field(description="download with GET /files/{id}", examples=["f_8c21e0d94b7a"])
    name: str = Field(examples=["contract_v1.pdf"])
    pages: int = Field(examples=[3])
    size: int = Field(description="bytes", examples=[48213])
    ext: Literal["pdf", "txt"] = Field(description="txt: a translation's plain-text copy")
    source: str = Field(description='"upload", or the operation that made it, e.g. "merge_pdfs", "redaction"',
                        examples=["upload"])
    parents: list[str] = Field(description="IDs of the file(s) it was made from", examples=[[]])
    created: float = Field(description="Unix time, seconds", examples=[1791180000.52])
    page_origin: list[list[str | int]] = Field(
        description="internal: [uploaded file id, page] each page came from; clients can ignore it",
        examples=[[["f_8c21e0d94b7a", 1], ["f_8c21e0d94b7a", 2]]])


class RedactionItem(BaseModel):
    id: str = Field(description="send in item_ids to redact it", examples=["S3"])
    kind: str = Field(examples=["PAN"])
    category: Literal["government_id", "financial", "contact", "personal", "secret"]
    masked: str = Field(description="never the full value", examples=["ABC…234F"])
    page: int = Field(examples=[1])
    basis: str = Field(examples=["checksum valid"])
    confidence: Literal["high", "medium"]
    read_by: Literal["text layer", "OCR"] = Field(description="OCR: a scan, digits may be misread")
    located: bool = Field(description="false: found in the text but not placed on the page; cannot be redacted")


class FormFillItem(BaseModel):
    id: str = Field(description="the form field's name: send in item_ids, key for edits", examples=["policy_number"])
    label: str = Field(examples=["Policy number"])
    type: Literal["text", "choice", "checkbox", "signature", "other"]
    options: list[str] | None = Field(None, description="choice fields only")
    value: str | bool | None = Field(examples=["POL/2026/004512"])
    status: Literal["found", "check: read from the page image", "unverified", "empty", "user only"] = Field(
        description="found: confirmed in the source; check: read from a scan by the AI; unverified: see why; "
                    "empty: the source does not state it; user only: signatures/declarations, cannot be approved")
    why: str | None = Field(None, description="unverified only: the reason")
    page: int | None = Field(None, description="where in the source document the value was found")
    read_by: str | None = Field(None, examples=["text layer"])


class Proposal(BaseModel):
    id: str = Field(examples=["p_5d0e7a19c3"])
    kind: Literal["redaction", "form_fill"]
    file_id: str = Field(description="the file the change applies to (form fill: the fillable form)")
    items: list[RedactionItem] | list[FormFillItem]
    preview: FileRecord | None = Field(description="redaction: a highlighted copy to show the user; form fill: null")
    summary: dict[str, int | str] = Field(description="counts per category (redaction) or per status (form fill, "
                                                      "plus source_file_id)")
    status: Literal["pending", "applied", "rejected"]
    created: float
    result: dict | None = Field(None, description="after approval: what the approve call returned")


class RedactionResult(BaseModel):
    file: FileRecord = Field(description="the redacted PDF")
    redacted: int
    left_unredacted: int
    verified: str
    note: str


class FormFillResult(BaseModel):
    file: FileRecord = Field(description="the filled form")
    filled: int
    left_empty: list[str]
    user_only: list[str]
    verified: str
    note: str


class Rejected(BaseModel):
    status: Literal["rejected"]


# Chat stream: one of these per line of the application/x-ndjson response.

class TextEvent(BaseModel):
    type: Literal["text"]
    text: str = Field(description="the assistant's reply; may contain Markdown")


class ToolStartedEvent(BaseModel):
    type: Literal["tool_started"]
    tool: str = Field(examples=["merge_pdfs"])
    args: dict | str = Field(description="a raw string if the model sent invalid JSON")


class FileCreatedEvent(BaseModel):
    type: Literal["file_created"]
    file: FileRecord


class ProposalEvent(BaseModel):
    type: Literal["proposal"]
    proposal: Proposal


class ToolFinishedEvent(BaseModel):
    type: Literal["tool_finished"]
    tool: str
    ok: bool
    seconds: float
    result: dict | None = Field(description="differs per tool and includes notes meant for the model: "
                                            "informational, not a stable contract")
    error: str | None


class ErrorEvent(BaseModel):
    type: Literal["error"]
    message: str


class DoneStats(BaseModel):
    model_calls: int
    model_seconds: float
    prompt_tokens: int
    completion_tokens: int
    repeated_calls: int
    total_seconds: float


class DoneEvent(BaseModel):
    type: Literal["done"]
    stats: DoneStats


ChatEvent = Annotated[Union[TextEvent, ToolStartedEvent, FileCreatedEvent, ProposalEvent, ToolFinishedEvent,
                            ErrorEvent, DoneEvent], Field(discriminator="type")]
