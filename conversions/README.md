# Conversions (reference only)

The document conversion scripts built before the iDocx AI work: PDF to Word, PowerPoint, Excel, text and images, and back. They are kept here as they were, but they are **not supported**; the supported product is [`idocx_ai/`](../idocx_ai/README.md).

| Folder | What it holds |
|---|---|
| `scripts/` | The converter scripts and their shared code (`blocks.py`, `ocr.py`, `pdf_look.py`, `to_docx.py`, ...), and `idox_app.py` with its page |
| `api/` | `idox_api.py`: a FastAPI service over the scripts, with its test page |
| `tests/` | Tests that need no model |
| `input/<route>_input/` | Sample inputs, one folder per conversion |
| `output/` | Everything the scripts write. Git ignores it. |
| `docs/` | The handover and status notes written during this work |

Run everything from this folder:

```bash
cd conversions
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/pdf_to_png.py input/pdf_to_png_input/<file>.pdf
.venv/bin/uvicorn api.idox_api:app --port 8000
.venv/bin/python -m unittest discover -s tests
```

The notes in `docs/` were written before this layout, so they give script paths from the repo root (`pdf_to_png.py`, `pdf_to_png_input/`). Those now live under `scripts/` and `input/`.
