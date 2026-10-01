# idox — Handover for the chat / service integration developer

**Written:** 2026-09-30 · **Repo:** `/home/sumukh/Downloads/idox` (remote `git@github.com:sumukh-naidu/idox.git`) · **Branch:** `feature/testing_2b_Q8mmproj` · **HEAD:** `21b6ab6`
**Audience:** a developer who will build a **chat option that triggers the document conversions** described here. You do not need to know how the extraction works internally to use it, but sections 4–5 explain it in case you have to debug or extend it.

**How to read this document:** every number and behaviour below was either *measured/run during the project* or is labelled as *not verified* or *proposal*. Where something is only read from the code, or is a recommendation, it says so. Nothing here is a promise about accuracy on documents that were not tested.

---

## Contents

0. [At a glance](#0-at-a-glance)
1. [What the project is](#1-what-the-project-is)
2. [Functionality catalogue](#2-functionality-catalogue-what-is-built)
3. [How many features are done](#3-how-many-features-are-done)
4. [How the features were made](#4-how-the-features-were-made)
5. [AI models: how many, and where they are used](#5-ai-models-how-many-and-where-they-are-used)
6. [Latency of the AI models, as of now](#6-latency-as-of-now)
7. [Exposing the features to a chatbot / other services](#7-exposing-the-features-to-a-chatbot-or-other-services)
8. [Environment and setup](#8-environment-and-setup)
9. [Current state of the repo and the machine](#9-current-state)
10. [Known issues, gaps and open decisions](#10-known-issues-gaps-and-open-decisions)
11. [Standing rules and conventions](#11-standing-rules-and-conventions-set-by-the-project-owner)
12. [Repo map](#12-repo-map)
13. [Other documents in the repo, and which to trust](#13-other-documents-in-the-repo-and-which-to-trust)
14. [Appendix: command reference](#14-appendix-command-reference)

---

## 0. At a glance

| | |
|---|---|
| **What it is** | A **local, fully offline** pipeline that converts documents between formats. A vision-language model reads pixels only where nothing else can; everything else is deterministic code. |
| **Features built** | **11 conversion routes** (12 outputs if you count the Markdown file the PDF converter always writes). Details in §2–§3. |
| **Routes that need the AI model** | 7 (PDF→Word, PDF→Excel, PDF→PowerPoint, PDF→TXT, Image→Word, Image→PDF, Image→Excel). |
| **Routes with no AI model** | 4 (Word→PDF, PDF→JPG, PDF→TIFF, TIFF→PDF), plus the "image" mode of Image→Word. |
| **AI models** | **One model family: Qwen3-VL, plus one other model on the tuhin-ai API.** Local **Qwen3-VL-2B** on this machine. **tuhin-ai API**: a remote **Qwen3.6-35B-A3B** (vision) on a colleague's laptop, authenticated by an API key, **verified working with this pipeline** (§5.6). The older remote 8B at `10.0.3.33` is **down**. Also **Tesseract 5.5.0** (OCR, not a language model). |
| **Speed** | Model routes are **slow: roughly 25 s to several minutes per page** on this CPU-only laptop (§6). Non-model routes take **under a few seconds to ~20 s**. |
| **Exposed to other services?** | **Partly.** There is still no API for other services, but there is now a **local web app** (`idox_app.py` + `idox_app.html`, localhost only) where a person uploads a file, picks a conversion, and downloads the result. It runs the same scripts as subprocesses, one job folder each, with a queue, cancel and downloads. It is a working reference for the job-service pattern in §7 (§7.8). |
| **Git** | Everything is committed (HEAD `21b6ab6`) except one untracked test file, `pdf_to_word_input/new.pdf`. |

**Five things you must know before you start:**

1. **The default model endpoint in the code is dead.** `blocks.DEFAULT_BASE_URL` still points to `http://10.0.3.33:8080` (the old remote 8B), which refuses connections, so a model route fails unless you pass `--base-url`. Use the tuhin-ai API (`--base-url http://10.0.3.2:8080` with the key in `IDOX_API_KEY`) or the local 2B (`--base-url http://127.0.0.1:8090`). The web app does this for you from its Settings. §5.3, §5.6.
2. **Most scripts cannot be imported.** They run their argument parsing at import time. Call them as subprocesses, or refactor (§7.3).
3. **Model routes take minutes and can fail** (a 4,000-token output cap, a 600 s request timeout, an endpoint that goes down). A chat layer must run them as background jobs with progress and clear failure messages (§7).
4. **`test_pdf.py` can silently skip a page** whose model call fails, and its final "content check" does not notice (§10.2). Do not treat its exit code (always 0) as success.
5. **Run each job from its own working directory** (`cd jobdir && python /repo/script.py ...`). The scripts use relative paths (`render/`), and this isolation was tested with two concurrent jobs (§7.4).

---

## 1. What the project is

**Goal (owner's words in the project history):** a *local, fully offline* pipeline that converts documents into Office and image formats, using a locally run vision-language model where a model is genuinely needed and plain deterministic code everywhere else. No cloud APIs.

**Guiding principles that shaped every decision:**

- **The model does one job only:** read one page image and describe it as structured blocks (headings, paragraphs, lists, captions, tables). It never writes Word/Excel/PowerPoint bytes. Office files are built by ordinary libraries from those blocks.
- **Evidence over assumption.** Every latency figure and every "is it fixed" claim was measured (server logs, byte-level file reads, re-runs). The owner rejected unverified claims repeatedly.
- **Deterministic fixes over prompt tweaks.** When the model misbehaves, the fix is plain code that measures the failure and corrects it, not "improve the prompt and hope".
- **Check the output, don't trust it.** Almost every route re-opens the file it wrote and checks it against the source (§4.7).

**Origin and history in one paragraph:** the project began as a latency investigation of running Qwen3-VL locally (vision-token maths, quantization of the vision encoder, iGPU offload, CPU governor), then became a real PDF→Word/Excel pipeline, then grew image inputs, PowerPoint output, and finally the pure image/PDF/TIFF conversions. A long list of real bugs was found and fixed along the way (duplicate blocks, false "centre" alignment, nested tables, bullet-character mismatches, and others). The detailed record is `memory.md` (§13 of this document says how much of it is current).

**Two kinds of input the pipeline distinguishes:**

- **Digital PDF** — has a real text layer. The text layer is *ground truth*: it lets the checks catch dropped or invented content, and lets layout (font size, bold, alignment) be measured instead of guessed.
- **Scanned PDF / raw image** — pixels only. There is no ground truth. Tesseract OCR gives a second, independent reading, but it is **never treated as truth** and **never used to edit the output**.

---

## 2. Functionality catalogue (what is built)

All commands are run from the repo root with `.venv/bin/python`. "Model?" means the vision-language model. "Verifies output?" means the script re-opens what it wrote and prints a pass/fail line.

### 2.1 Conversion routes

| # | Route | Script | Model? | Verifies output? | Success signal (for a caller) |
|---|---|---|---|---|---|
| 1 | **PDF → Word** (.docx) | `test_pdf.py --docx X.docx` | **Yes**, per page | Yes (word coverage read back from the .docx) | Prints `Word document written to …`. **Exit code is always 0.** |
| 2 | **PDF → Excel** (.xlsx) | `test_pdf.py --xlsx X.xlsx` | **Yes**, per page | Yes (word coverage) | Prints `Excel file written to …`. Exit code always 0. |
| 3 | **PDF → PowerPoint** (.pptx) | `test_pdf.py --pptx X.pptx` | **Yes**, per page | Yes (word coverage) | Prints `PowerPoint deck written to …`. Exit code always 0. |
| (3b) | PDF → Markdown (by-product) | `test_pdf.py --out X.md` (also the default `extracted.md`) | Yes (same run) | — | Written on every run. |
| 4 | **Image → Word** (.docx) | `image_to_word.py` | **Yes** in `--mode text`; **No** in `--mode image` | Self-consistency + OCR cross-check only | Prints `-> path`; exit 1 if any image failed. |
| 5 | **Image → PDF** (also covers **JPG → PDF**) | `image_to_pdf.py` | **Yes** (+ LibreOffice) | Self-consistency + OCR cross-check only | Prints `-> path`; exit 1 if any failed. |
| 6 | **Image → Excel** (.xlsx) | `image_to_excel.py` | **Yes** | Self-consistency + OCR cross-check only | Prints `-> path`; refuses (exit 1) if no table is found. |
| 7 | **Word → PDF** | `word_to_pdf.py` | **No** (LibreOffice) | **Yes** (word, paragraph, table-cell and image coverage) | `verdict PASS/FAIL`; exit 1 on FAIL. |
| 8 | **PDF → JPG / JPEG** | `pdf_to_jpg.py` (`--ext jpeg` for the .jpeg ending) | **No** | **Yes** | `VERDICT: PASS/FAIL`; exit 1 on FAIL. |
| 9 | **PDF → TIFF** (one multi-page file) | `pdf_to_tiff.py` | **No** | **Yes** (pixel-exact) | `VERDICT: PASS/FAIL`; exit 1 on FAIL. |
| 10 | **TIFF → PDF** (searchable) | `tiff_to_pdf.py` | **No** (Tesseract OCR only) | **Yes** (pixel-exact picture) | `VERDICT: PASS/FAIL`; exit 1 on FAIL. |
| 11 | **PDF → TXT** | `pdf_to_txt.py` | **Yes**, per digital or scanned page | **Yes** for digital pages (read back, word by word against the PDF's text); scanned pages are reported **unverified** | `VERDICT: PASS / FAIL`; exit 1 on FAIL. |

**JPG → PDF:** there is no dedicated script. `image_to_pdf.py` accepts any image format, including JPG, and was tested on a JPG. The folders `jpg_to_pdf_input/` and `jpg_to_pdf_output/` exist for it (run with `--outdir jpg_to_pdf_output`). Two questions are still open for the owner (§10.3).

### 2.2 What each route does, and what it does not

**PDF → Word / Excel / PowerPoint (`test_pdf.py`)** — the most complete route.

- Handles digital and scanned PDFs page by page. A page with no text layer is "scanned"; `--scan-mode text` (default) reads it with the model and writes an editable transcript; `image` embeds the page picture and does *not* run the model (fast, not editable); `both` writes the transcript to `X.docx` and the page pictures to a separate `X_scan.docx`.
- Word output matches the source page size, margins and body font size, keeps headings, lists, captions and real tables (with merged divider rows), and inserts images taken byte-for-byte from the PDF at their original size and reading-order position.
- Excel output is **one sheet for the whole document**, tables as grids, prose in column A, **every value stored as text** (nothing is silently converted to a number), images embedded in their own cell.
- PowerPoint output is **one slide per PDF page, always** (a page is never split); the first heading becomes the slide title; layout is estimated because python-pptx cannot measure text.
- It does **not** reproduce fonts, colours, vector drawings/charts, or exact positions. Vector graphics are not extracted at all.
- Extras: `--pages 1-3`, `--dpi` (default 125), `--no-ocr`, `--show-reasoning`, `--base-url`, `--model`.

**PDF → TXT** (`pdf_to_txt.py`) — the model reads every page (digital or scanned) and returns it as blocks; the script writes headings and paragraphs as lines, lists as `- item`, tables as space-aligned columns, and separates pages with a line `----- Page N -----`. **Digital pages** are then corrected and checked against the PDF's text layer: `snap_to_text_layer` replaces the model's misreads with the PDF's exact words, dropped lines are restored from it, and the saved file is read back and compared word by word (coverage, words not in the PDF, repeated content, and any figure that differs). **Scanned pages** have no text layer, so nothing can be checked or restored: they are marked **UNVERIFIED** in the report, with Tesseract's agreement shown as information only. A failed model call falls back to the PDF's own text on a digital page; on a scanned page a notice is written in its place and the run fails. **Quality:** all 7 digital pages tested pass (4 of 7 before the text-layer correction was added); 2 scanned pages read cleanly. This is a small test set (§10.2).

**Image → Word / PDF / Excel** — one image is one page. A raw image has no text layer, so nothing verifies the model's reading except a Tesseract cross-check, which only *reports* disagreements. `image_to_word.py --mode image` embeds the picture unchanged (no model); `--mode both` writes two separate files. `image_to_excel.py` has no image mode by design.

**Word → PDF** — LibreOffice renders the .docx; the script then reads the PDF back and checks that words, paragraphs, table cells and images survived. Standalone: it deliberately shares no code with the rest.

**PDF → JPG** — PyMuPDF draws each page (150 dpi, quality 90 by default). One subfolder per PDF, one JPG per page. Verified by reading each JPG back: size, not blank, every PDF text line sits on ink, and an OCR word check (OCR is treated as evidence, not truth).

**PDF → TIFF** — one multi-page, colour, lossless-LZW TIFF per PDF at 300 dpi by default. Verified by reading the TIFF back: frame count, size, stored dpi, each frame **byte-for-byte equal to a fresh render**, not blank, text lines on ink, OCR word check.

**TIFF → PDF** — each frame is embedded losslessly and unchanged, and Tesseract adds an invisible text layer so the PDF is searchable. Page size comes from the TIFF's dpi. Handles 1-bit, greyscale, palette, CMYK, RGBA and 16-bit frames. Verified: page count, size, exactly one picture per page covering it, **picture byte-identical to the frame**, and the text layer present.

### 2.3 Cross-cutting features

- **Duplicate suppression** (`drop_duplicate_blocks`): removes text, list items and tables the model repeated.
- **Nested tables** (`merge_nested_tables`): merges a table-inside-a-table cell that the flat schema could only emit as separate tables. Verified live on a 3-level image.
- **Heading order fix** (`fix_trailing_heading_after_table`).
- **Repairs from the text layer** (digital PDFs only): dropped lines and table rows are restored *from the PDF's own characters*, never from OCR and never from a second model call.
- **Measured appearance** (digital PDFs only): alignment, size, bold, italic and heading/caption type are measured from the text layer, overriding the model's guesses.
- **Image extraction** from PDFs, byte-exact, placed in reading order.

### 2.4 Planned or requested but NOT built

| Item | State |
|---|---|
| CSV output (from any source) | Not built. |
| Image → PowerPoint | Not built. |
| Image → XML | Not built. |
| Human-review report of OCR-vs-model disagreements | Proposed, not built. |
| Skipping the model on digital pages (use the text layer) | Identified as the biggest latency lever, not built. |

### 2.5 Developer / utility scripts (not conversions)
`bench.py` (latency benchmark via Ollama — stale), `show_docx.py` (prints a .docx's real structure), `test.py`, `test_json.py` (early smoke tests; they reference `test_page.png`, which is not in this repo).

---

## 3. How many features are done

| Category | Count | Items |
|---|---|---|
| **Conversion routes built and working** | **11** | PDF→Word, PDF→Excel, PDF→PowerPoint, PDF→TXT (digital pages), Image→Word, Image→PDF (covers JPG→PDF), Image→Excel, Word→PDF, PDF→JPG, PDF→TIFF, TIFF→PDF |
| By-product outputs | 1 | Markdown from `test_pdf.py` |
| **Of those, using the AI model** | **7** | PDF→Word, PDF→Excel, PDF→PowerPoint, PDF→TXT, Image→Word, Image→PDF, Image→Excel |
| **Of those, with no AI model** | **4** | Word→PDF, PDF→JPG, PDF→TIFF, TIFF→PDF |
| Planned, not built | 4 | CSV, Image→PPT, Image→XML, review report |
| Dedicated JPG→PDF script | 0 | works through `image_to_pdf.py`; a dedicated script is an open question |

**Where each has been verified.** The whole pipeline was first built and tested on a previous laptop (`/home/aiteam/idox`). On the current machine (moved 2026-09-29) these were **re-run and confirmed**: `image_to_word.py`, `image_to_pdf.py` (on a JPG), `image_to_excel.py` (ran; failed on one hard image at 512 tokens, §6.3), `pdf_to_jpg.py`, `pdf_to_tiff.py`, `tiff_to_pdf.py`, and `test_pdf.py --scan-mode image`, and `pdf_to_txt.py` (built on this machine). **Not re-run on this machine:** `test_pdf.py` with the model on real pages, `word_to_pdf.py`.

---

## 4. How the features were made

### 4.1 Architecture

```
input ──► Tier 0: deterministic parse ──► Tier 1: the model (ONE call per page) ──► post-processing ──► Tier 3: deterministic writer ──► verify
```

- **Tier 0 (PyMuPDF):** reads the PDF's text layer, renders each page to a PNG (125 dpi), extracts embedded images byte-for-byte, measures page geometry. `is_scanned = not page_text.strip()` decides per page.
- **Tier 1 (model):** `blocks.extract_page()` sends one page image with a fixed system prompt and gets back JSON. **One call per page. The model is called on digital pages too** (even though the text layer already holds the content) — a known inefficiency (§10.4).
- **Post-processing (plain code):** `drop_duplicate_blocks` → `fix_trailing_heading_after_table` → `merge_nested_tables`, then checks and (digital pages only) repairs.
- **Tier 3 (writers):** `to_docx.py`, `to_xlsx.py`, `to_pptx.py`; LibreOffice for Word→PDF. **The model never writes Office bytes.** This boundary is intentional and load-bearing.

### 4.2 The schema (the contract between the model and the code)
Defined in `blocks.py` with pydantic; used for schema-constrained JSON decoding.

```
Page
 ├─ analysis: LayoutAnalysis      (n_blocks: int, table_column_counts: [int])   ← written FIRST
 └─ blocks: [ TextBlock | TableBlock ]                                            ← written SECOND
TextBlock  : kind (heading|paragraph|caption|list|image), text, align, size, bold
TableBlock : kind="table", has_header, n_data_rows, n_cols, header[], rows[][]
```
A trimmed variant (`PageNoLook` / `TextBlockNoLook`, without `align/size/bold`) is used for digital pages, because those three values are measured from the text layer afterward; asking for them was pure wasted generation (measured 17 tokens per block).

**Why the schema looks like this:** constrained decoding enforces *shape*, never *meaning*. Rules stated only in words are advisory and a small model drops them, so the schema was reshaped instead (separate text and table block types, counts before rows, no fields with defaults). The full reasoning is in the `blocks.py` module docstring.

### 4.3 The model call
`blocks._extract_page_raw_server()` POSTs to `<base_url>/v1/chat/completions` (OpenAI-compatible, served by llama.cpp's `llama-server`) with:

- the system prompt (`SYSTEM_PROMPT`, 8 numbered rules) and a one-line user prompt, plus the page image as a base64 data URL;
- `response_format: json_schema` (the pydantic schema), `temperature: 0`, `repeat_penalty: 1.15` (fixes a confirmed bug where greedy decoding re-emitted blocks), `n_predict: 4000` (a hard ceiling; hitting it raises an error);
- a **600 s** HTTP timeout.

The response's `timings` block gives real server-measured prefill and generation times, which the scripts print.

### 4.4 Validation and repair (the four checks)

1. **`check_structure`** — did the model contradict itself (claimed rows vs returned rows, ragged rows)?
2. **`check_grounding`** — is every string the model wrote really in the ground truth? (digital: the PDF text layer; raw image/scan: an OCR reading)
3. **`check_geometry`** — advisory comparison with PyMuPDF's own table detector (which is itself unreliable).
4. **`check_coverage`** — is everything in the ground truth present in the output? This is the check that catches silently dropped content.

For **digital PDFs**, missing lines and missing table rows are then **restored from the PDF's own text** (a copy, not a second reading). For **scans and images**, OCR disagreements are only **reported**.

### 4.5 The writers

- `to_docx.py` — python-docx. Matches source page geometry; pads/truncates ragged rows instead of crashing; merges spanning divider rows; explicit 6 pt spacing around tables; images placed by "fraction of text above them".
- `to_xlsx.py` — openpyxl. One sheet, text-only cells (a leading `= + - @` is stored with a quote prefix so it is never parsed as a formula), images in their own framed cell.
- `to_pptx.py` — python-pptx, 16:9, "Title Only" layout, body positioned below the real title box, capped against the space actually left on the slide.
- All three have a `verify_*` function that reads the saved file back and reports word coverage.

### 4.6 The no-model routes (how they work)

- **PDF → JPG / TIFF:** PyMuPDF `get_pixmap(dpi=…, alpha=False)` draws each page exactly as a viewer would; Pillow writes JPG or TIFF. A page declared implausibly large (some scanners write the page box in pixels) is drawn at a lower dpi (cap: 5,000 px on the long side). TIFF frames are generated one at a time so memory stays flat.
- **TIFF → PDF:** each frame → RGB → embedded as a lossless PNG stream on a PDF page sized `pixels / dpi × 72`; Tesseract's *text-only* PDF (`-c textonly_pdf=1`) is overlaid with `show_pdf_page`. OCR runs on 4 pages at a time; each Tesseract process is limited to one thread.
- **Word → PDF:** LibreOffice headless with a throwaway profile (otherwise a LibreOffice window already open on the desktop silently swallows the call).

### 4.7 The verification pattern (used by the newest scripts)
Every newer script ends with `VERDICT: PASS` or `FAIL`, exits 1 on FAIL, and works like this: **re-open the file just written, and compare it against the source by a check that does not depend on the thing being tested.** The checks were each proven able to *fail* by deliberately damaging output (a single changed pixel, a blank page, a missing frame, a wrong size, a failed text layer). A check that has only ever passed was not considered proven.

Two lessons worth knowing:

- **OCR is a weak judge.** It misses text printed white on dark backgrounds and turns accented letters into plain ones (English-only). On a real 10-page PDF, OCR "failed" 3 pages whose images were byte-exact. The owner has not yet decided whether OCR should be advisory-only in `pdf_to_jpg.py` and `pdf_to_tiff.py` (§10.3).
- **Removing a "redundant" check can remove the only independent one.** In `tiff_to_pdf.py`, dropping a duplicate OCR pass briefly let a broken text layer pass as success. Fixed by making a failed text layer an explicit failure.

### 4.8 Bugs found and fixed (selection)
Full list with root causes: `memory.md` §5, §10, §14, §19, §20. Highlights: duplicate blocks from greedy decoding; false "centre" alignment on ordinary paragraphs; bullet-character mismatch that made the *repair step itself* insert duplicates; a `sorted()` crash comparing image dicts; silent fallback to a different model; nested tables; tables sitting flush against text; the upscaling step that turned out to be pointless once image tokens were capped (removed).

---

## 5. AI models: how many, and where they are used

### 5.1 Inventory

| Model / engine | What it is | Size / build | Where it runs | Status now |
|---|---|---|---|---|
| **Qwen3-VL-2B-Instruct** | Vision-language model (the extractor) | Q4_K_M weights (1.1 GB) + `mmproj` vision encoder at **Q8_0** (445 MB), from Qwen's official HF repo `Qwen/Qwen3-VL-2B-Instruct-GGUF`, files in `models_manual/` | **This machine**, `llama-server` on `http://127.0.0.1:8090`, **CPU only** | **In use now, running** |
| **Qwen3-VL-8B-Instruct** | Same family, larger | Q4_K_M | **Remote machine** `http://10.0.3.33:8080` (a colleague's, GPU/accelerated) | **Configured as the code default (`DEFAULT_BASE_URL`), currently DOWN** |
| **Qwen3.6-35B-A3B** (tuhin-ai API) | Vision-language model, much larger. The server reports `Qwen3.6-35B-A3B-UD-IQ4_XS.gguf`, capabilities `completion` and `multimodal` | 4-bit quantised GGUF | **A colleague's laptop** on the office network, `http://10.0.3.2:8080` (the name `tuhin-ai.local` did not resolve on this machine) | **In use through the web app; verified with this pipeline 2026-09-30.** Needs an API key. |
| Qwen3-VL 4B / 2B via **Ollama** | Same family, Ollama's bundled builds | `qwen3-vl:4b-instruct`, `qwen3-vl:2b-instruct` | Ollama daemon | **Legacy path.** Used only if `--base-url ""` is passed. Ollama is **not installed** on this machine. `blocks.MODEL_NAME` still names the 4B. |
| **Tesseract 5.5.0** | OCR engine — recognises characters; it is not a language model (language data: `eng`, `osd` only) | system package | this machine | Used for verification and, in TIFF→PDF, to *produce* the text layer |

**So: one model family (Qwen3-VL) is used, deployed in two sizes (2B local and active, 8B remote and down), plus Tesseract.** No other AI model is used anywhere. LibreOffice, PyMuPDF, Pillow and the Office libraries are not AI.

### 5.2 Where each is called

| Route | Qwen3-VL | Tesseract | Notes |
|---|---|---|---|
| PDF→Word/Excel/PPT (`test_pdf.py`) | **One call per page**, digital and scanned alike (skipped only for `--scan-mode image` on a scan) | Only for **scanned** pages, as a checker | Model call is at the Tier 1 step in `test_pdf.py` (`extract_page(..., include_look=is_scanned)`). |
| **PDF→TXT** (`pdf_to_txt.py`) | **One call per digital or scanned page** (trimmed schema); none for blank pages | Scanned pages only, as an advisory comparison | On a digital page a failed call falls back to the PDF's text layer. |
| Image→Word (`--mode text`/`both`) | **One call** | Cross-check (`ocr.ocr_image`) | `--mode image` uses neither. |
| Image→PDF | **One call** | Cross-check | Then LibreOffice. |
| Image→Excel | **One call** | Cross-check | |
| Word→PDF | — | — | LibreOffice only. |
| PDF→JPG | — | Verification only | |
| PDF→TIFF | — | Verification only | |
| **TIFF→PDF** | — | **Builds the text layer** (functional, not just a check) | |

The **only** place the model is invoked in the code is `blocks.extract_page()` → `_extract_page_raw_server()` (`blocks.py`, ~line 1025 and 1134).

### 5.6 The tuhin-ai API (added 2026-09-30)
- **Address:** `http://10.0.3.2:8080` (office network only). The friendly name `http://tuhin-ai.local:8080` did not resolve on this machine; the IP did. Pass the address **without** `/v1`; the code adds `/v1/chat/completions`.
- **Auth:** `Authorization: Bearer <key>`. The key comes from the API's owner. **It is never stored in the repo, the docs or the job files.** The code reads it from the environment variable `IDOX_API_KEY`; the web app keeps it in memory only and passes it to each script through that variable.
- **Settings the code now reads from the environment** (all optional; with none set the request is exactly what it was for the local server): `IDOX_API_KEY`, `IDOX_THINKING` (`off` or `on`, sent as `chat_template_kwargs.enable_thinking`; the API owner advises **off** for extraction), `IDOX_MAX_IMAGE_SIDE` (shrink the image, the owner advises at most 1600 px), `IDOX_TIMEOUT` (default 600 s; the owner advises 10 minutes or more), `IDOX_MODEL`.
- **What was verified against the live API:** the key is accepted and a wrong key returns 401; an image plus the page JSON schema plus thinking off returns a valid page; the web app's connection test and a read of a generated test image both pass (5.4 s warm, about 30 s on the first request after idle).
- **API limits, as documented by its owner (not measured by us):** two requests run at once and more queue; about 9-12 tokens/s writing; an image is capped at about 1,000 tokens; the first request after a restart is slower; office network only.
- **Measured through this pipeline (2026-09-30):** `hi.png` to Word **26.7 s** alone; one digital PDF page to text **61.7 s** the first time and **25.2 s** on an immediate repeat (the server probably reused its read of the identical image); three jobs at once (two running, one queued) took **80 s, 110 s and 202 s**, because they shared the API laptop. For comparison the local 2B did `hi.png` in about 27 s. **So the API is not faster than the local 2B for this pipeline. Whether its output is more accurate has not been measured.**
- **Not tested against the API:** scanned PDFs, PDFs with many pages, pages with complex tables, and two people using it at once.
- **Main risk:** the pipeline relies on the server honouring the JSON schema. It did for every page tried; a page type that breaks it would show up as a failed job with the server's message.

### 5.3 How the endpoint is chosen (important for integration)

- `blocks.DEFAULT_BASE_URL = "http://10.0.3.33:8080"` (remote 8B) and `blocks.LOCAL_BASE_URL = "http://127.0.0.1:8090"`.
- Every model script has `--base-url` defaulting to `DEFAULT_BASE_URL`. **Because the remote is down, you must pass `--base-url http://127.0.0.1:8090` on every model run** until it returns (or change the constant, which was deliberately left untouched pending the owner's decision).
- An **empty** `--base-url ""` switches to Ollama.
- History: the default was once silently falling back to Ollama's model when the flag was omitted, which produced visibly worse output. That is why the default is now an explicit constant.

### 5.4 Serving configuration on this machine
```bash
setsid nohup ~/.local/opt/llama.cpp/llama-b11247/llama-server \
  --model  <repo>/models_manual/Qwen3VL-2B-Instruct-Q4_K_M.gguf \
  --mmproj <repo>/models_manual/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
  --port 8090 --host 127.0.0.1 --no-webui --ctx-size 8192 \
  --image-min-tokens 512 --image-max-tokens 512 \
  > ~/.local/share/idox/llama-server.log 2>&1 < /dev/null & disown
```

- **`--image-min-tokens 512 --image-max-tokens 512` is the current setting** (it was 1024 minimum until 2026-09-29). It cuts latency a lot but costs accuracy on dense pages (§6.3). Qwen's own server warning says Qwen-VL needs at least 1,024 image tokens for accuracy.
- Health check: `GET http://127.0.0.1:8090/health` → `{"status":"ok"}`. Stop it with `kill $(pgrep -x llama-server)`. **Do not use `pkill -f` with the binary path** — it also matches the shell running the command and kills it (this happened twice).
- CPU only. **Never enable GPU/Vulkan offload** on this hardware (it froze the previous laptop and needed a forced reboot). The CPU-only build is deliberately installed.

### 5.5 Model behaviour you should know

- **`temperature 0` is not perfectly reproducible** (multi-threaded floating-point order plus server cache reuse). The same page can drop different content on different runs. This is the reason "retry" (not a code patch) is the lever for what remains of the dropped-content problem.
- **Alignment on scans/images is unreliable**: the model is biased toward "centre" for short bold text. There is no text layer to measure from. Unresolved (§10.4).
- **Character-level misreads occur at 512 image tokens on the 2B** (e.g. "nascetur" read as "nescetur"). Whether the cap is the cause was not tested at 1,024.

---

## 6. Latency as of now

**Hardware for every number below unless stated:** Intel i7-1165G7, 4 cores / 8 threads, 14 GB RAM, CPU only, `powersave` governor, Ubuntu 26.04, llama.cpp b11247. **Model = the local Qwen3-VL-2B** unless stated. Numbers come from the server's own timers (prefill = reading the image and prompt; generation = writing the JSON).

### 6.1 What determines the time

- **Generation dominates.** Each output token needs the whole model read from RAM once, so time grows with output length, not with anything the CPU can speed up: about **8–12 tokens/second** on this laptop.
- **Prefill scales with image tokens.** At the 512-token cap it measured **12–19 s**; at ~1,550 image tokens it was **65 s**.
- **Rule of thumb at the current 512-token setting (fitted to the three runs below that completed, not a guarantee):** total time per page ≈ **13 s + (output tokens ÷ 12)**. It reproduces `hi.png` (131 tokens → ~24 s), `sample2.png` (653 → ~67 s) and the JPG→PDF page (556 → ~59 s). Generation slows to about 8 tokens/s on long outputs (`complex.png`: 1,095 tokens took 131 s), so a dense nested-table page takes 2–3 minutes or more.
- A **multi-page PDF is processed page by page, sequentially**, so time is roughly the sum. A 10-page dense PDF through the model was **not measured on this machine**; expect on the order of 10 minutes or more.

### 6.2 Measured runs (this machine, local 2B)

| Test | Image tokens setting | Prefill | Generation | Total |
|---|---|---|---|---|
| `hi.png` (2 short lines) | min 1024 (2,089 prompt tokens) | 61.0 s | 12.9 s / 131 tok (10.1 tok/s) | **74.1 s** |
| `hi.png` | 512 (1,138 tokens) | 17.4 s | 11.0 s / 132 tok (11.9 tok/s) | **28.4 s** |
| `hi.png`, after removing the upscale step | 512 (1,170 tokens) | 13.4 s | 10.6 s / 131 tok | **24.0 s** |
| `sample2.png` (content not inspected here) | 512 (~500 image tokens) | 12.4 s | 54.7 s / 653 tok (11.9 tok/s) | **67.1 s** |
| JPG→PDF test page (paragraphs + table + logo) | 512 | 12.4 s | 47.4 s / 556 tok | **60.0 s** (62 s wall incl. OCR + LibreOffice) |
| `complex.png` (3-level nested table) | ~1,550 image tokens (min 1024) | 65.1 s | 130.8 s / 1,095 tok (8.4 tok/s) | **195.9 s** (owner saw 176 s in the terminal) |
| `complex.png` | **512** | 19.2 s | 560 s / 4,000 tok (7.1 tok/s) | **FAILED**: hit the 4,000-token output cap after 9 min 40 s, no file written |

### 6.3 The 512-token trade-off (open decision)

- 512 image tokens cut a simple page from 74 s to ~24–28 s.
- On the hardest test image (`complex.png`) it **failed outright**, and on a cleaner test page it produced letter-level misreads. It was run **once** on `complex.png`, so this does not prove it always fails on dense pages.
- The owner chose 512 to reduce latency. An intermediate value (768) was proposed and **not tested**.

### 6.4 Other measurements

- **Remote 8B (measured 2026-09-28 when it was up; it is down now):** prefill 14.07 s vs ~68 s for the 2B on the identical page, generation ~19.25 vs ~12–13 tok/s. The 8B was faster despite being larger, because the remote hardware is far faster; model size did not decide it.
- **Previous laptop (i7-1185G7), stale:** the canonical benchmark `text_only_1page.pdf` took 154.10 s (prefill 68.72 s / 2,077 tokens; generation 85.08 s / 1,089 tokens). Recorded before most later changes. Do not quote it as current. The very first Ollama 4B baseline was 257.6 s (`bench_results.json`).
- **Settled questions (do not re-test):** vision-encoder precision (F16 vs Q8_0) makes no meaningful difference (107.9 s vs 108.83 s); iGPU offload helped prefill only and froze the machine; the `performance` CPU governor was *slower* (192.94 s vs 154.10 s) due to thermal throttling.
- **Latency test hygiene:** the server caches the fixed prompt prefix. Restart the server (or vary the input) between comparisons, or the second run looks artificially fast. An earlier 256/512-token comparison was wrong for exactly this reason and had to be redone.

### 6.4b PDF → TXT (model, measured this session)
Local 2B, 512 image tokens, trimmed schema. Per digital page: **22–103 s** (generation 10–89 s for 123–981 output tokens; prefill about 13 s). Two-page `sample_digital_document.pdf`: 51–75 s. `Free_Test_Data` page 1 (a long dense page, 981 tokens): 103 s. Scanned pages (2 tested): **18–20 s each** (200–225 output tokens). Blank pages cost nothing. A 10-page dense PDF was not run in full. The server caches repeated inputs, so a repeat run can show a near-zero prefill.

### 6.5 Non-model routes (measured this session)

| Route | Test | Time |
|---|---|---|
| PDF → JPG | 3-page PDF, 150 dpi, with OCR verification | ~2.0 s; render only ~0.2 s |
| PDF → TIFF | 3 pages, 300 dpi | 1.0 s to write, 5.6 s with verification |
| PDF → TIFF | 10 pages, 300 dpi | 1.9 s to write, 13.3 s with verification |
| TIFF → PDF | 10 frames, 2550×3300 px (`sample10.tiff`) | **11.7 s** (4 OCR workers); 21 s with 1 worker; 40.5 s for the version before the speed-up |
| Tesseract alone | one 300 dpi page | ~1.6 s |
| Word → PDF | — | **not measured this session** |

Peak memory for `tiff_to_pdf.py` on `sample10.tiff` was about 600 MB.

### 6.6 What this means for a chat experience

- Anything through the model needs **background execution with progress**, not a blocking reply. Expect from ~25 s (one simple page) to 3+ minutes (one dense page) to many minutes (multi-page).
- Non-model routes are near-instant to ~20 s and could be answered almost synchronously.
- **Concurrency on the model server was never tested.** Assume one page at a time per server until measured; the CPU is fully used by a single request.

---

## 7. Exposing the features to a chatbot or other services

### 7.1 Is there a plan today?
**No.** A search of the code and every document in the repo found no API, service, queue, or chat integration, and no written plan for one. The project owner has said that **another developer (you) will build a chat option that triggers these conversions**, and that this handover exists for that purpose. Everything below is therefore **a recommendation from reading and running the code, not a decision already made.** The decisions that need the owner are listed in §7.7.

### 7.8 A working reference: the local web app (added 2026-09-30)
`idox_app.py` (server, standard library only) and `idox_app.html` (the page). Run `.venv/bin/python idox_app.py`, open `http://127.0.0.1:8765`.
- **What it does:** upload one or more files; each gets a "Convert to" list based on its type (PDF, image, Word, TIFF, 14 conversions in all) and optional pages, dpi and scanned-page settings; jobs queue and run as subprocesses, each in its own folder under `~/.local/share/idox/jobs/<id>/`; progress, live status, cancel, log, per-file download and one `.zip`; a Settings panel for the engine (tuhin-ai API or local), address, key, thinking, image size and wait limit, with a connection test and a "read a test image" check.
- **How it decides the outcome** (because several scripts exit 0 even when a page failed): output files present, then the log is scanned for explicit failure lines and `VERDICT: FAIL`. States: **Done**, **Done, please check** (files exist but a check failed or a page was skipped), **Failed**, **Cancelled**.
- **Safety:** binds to 127.0.0.1 only; refuses a Host that is not localhost and a write whose Origin is another site; only the 14 listed routes can run; arguments are passed as a list (no shell); uploads are renamed to a safe stem; downloads cannot leave the job folder; the key never reaches the browser, a file or a command line.
- **Concurrency:** at most 2 model jobs on the API at once (the API's documented limit), 1 on the local model, 3 other jobs.
- **Tested (2026-09-30):** all no-model routes, three model jobs through the real API, cancel (stops within 2 s, no process left), wrong key, switching to the local model, forgetting the key, 17 safety checks, a restart (16 jobs reloaded from disk), and the page in a real browser (light, dark, 400 px wide, no script errors).
- **Not tested:** a very large upload, several users at once, long PDFs, and running it on another operating system.
- **For the chat developer:** the same job model (upload, queue, poll, download) and the same outcome rules can sit behind a chat. Reuse `ROUTES` and `build_command()` in `idox_app.py` as the intent-to-command table.

### 7.2 What exists today

- Every feature is a **command-line script** that reads files from disk and writes files to disk. There is **no importable function** per feature.
- Input/output is **folder-based**: each route has an `*_input/` and `*_output/` folder (§12).
- Results are communicated by **printed text** (and, for newer scripts, a `VERDICT:` line and an exit code). There is no structured (JSON) output.

### 7.3 What blocks a direct integration (all verified)

| Constraint | Detail |
|---|---|
| **Scripts run at import** | `test_pdf.py`, `image_to_word.py`, `image_to_pdf.py`, `image_to_excel.py`, `word_to_pdf.py`, `pdf_to_jpg.py`, `pdf_to_tiff.py`, `tiff_to_pdf.py`, `pdf_to_txt.py` (and `bench.py`, `show_docx.py`) run `argparse` and their whole job as top-level code. Importing one starts a conversion and hijacks your process's arguments. (This is why the project copies small helpers instead of importing across scripts.) |
| **Importable modules** | Only `blocks.py`, `ocr.py`, `to_docx.py`, `to_xlsx.py`, `to_pptx.py`. These are the real building blocks and can be imported. |
| **Inconsistent exit codes** | `test_pdf.py` **always exits 0**, even when a page failed. The others exit 1 on failure. A missing input file is *skipped with a message and exit 0*. |
| **Output overwrites silently** | Re-running with the same name overwrites the previous output. |
| **Relative paths** | Defaults and scratch space (`render/`, `*_output/`) are relative to the current directory. |
| **Long-running** | Model routes take minutes (§6). |
| **Endpoint default is dead** | See §5.3. |
| **Failure modes to surface** | model endpoint down (connection refused); 4,000-token output cap ("model hit the 4000-token generation limit"); 600 s timeout; Tesseract or LibreOffice not installed; password-protected PDF; an empty result from a page the model could not read. |

### 7.4 Isolation and concurrency (tested)
Because paths are relative to the working directory while imports resolve from the script's own folder, **running each job from its own empty directory works**:
```bash
mkdir -p /jobs/<id> && cd /jobs/<id> \
  && /path/to/idox/.venv/bin/python /path/to/idox/test_pdf.py in.pdf \
       --base-url http://127.0.0.1:8090 --docx out.docx --out out.md
```
Tested with **two simultaneous `test_pdf.py` jobs** (using `--scan-mode image`, no model): each got its own `render/` folder and neither touched the repo's. Also tested from a foreign working directory: `pdf_to_jpg.py` and `pdf_to_tiff.py`. **Not tested:** two simultaneous *model* jobs against one server.

### 7.5 Recommended approach (proposal)

**Phase 1 — no refactor (fastest, works today):** a thin service that runs the scripts as **subprocesses** (argument *lists*, never a shell string), one job directory per request.

- Map the chat intent to a route (table below), validate the file type, build the command, run it in `/jobs/<id>/`, stream stdout for progress, then collect the output files.
- Decide success by **`VERDICT:`/exit code where available**, and for `test_pdf.py` and the `image_to_*` scripts by **the output file existing plus the absence of `FAILED`/`EXTRACTION FAILED` in the log** (do not trust exit 0).
- Return the file(s) and a short summary (page count, verdict, warnings such as "scanned pages were verified only as drawn").

**Phase 2 — a proper library (recommended long-term):** the scripts already delegate the real work to importable modules. Extract each script's `run()` into a function that **returns a result object** (output paths, per-page results, verdict, warnings) instead of printing, and make the CLI a thin wrapper. A `run(...)` function already exists in every script except `test_pdf.py` (which is one long top-level loop and needs more work), so for those it is mostly a matter of moving the argparse block under `if __name__ == "__main__":` and returning results instead of printing. This removes the import problem, gives structured results, and lets a service run without parsing logs.

**Other recommendations:**

- **Job queue with a single model worker.** One model request at a time per server until concurrency is measured.
- **Make the model endpoint configuration explicit** (an environment variable or config file read by your service), so the service is not tied to the dead default.
- **Pre-flight checks** in the service: is `llama-server` healthy, are `tesseract` and `soffice` on the PATH, is the file readable. Fail fast with a plain message.
- **Keep it offline and private.** The project's stated goal is fully local processing, and the `.gitignore` marks real documents as data that must stay local. Do not send documents to an external service, and clean up job directories.
- **Treat file names as untrusted** (path traversal), and enforce size and page limits: an oversized or huge-page PDF will be slow or memory-heavy.

### 7.6 Intent → command mapping (what a chat layer would call)

Replace `<job>` with a per-job directory. `MODEL` = `--base-url http://127.0.0.1:8090` (needed while the remote 8B is down).

| User asks for | Command | Model? | Typical wait |
|---|---|---|---|
| PDF → Word | `test_pdf.py in.pdf MODEL --docx out.docx --out out.md` | Yes | minutes/page |
| PDF → Excel | `test_pdf.py in.pdf MODEL --xlsx out.xlsx --out out.md` | Yes | minutes/page |
| PDF → PowerPoint | `test_pdf.py in.pdf MODEL --pptx out.pptx --out out.md` | Yes | minutes/page |
| Image → Word | `image_to_word.py img MODEL --outdir <job>` (`--mode image` for a picture-only Word file, no model) | Yes / No | ~25–200 s |
| Image / **JPG → PDF** | `image_to_pdf.py img MODEL --outdir <job>` | Yes | ~25–200 s |
| Image → Excel | `image_to_excel.py img MODEL --outdir <job>` | Yes | ~25–200 s (fails if no table) |
| Word → PDF | `word_to_pdf.py in.docx --outdir <job>` | No | seconds (not measured) |
| PDF → JPG | `pdf_to_jpg.py in.pdf --outdir <job>` | No | ~1–2 s |
| PDF → TIFF | `pdf_to_tiff.py in.pdf --outdir <job>` | No | seconds |
| TIFF → PDF | `tiff_to_pdf.py in.tiff --outdir <job>` | No | ~1–2 s/page |
| PDF → TXT | `pdf_to_txt.py in.pdf MODEL --outdir <job>` (digital pages verified; scanned pages read by the model and reported unverified) | Yes | ~20–100 s per page |
| anything → CSV / Image → PPT / XML | **not built** | — | — |

**Parameters a chat layer could expose:** `--pages` (test_pdf, pdf_to_jpg, pdf_to_tiff, pdf_to_txt); `--dpi` and `--quality` (pdf_to_jpg), `--dpi` and `--compression lzw|deflate` (pdf_to_tiff); `--no-text` (tiff_to_pdf: picture-only PDF); `--scan-mode text|image|both` (test_pdf); `--mode text|image|both` (image_to_word).

**Output files a job produces:** e.g. `<job>/out.docx`, `<job>/out.md`; `pdf_to_jpg` → `<outdir>/<name>/page_001.jpg …`; `pdf_to_tiff` → `<outdir>/<name>.tiff`; `tiff_to_pdf` → `<outdir>/<name>.pdf`; `--scan-mode both` also writes `<docx>_scan.docx`; `image_to_word --mode both` writes `<name>.docx` and `<name>_scan.docx`.

### 7.7 Decisions needed from the project owner before / during integration

1. **Which endpoint** should services use, and should `DEFAULT_BASE_URL` change (it is dead, and left unchanged deliberately)?
2. **Image-token setting: 512 or 1,024?** It changes both latency and accuracy (§6.3).
3. **Is the 2B enough** for a chat product, or is the remote 8B (faster and larger, but on a colleague's machine) the target?
4. **Do you want the refactor** (Phase 2), and should it be done inside this repo?
5. **What should the chat say about accuracy?** Scans and images have no ground truth; only digital PDFs get strong content checks (§4.4). The verification report is honest about this, and the chat should relay it.

---

## 8. Environment and setup

### 8.1 This machine (set up 2026-09-29, all in user space)

- Ubuntu 26.04, i7-1165G7, 14 GB RAM, Iris Xe graphics (unused on purpose).
- **Python 3.13.15** via **uv** (`~/.local/bin/uv`). `.venv` built from `requirements.txt`. Add packages with `uv pip install --python .venv/bin/python <pkg>` (uv venvs have no `pip`). The old Python 3.12 venv was moved, not deleted, to `~/.local/share/idox-old-venv-py312`.
- **llama.cpp `llama-server` b11247, CPU-only build** in `~/.local/opt/llama.cpp/llama-b11247/`. Log: `~/.local/share/idox/llama-server.log`.
- System packages now installed: `git`, `tesseract-ocr` (5.5.0), `libreoffice` (26.2.5.2). `curl` is installed too (`/usr/bin/curl`).
- Model weights in `models_manual/` (git-ignored; ~1.5 GB).

### 8.2 Setting up a fresh machine

1. `sudo apt-get install -y git curl tesseract-ocr libreoffice-writer`
2. Install uv, then: `uv python install 3.13 && uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt`
3. Download a CPU-only `llama-server` release (llama.cpp GitHub releases; the `ubuntu-x64` build, **not** the Vulkan one).
4. Get the two GGUF files from `Qwen/Qwen3-VL-2B-Instruct-GGUF` on Hugging Face (`Qwen3VL-2B-Instruct-Q4_K_M.gguf` and `mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf`) into `models_manual/`.
5. Start the server (§5.4) and check `/health`.
6. Smoke test: `.venv/bin/python image_to_word.py image_to_word_input/hi.png --base-url http://127.0.0.1:8090 --outdir /tmp/smoke` (expect 2 blocks, ~25–75 s).

`requirements.txt` pins: pymupdf 1.28.2, pydantic 2.13.5, python-docx 1.2.0, openpyxl 3.1.5, python-pptx 1.0.2, pillow 12.3.0, pytesseract 0.3.13, requests 2.34.2, ollama 0.6.2 (only for the legacy Ollama path).

---

## 9. Current state

- **Git:** branch `feature/testing_2b_Q8mmproj`, HEAD `de9c634`. **Uncommitted:** `idox_app.py` and `idox_app.html` (new), edits to `blocks.py` (API key, thinking flag, image cap, clearer errors), `pdf_to_jpg.py` (`--ext`), `memory.md`, this file, the two `pdf_to_jpeg_*` folders, and `pdf_to_word_input/new.pdf`. `sample_test_document.pdf` and `handover_chat_integration.html` show as deleted (not by the assistant).
- **Server running now:** local 2B on `127.0.0.1:8090`, `--ctx-size 8192`, `--image-min-tokens 512 --image-max-tokens 512`. **Remote 8B: down.**
- **Documents:** `memory.md` and `status2.md` were kept current through 2026-09-30. `handover.md` and `status.md` are old (§13).
- **Folders `jpg_to_pdf_*`** exist and are empty. `pdf_to_txt_input/` and `pdf_to_txt_output/` exist for `pdf_to_txt.py`.

---

## 10. Known issues, gaps and open decisions

### 10.1 Suspected bugs found by reading the code (**not verified by running**)
Also listed in `memory.md` §16.

1. **`merge_nested_tables` with a parent that has no header:** the merged header and width come from the child only, so the parent's earlier rows are cut to that width and the expanded rows come out longer than the header.
2. **`merge_nested_tables` false merges:** any table whose last cell is blank, followed directly by another table, is merged (e.g. two separate tables where the first ends in a totals row with an empty last cell).
3. **`fix_trailing_heading_after_table` on multi-page PDFs:** a heading at the bottom of a page often introduces the *next* page; it would be moved above the table before it.
4. **Bullet stripping in `to_docx.py`:** `strip(" •◦▪-–\t")` trims both ends of each list item, so "-5% change" loses its minus sign.
5. Minor: `_PUNCT` folds "·" to "-"; the raw-server request labels every image `image/png` even for a JPEG; `test.py`/`test_json.py`/`bench.py` reference files not in the repo; `xlsx_output/` is git-ignored but its files are tracked.

### 10.2 Verified gaps

- **`test_pdf.py` skips a failed page silently.** If the model call for a page raises, the script prints `Tier 1 FAILED …` and moves on; that page is then also absent from the source text used for the final content check, so the check can still report 100%. Only the console shows it. It also always exits 0. (Not fixed; offered to the owner.)
- **The OCR word check in `pdf_to_jpg.py` and `pdf_to_tiff.py` can fail good output** (§4.7). E.g. 3 of 10 pages of `ten_page.pdf` failed only on OCR while being byte-exact. Fix options offered, not chosen (§10.3).
- **A text layer can differ from what is visible.** In `ten_page.pdf` the rupee sign renders as a black box (the PDF's font lacks the glyph) and the text layer stores it as the letter "I". Any text extraction gives `I1,250.00`.
- **`test_pdf.py` can produce a Word file that is missing whole pages while reporting 100%, and its timing summary ignores failed pages.** Shown on a real 2-page report (local 2B): page 2 hit the 4,000-token cap after 544 s and was skipped; the Word file held only page 1 in scrambled order; the tool printed "content check: 100%" and "TOTAL 37.28 s" while the real run took 595.5 s. A caller must not trust that script's exit code or those two lines; check the page count in the log (`Tier 1 FAILED`). Details: `memory.md` §23.
- **In `test_pdf.py`, the repair step can still duplicate content.** When the model misreads a paragraph, `check_coverage` reports the correct line as missing and `repair_missing_lines` inserts the exact PDF line beside the misread one. `pdf_to_txt.py` no longer has this problem, because `blocks.snap_to_text_layer` corrects the misread first. `test_pdf.py` does not call it yet (a one-line change plus testing; not done).
- **PDF → TXT was tested on a small set:** 7 digital pages and 2 scanned pages. Multi-column layouts were not tested. The correction step is deliberately conservative (invented text and real-but-different words are left alone, checked on synthetic cases), but how often it wrongly "corrects" a word on other documents is unknown. Symbols are not corrected: a PDF's own text layer can hold garbage for them.
- **Scanned pages are never verified** (no ground truth). The OCR figure is advisory: it misses white-on-dark text and accents.
- **The model sometimes drops content on scans, and the cause is known** (measured on 2 scanned pages, small samples). It is **not** a reading failure: read from a tight strip, the model gets the same lines exactly right every time. In full-page reads it skips items at random, often the last one ("Date"; a final paragraph), and different items on different runs. A thin hyphen was lost on 9 of 9 reads of a rendered page (higher render dpi did not help) and correct on 4 of 4 reads of the scan's own embedded image; but that route lost whole lines on another page (4 of 4 reads against 1 of 4), so `pdf_to_txt.py --scan-image` is experimental and off by default. The step that fixes both, OCR finds the line and **the model re-reads just that strip**, works (6 of 6 exact, about 16–19 s per line) but its placement logic is not good enough yet and it is not built into the script (`memory.md` §21.2).
- **A broken font can make the text layer differ from what is visible.** `ten_page.pdf` page 3 shows a black box for the rupee sign while its text layer says `I1,250.00`; the model wrote `$1,250.00`, and `pdf_to_txt.py` correctly flags the difference.
- **The model runs on digital pages that already have a text layer** (§4.1).
- **No output verification** for the `image_to_*` routes beyond self-consistency and the OCR cross-check; the saved file is not read back.
- **Raw images have no embedded picture objects:** a logo in a photographed page is *read* by the model (and became the text "FreeTestData") rather than preserved.
- **A headerless table can be given a bold first row** (the model treats row 1 as a header).
- **Alignment on scans/images** is unreliable (§5.5).
- **Nested tables, third failure shape** (rows crammed unevenly into one block) is not handled.

### 10.3 Decisions the owner has not yet made

1. **PDF → TXT** — built, with defaults the owner did not choose: tables as space-aligned columns, page separator `----- Page N -----`, UTF-8. The owner approved the text-layer correction and model reading for scanned pages, and declined a retest at 1,024 image tokens. Still open: whether those three formatting defaults are what they want, and whether to wire `snap_to_text_layer` into `test_pdf.py` as well.
2. **JPG → PDF** — keep using `image_to_pdf.py`, or build a dedicated script with a verification verdict, multi-image merge and an "embed as picture" option? And rerun the test at 1,024 image tokens to see whether the misread letters go away.
3. **OCR advisory?** — make the OCR word check advisory (not failing) in `pdf_to_tiff.py` and `pdf_to_jpg.py`, adding a deterministic comparison for the JPG. Recommended; owner deferred until they test.
4. **512 vs 1,024 image tokens** (or 768).
5. **Alignment for scans/images** — bigger model, OCR-based alignment flagging, or a dedicated layout-detection model. None chosen.
6. **Auto-retry on low OCR coverage**, the **human-review report**, and **skipping the model on digital pages** — all proposed, none built.

---

## 11. Standing rules and conventions (set by the project owner)

- **Evidence over assumption:** measure; say "not verified" when something is not; redo an analysis honestly when it turns out wrong.
- **Do not commit unless asked.** The owner makes their own commits.
- **Never modify or delete existing folders/files.** Add new I/O folders beside them.
- **OCR reports; it never writes into the output.** An automatic OCR-restoration feature was built and then fully reverted after it inserted misread characters (`+`, `©`, `*`) into documents.
- **Deterministic fixes over prompt engineering.** An elaborate coordinate-based prompt for alignment was explicitly rejected.
- **Ask before big or scope-expanding decisions** (changing the default endpoint, adding dependencies, building a workflow-scale feature).
- **Machine rules:** never enable GPU/Vulkan offload; never set the `performance` CPU governor.
- **Small helpers are copied, not imported, across the top-level scripts** (they run argparse at import). Each script is meant to stand alone.
- **Folder naming:** `<route>_input/` and `<route>_output/`, created per route.
- **Scripts follow one shape:** module docstring explaining *why*, a `run()` per input, `argparse` at the bottom, `sys.exit(1 if fail else 0)`.

---

## 12. Repo map

**Web app:** `idox_app.py` (server) and `idox_app.html` (page), see §7.8.

**Core modules (importable):** `blocks.py` (schema, prompts, model call, checks, repairs, about 1,700 lines), `to_docx.py`, `to_xlsx.py`, `to_pptx.py`, `ocr.py`.

**Conversion scripts (CLI, not importable):** `test_pdf.py` (PDF → Word/Excel/PPT/MD, 1,269 lines), `image_to_word.py`, `image_to_pdf.py`, `image_to_excel.py`, `word_to_pdf.py`, `pdf_to_jpg.py`, `pdf_to_tiff.py`, `tiff_to_pdf.py`, `pdf_to_txt.py`.

**Dev tools:** `bench.py`, `show_docx.py`, `test.py`, `test_json.py`.

**Key functions in `blocks.py`:** `extract_page` (~L1025), `_extract_page_raw_server` (~L1134), `SYSTEM_PROMPT` (~L295), `drop_duplicate_blocks` (~L407), `fix_trailing_heading_after_table` (~L516), `merge_nested_tables` (~L554), `check_structure` (~L639), `check_coverage` (~L731), `check_grounding` (~L921), `DEFAULT_BASE_URL` / `LOCAL_BASE_URL` (~L1016 / L1022).

**Folders**

| Folder | Purpose |
|---|---|
| `pdf_to_word_input/`, `pdf_to_word_output/` | `test_pdf.py` (also used for Excel testing) |
| `pdf_to_ppt_input/`, `pdf_to_ppt_output/` | `test_pdf.py --pptx` |
| `pdf_input_xlsx/`, `xlsx_output/` | early PDF→Excel tests |
| `scanned_pdf_input/`, `scanned_pdf_output/` | scanned-PDF tests |
| `image_to_word_input/`, `image_to_word_output/` | `image_to_word.py` |
| `image_input/`, `pdf_out_img/` | `image_to_pdf.py` (original folders) |
| `image_to_excel_input/`, `image_to_excel_output/` | `image_to_excel.py` |
| `word_to_pdf_input/`, `pdf_out/` | `word_to_pdf.py` (default `--outdir pdf_out`) |
| `pdf_to_txt_input/`, `pdf_to_txt_output/` | `pdf_to_txt.py` |
| `pdf_to_jpg_input/`, `pdf_to_jpg_output/` | `pdf_to_jpg.py` |
| `pdf_to_tiff_input/`, `pdf_to_tiff_output/` | `pdf_to_tiff.py` |
| `tiff_to_pdf_input/`, `tiff_to_pdf_output/` | `tiff_to_pdf.py` |
| `jpg_to_pdf_input/`, `jpg_to_pdf_output/` | reserved for JPG→PDF (empty) |
| `models_manual/` | model weights (git-ignored) |
| `render/` | scratch, overwritten by `test_pdf.py` each run (git-ignored) |
| `test_output/` | disposable outputs from the token-reduction experiments |
| `.kilo/worktrees/` | two older snapshots left by another tool; **not used** |
| `.venv/` | Python 3.13 environment (git-ignored) |

Input/output folders contain many committed sample and result files from testing. They are examples, not part of the product.

---

## 13. Other documents in the repo, and which to trust

| Document | Status |
|---|---|
| **`handover_chat_integration.md`** (this file) | Written for you. Current as of 2026-09-30. |
| **`memory.md`** | The comprehensive technical record. **§1–§13 were written up to 2026-09-25 and are partly outdated; later sections win.** §14 (third wave: 8B endpoint, PowerPoint, nested tables), §15 (this machine), §16 (suspected bugs), §17–§20 (JPG, TIFF, TIFF→PDF work, including the speed-up) are current. Contains root causes for every bug. |
| **`status2.md`** | A short "right now" snapshot. Current. |
| `handover.md`, `status.md` | **Historical** (2026-09-21). Superseded; they cite line numbers that have moved. |
| `eod.md` | **Unrelated to this project's code** — an end-of-day report about a different project (an IDP proof of concept and a mobile app). Ignore it. |
| `requirements.txt` | Pinned Python dependencies. |

The project owner also keeps a **conversation history document outside the repo** (`idox-conversation-history.md`) that narrates how decisions were reached: what was tried, rejected and why. Ask them for it if you need the reasoning behind a decision.

---

## 14. Appendix: command reference

Run from the repo root. `MODEL` = `--base-url http://127.0.0.1:8090` (required while the remote 8B is down).

```bash
# PDF -> Word / Excel / PowerPoint (+ Markdown)
.venv/bin/python test_pdf.py in.pdf MODEL --docx out.docx --xlsx out.xlsx --pptx out.pptx --out out.md
#   --pages 1-3   --dpi 125   --scan-mode text|image|both   --no-ocr   --show-reasoning

# Image -> Word / PDF / Excel
.venv/bin/python image_to_word.py  img.png MODEL --mode text|image|both --outdir DIR
.venv/bin/python image_to_pdf.py   img.jpg MODEL --outdir DIR
.venv/bin/python image_to_excel.py img.png MODEL --outdir DIR

# Word -> PDF (no model)
.venv/bin/python word_to_pdf.py in.docx --outdir DIR          # or --out exact/path.pdf

# PDF -> JPG or JPEG (no model)   default 150 dpi, quality 90, one subfolder per PDF
# (the same format either way; --ext jpeg names the files .jpeg instead of .jpg)
.venv/bin/python pdf_to_jpg.py in.pdf --outdir DIR --pages 1-3 --dpi 200 --quality 95 --no-ocr --ext jpeg

# PDF -> TIFF (no model)  default 300 dpi, LZW, one multi-page file per PDF
.venv/bin/python pdf_to_tiff.py in.pdf --outdir DIR --pages 1-3 --dpi 200 --compression lzw|deflate --no-ocr

# TIFF -> PDF (no model)  searchable PDF, needs tesseract
.venv/bin/python tiff_to_pdf.py in.tiff --outdir DIR --workers 4 --no-text

# PDF -> TXT (model on digital and scanned pages; scanned pages reported unverified)
.venv/bin/python pdf_to_txt.py in.pdf MODEL --outdir DIR --pages 1-3 --dpi 125

# Server
curl -s http://127.0.0.1:8090/health
kill $(pgrep -x llama-server)               # stop (do NOT use pkill -f)
```

**How to read a run's result:**

- `VERDICT: PASS -- every page converted and verified` / `VERDICT: FAIL -- see the problems above` — `pdf_to_jpg`, `pdf_to_tiff`, `tiff_to_pdf`, `pdf_to_txt` (exit code 1 on FAIL). `pdf_to_txt` marks scanned pages `UNVERIFIED` in the report; its `PASS` line says how many.
- `verdict PASS|FAIL` — `word_to_pdf.py`.
- `-> path` / `EXTRACTION FAILED: …` — `image_to_*`.
- `Tier 1 FAILED to produce valid output: …` — a skipped page in `test_pdf.py`.
- `1. self-consistency: PASS|FAIL`, `2. text-layer grounding`, `3. geometry`, `4. content coverage` — the four checks in `test_pdf.py`; the `image_to_*` scripts print 1, 2 and 4 (OCR-based).
- The `NOTE: still not ground truth` line on image routes means exactly that.

**Glossary:** *prefill* — the model reading the image and prompt; *generation* — the model writing the JSON; *image tokens* — how many tokens one image costs (32×32 px = 1 token); *text layer* — the real characters stored inside a digital PDF; *ground truth* — a source the output can be checked against; *frame* — one page of a TIFF.
