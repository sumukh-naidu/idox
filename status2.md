# idox — Status 2

Snapshot of the project *right now*. For full background, every bug's root cause, and the
complete architecture, see `memory.md` (§14–§16 cover everything since 2026-09-25). This
file only covers current state and the open decisions.

**Handover for the chat integration developer:** `handover_chat_integration.md` (2026-09-30).

**Last updated:** 2026-09-30
**Branch:** `feature/testing_2b_Q8mmproj`, HEAD `b240895`. Everything was committed as of
this update, apart from the edits to this file and `memory.md`.
**Machine:** now `/home/sumukh/Downloads/idox` (moved from `/home/aiteam/idox`, see
`memory.md` §15).

---

## Model in use right now

- **Code default:** `DEFAULT_BASE_URL = http://10.0.3.33:8080`, the remote Qwen3-VL-8B.
  **It is DOWN as of 2026-09-29.**
- **In use instead:** the local 2B (Qwen3-VL-2B-Instruct, Q4_K_M + Q8_0 mmproj) on
  `http://127.0.0.1:8090`. **Pass `--base-url http://127.0.0.1:8090` on every run** until
  the 8B is back. Leaving it out sends the request to the dead remote endpoint, and it fails.
- Launch and stop commands: `memory.md` §15.
- **Image tokens fixed at 512** (`--image-min-tokens 512 --image-max-tokens 512`) to cut
  latency. `hi.png` went from 74.1s to 28.4s, but `complex.png` failed at 512 (hit the
  4,000-token output limit, no file). Details in `memory.md` §15.

---

## Environment on this machine

| Piece | State |
|---|---|
| Python | 3.13.15 via `uv`, `.venv` rebuilt from `requirements.txt` |
| llama-server | b11247, CPU-only, `~/.local/opt/llama.cpp/llama-b11247/` |
| Model weights | `models_manual/` (present) |
| tesseract | **not installed**, so the OCR checks report N/A |
| LibreOffice | **not installed**, so `image_to_pdf.py` / `word_to_pdf.py` fail |
| git, curl | **not installed** |
| Ollama | not installed (optional; only needed for `--base-url ""`) |

Install the missing system packages with:
`sudo apt-get install -y git curl tesseract-ocr libreoffice-writer`

Verified: `image_to_word.py` ran end to end against the local 2B (one smoke test, 74.1s,
self-consistency PASS). The other scripts import cleanly but haven't been re-run on this
machine yet.

---

## What works (verified on the previous machine)

- **PDF → TXT** (`pdf_to_txt.py`), new 2026-09-30: model on digital AND scanned pages. Digital
  pages are corrected and checked against the PDF's text layer (7 of 7 test pages pass);
  scanned pages are read by the model and reported UNVERIFIED with an advisory OCR figure
  (`memory.md` §21, §21.1). Root causes of what the model misses on scans: §21.2.
- **TIFF → PDF** (`tiff_to_pdf.py`), new 2026-09-30: searchable PDF, lossless picture plus
  OCR text layer, no model (`memory.md` §20).
- **PDF → TIFF** (`pdf_to_tiff.py`), new 2026-09-30: one multi-page lossless TIFF per PDF,
  300 dpi, verified pixel-exact against the PDF (`memory.md` §19).
- **PDF → JPG** (`pdf_to_jpg.py`), new 2026-09-29: direct page rendering, no model.
  Tested on 4 PDFs (`memory.md` §17).
- **PDF → Word / Excel / PowerPoint** (`test_pdf.py --docx/--xlsx/--pptx`), both digital
  and scanned (`--scan-mode text/image/both`).
- **Image → Word / PDF / Excel** (`image_to_word.py`, `image_to_pdf.py`,
  `image_to_excel.py`), with low-resolution images upscaled to the 125dpi budget.
- **Deterministic fixes after extraction:**
  - drop duplicate blocks, including list items and tables
  - move a table's heading back in front of it when it trails the table
  - merge nested tables, deepest level first

---

## Open problems (don't conflate them)

1. **Alignment on scanned pages and raw images.** The model is biased toward "center" for
   heading-like text. No fix chosen (`memory.md` §8).
2. **Content sometimes missing on raw images.** The resolution fix helped. What remains is
   the model varying from run to run (§12). Proposed: auto-retry when OCR coverage is low.
   Not built.
3. **Nested tables, third shape.** Rows arrive crammed unevenly into one single table
   block. Not handled.
4. **Suspected bugs from the code read-through** (`memory.md` §16, not yet verified):
   - `merge_nested_tables` with a parent table that has no header
   - `merge_nested_tables` joining separate tables when the first ends in a blank cell
   - trailing-heading fix misfiring on multi-page PDFs
   - bullet stripping in Word removing a leading minus sign
5. **Not built:**
   - human-review report for OCR disagreements
   - skipping the model on digital pages
   - CSV output, image → PPT/XML

---

## Numbers worth keeping straight

- Old laptop, local 2B, `text_only_1page.pdf`: 154.10s. This is from before most fixes;
  treat it as stale.
- 8B remote vs 2B local, same page: prefill 14.07s vs about 68s, generation about 19.25 vs
  12-13 tok/s.
- Latency comparisons need a cold cache: restart the server between runs (`memory.md` §14.3).
- Governor and iGPU questions are closed on the old laptop (§7). This machine has the same
  CPU family and Iris Xe graphics, so the iGPU/Vulkan rule is kept here too.
