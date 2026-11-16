# iDocx AI

A chat service for the iDocx AI features. The user uploads PDFs (or an Excel workbook) and asks in plain words; a local model plans the work and calls this service's tools to do it. Changes that need the user's say (redaction, form fill) come back as proposals the user approves in the UI.

The API is described in [API.md](API.md) and, while the service runs, at `/docs`.

## Run

Needs Python 3.13, [uv](https://docs.astral.sh/uv/), and a llama.cpp model server (Qwen3.6-35B-A3B, started with `--jinja`).

```bash
uv sync
LLAMA_ENDPOINT=http://<model server>:8080 bash run.sh
```

Then open `http://<this machine>:8100/`. The service has no login yet, so use made-up documents only.

## Test

```bash
uv run pytest
```

This runs every feature's tool checks against the made-up samples, without the model. One feature at a time, with its full report:

```bash
uv run python tests/checks/fr_prd_104_cleanup.py
```

The **evals** send the requirement document's example requests through the real model and score the result. They need the model server and take minutes:

```bash
LLAMA_ENDPOINT=http://<model server>:8080 uv run python evals/fr_prd_104_cleanup.py
```

Checks and evals write their files and `report.txt` to `tests/output/` and `evals/output/`. Git ignores both folders.

## Layout

| Path | What it holds |
|---|---|
| `app.py` | HTTP routes: upload, download, chat stream, proposals |
| `core/` | The agent loop, model client, file store, proposals, shared PDF helpers |
| `features/<feature>/` | One folder per feature: its tools (`tools.py`: prompt, tool schemas, sidebar info) and their logic |
| `samples/<feature>/` | Made-up test PDFs, their `ground_truth.json`, and `make_samples.py`, which regenerates them |
| `tests/` | `test_features.py` (pytest) and the per-feature checks it runs, in `checks/` |
| `evals/` | Model-in-the-loop checks |
| `static/index.html` | The test page |
| `data/` | Uploaded and generated files at runtime (git ignores it) |

## Features

| ID | Feature | Folder |
|---|---|---|
| FR-PRD-104 | One-click cleanup | `fr_prd_104_cleanup` |
| FR-AI-01 | Workflow automation | `fr_ai_01_workflow` |
| FR-PRD-105 | Accessibility | `fr_prd_105_accessibility` |
| FR-AI-03 | Document comparison | `fr_ai_03_compare` |
| FR-AI-05 | Smart redaction | `fr_ai_05_redaction` |
| FR-AI-02 | Extraction and form filling | `fr_ai_02_forms` |
| FR-AI-04 | Translation | `fr_ai_04_translation` |
| none | Excel to Word | `excel_to_word` |

To add a feature, create `features/<feature>/` with a `tools.py` (`PROMPT`, `TOOLS`, `INFO`), register it in `core/tools.py`, put its samples in `samples/<feature>/`, and add a check in `tests/checks/<feature>.py`.
