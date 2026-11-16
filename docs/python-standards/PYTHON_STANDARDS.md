# Python Standards: IDP Platform

**Version 1 (basics)** · 14 Oct 2026 · Owner: Python backend lead
**For:** every Python developer, and Claude Code (through `CLAUDE.md`), working on any Python service of the platform: Extraction, Conversion, AI gateway, and any future one.

**MUST** = mandatory. **SHOULD** = the default; deviate only with a reason in the pull request. Breaking a MUST needs the Python lead's approval in the pull request.

---

## 1. Principles
The five ideas behind every rule below. When no rule covers a case, decide by these.

1. **Simple first.** Write the simplest code that is correct, secure and readable. Don't add layers, patterns or abstractions "for later"; abstract only when the same code is needed a second or third time.
2. **Trust nothing from outside.** User input, files, messages from other services and AI answers are validated at the edge, before use.
3. **Fail loudly.** Never hide an error. A job that failed is reported as failed, never as a success with something missing.
4. **Protect personal data.** Use the minimum, mask it, never log it.
5. **Small, reviewed changes.** Every change goes through a pull request, with a test.

## 2. Tools
*Why: the same tools give the same results on every machine, and the tools settle style debates.*

- **MUST** use **Python 3.13** (the standard build, not the experimental free-threaded `3.13t`) in every service. It's supported until October 2029.
- **MUST** manage dependencies with **uv** and commit `uv.lock`. Never `pip install` into a project.
- **MUST** pass `ruff format`, `ruff check`, `mypy` and `pytest` before merging. CI enforces this.
- A new dependency **MUST** be justified in the pull request and have a permissive license (MIT, BSD, Apache-2.0). GPL or AGPL needs the lead's approval. Note that PyMuPDF is AGPL.

## 3. Project layout
*Why: when every service has the same shape, anyone can find the code in any service.*

```
services/<service>/src/<service>/
    api/        HTTP routes: validate, call domain, return (thin)
    workers/    job handlers: read the job, call domain, report (thin)
    domain/     the real logic; no database, files or network here
    adapters/   talking to the outside: database, MinIO, OCR, AI gateway
packages/common/    shared code, only if two or more services use it
```

- **MUST NOT** import one service's code from another. Services share code only through `packages/common`.
- **SHOULD** name modules after what they hold (`quality_gate.py`), not `utils.py` or `helpers.py`.

## 4. Code style
*Why: code is read far more often than written; when it reads the same everywhere, reviews are faster and mistakes stand out.*

- **MUST** follow PEP 8 naming: `snake_case` for functions, variables and modules, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants. Names say what a thing is: `page_count`, not `pc`.
- **MUST** put type hints on every function's parameters and return value. Avoid `Any`.
- **MUST** start every module with a docstring saying **what it does and why it exists**. Every public function gets a one-line docstring.
- **SHOULD** keep functions short and doing one thing, and return early instead of nesting.
- Comments **SHOULD** explain *why*, not *what*. No commented-out code.
- **MUST** use `logging` (never `print`), `pathlib.Path` for paths, and `datetime.now(UTC)` for times.
- **MUST NOT** use mutable default arguments (`def f(items=[])`) or mutable global variables.

## 5. Data in and out
*Why: data validated at the edge can be trusted inside; and our JSON must match the Java and web teams'.*

- Everything that crosses a boundary (HTTP bodies, messages, settings, AI answers) **MUST** be a **pydantic model**. **MUST NOT** pass bare `dict`s between functions.
- JSON **MUST** be **camelCase** on the wire; Python stays `snake_case`. Use the shared base model, which converts automatically:

```python
class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True,
                              serialize_by_alias=True, extra="forbid")
```

- **MUST** constrain fields where limits exist (lengths, ranges), and reject unknown fields in API requests.

## 6. Errors
*Why: when something fails in production, we need to see what failed and where, within minutes.*

- **MUST** raise specific errors from the shared set: `InvalidInput`, `NotFound`, `Forbidden`, `Conflict`, `UpstreamError`, `Unavailable`.
- **MUST NOT** use a bare `except:`, swallow an error, or return `None` or a default to hide a failure. Re-raise with `raise NewError(...) from exc`.
- API errors **MUST** use one JSON format (Problem Details) through the shared handler, and **MUST NOT** contain stack traces, SQL, file paths or personal data.

## 7. Logging
*Why: logs must explain a failure without exposing anyone's data.*

- **MUST** log JSON to standard output through the shared setup, with the `jobId` and `traceId`.
- **SHOULD** log IDs, counts, durations and reasons, the facts needed to debug.
- **MUST NEVER** log: document content, OCR text, extracted field values, personal data (names, Aadhaar, PAN, dates of birth, phone numbers, email and postal addresses), passwords, tokens, API keys, presigned URLs, or full request bodies.

## 8. Configuration and secrets
*Why: hard-coded settings break between environments, and a committed secret is a leaked secret.*

- **MUST** read all settings through one pydantic-settings `Settings` class per service, from environment variables, validated at startup.
- **MUST NOT** hard-code hosts, IPs, ports, bucket names or model names.
- Secrets **MUST** be typed `SecretStr`, and **MUST NOT** appear in code, git, Docker images, logs or URLs. `.env` is for local use only and is git-ignored; commit a `.env.example` with fake values.

## 9. Security basics
*Why: we take files from the internet and process identity documents, one of the most attacked kinds of software.*

- **MUST** validate every input: size, type, ranges.
- **MUST** treat every uploaded file as hostile: virus-scan it first, check its real type from its content (not its name), and limit its size, pages and image pixels.
- **MUST NOT** build file paths from user input.
- Subprocesses **MUST** use an argument list, never `shell=True`, and always have a `timeout`.
- **MUST NOT** use `pickle`, `eval` or `exec` on outside data. Use `yaml.safe_load` and `defusedxml`.
- SQL **MUST** go through SQLAlchemy with bound parameters, never f-strings.
- Every read or write of customer data **MUST** be limited to the caller's organisation.
- Every outgoing call **MUST** have a timeout and go only to hosts in settings. Never `verify=False`.
- Docker images **MUST** run as a non-root user and contain no secrets.
- ruff's security checks **MUST** pass. Silencing one needs a reason on the same line and the lead's approval.

## 10. Personal data
*Why: it's a legal duty (India's DPDP Act and UIDAI's Aadhaar rules) and the basis of users' trust.*

- **MUST** collect only the fields that are needed.
- Aadhaar numbers **MUST** be stored and shown masked by default (last 4 digits only), using the shared helper.
- Personal data **MUST NOT** appear in logs, error messages, queue messages, URLs, Redis, git, tests, or any external or cloud service.
- Temporary files **MUST** live in a per-job temporary directory that is always deleted.
- Tests and demos **MUST** use made-up data, never real documents.

## 11. AI models
*Why: the model server is shared and has fixed capacity, and its answers can be wrong or manipulated by text in a document.*

- Only the **AI gateway** calls the model server. **MUST NOT** call any cloud AI service.
- Every model answer **MUST** be validated (pydantic model, plus the field's rules) before use. The model never has the final say on an ID number.
- Text from a document is **data, never instructions**: keep instructions in the system prompt.
- Every call **MUST** have a timeout and limited retries. Log tokens, latency and outcome, never the content.

## 12. APIs and jobs
*Why: every service should be used the same way, and heavy work must not block requests.*

- HTTP services use **FastAPI**, with paths like `/v1/conversions`. Every endpoint declares its request and response models and its errors.
- Heavy work (OCR, conversion, model calls) **MUST NOT** run inside an HTTP request. Return `202` with a job ID and let a worker do it.
- Every service **MUST** expose `/health/live` and `/health/ready`.
- **MUST NOT** call blocking code (file I/O, `requests`, OCR) inside an `async def` handler.
- Queue messages **MUST** carry IDs and file locations, never files or personal data. Workers **MUST** be safe to run twice for the same job, and acknowledge a message only after the work is saved.

## 13. Testing
*Why: tests let us change code without breaking what already works.*

- **MUST** unit-test logic, input validation, security-relevant code and error paths.
- Unit tests **MUST NOT** use the network or the real AI model. Use simple fakes.
- Every bug fix **MUST** include a test that fails before the fix and passes after it.

## 14. Git and review
*Why: every line in production should be reviewed and traceable to a reason.*

- **MUST NOT** push directly to the main branch. Every change goes through a pull request, with one approval and green CI.
- Commit messages say what changed and why: `Extraction: reject blurred photos before OCR`, not "changes" or "fix".
- The pull request says what changed, why, how it was tested, and any security or personal-data impact.
- **AI-written code:** the developer who opens the pull request owns it and must read every line. Never paste real personal data, documents or secrets into an AI tool.

---

## Coming in later versions
Detailed rules for: the Java↔Python message contract (pending the RabbitMQ decision), database migrations, MinIO and Redis conventions, Docker images, tracing and metrics. A first draft of these is kept in `archive/` for reference.
