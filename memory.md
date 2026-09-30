# idox — Memory (full project reference)

This is the comprehensive reference document for the idox project. Read this first if
you're new to the project — it covers the goal, architecture, every script, the full
history of bugs found and fixed, and the currently open problem. For a short "what's true
right now" snapshot, see `status2.md` instead.

An earlier, now-partially-superseded version of this document exists as `handover.md` /
`status.md` (written mid-session, before several of the fixes and the new image_to_word.py
script existed). This document supersedes them — treat `handover.md`/`status.md` as
historical, not current.

**For the developer building the chat/service integration, start with `handover_chat_integration.md`** (written 2026-09-30): features, models, latency and what a caller must know. This file remains the deep technical record.

**Updated 2026-09-29.** §1-§13 were written up to 2026-09-25 and are kept as the historical
record; where a later section contradicts an earlier one, **the later section wins**. The
biggest changes since: the default endpoint (§3 is superseded by §14.1), new scripts and
fixes (§14), and the project's move to a new machine (§15). §16 lists suspected bugs found
by reading the code, not yet verified by running it.

---

## 1. Goal

A **local, fully offline** pipeline that converts documents into Office formats, using a
locally-run vision-language model (Qwen3-VL) for extraction wherever a model is genuinely
needed, and deterministic code for everything else. Four conversion directions exist:

1. **PDF → Word / Excel** (`test_pdf.py`) — handles both digital PDFs (real text layer)
   and scanned PDFs (no text layer, pixels only).
2. **Raw image → Word** (`image_to_word.py`) — new this session.
3. **Raw image → PDF** (`image_to_pdf.py`) — via Word + LibreOffice.
4. **Word → PDF** (`word_to_pdf.py`) — pure LibreOffice, no model, no vision involved at all.
5. **PDF → PowerPoint** (`test_pdf.py --pptx`, writer `to_pptx.py`) — added after this
   section was first written, see §14.
6. **Raw image → Excel** (`image_to_excel.py`) — added after this section was first
   written, see §14.

7. **PDF → JPG** (`pdf_to_jpg.py`) — added 2026-09-29, see §17.
8. **PDF → TIFF** (`pdf_to_tiff.py`) — added 2026-09-30, see §19.
9. **TIFF → PDF** (`tiff_to_pdf.py`) — added 2026-09-30, see §20.
10. **PDF → TXT** (`pdf_to_txt.py`) — added 2026-09-30, see §21 (digital pages only).

Not built yet: CSV output (any source), image → PowerPoint, image → XML.

A major, ongoing secondary goal: reduce latency and improve reliability, with every claim
backed by direct measurement (server logs, tokenizer counts, `/sys` counters, before/after
comparisons) rather than assumption. The project owner has repeatedly and explicitly
rejected unverified claims.

---

## 2. Architecture (tiered, unchanged in spirit, refined in practice)

```
input -> Tier 0 (deterministic) -> Tier 1/2 (model, where actually needed) -> Tier 3 (deterministic)
```

- **Tier 0 — deterministic parse.** PyMuPDF reads a PDF's real text layer when present
  (`get_text()`), renders pages to PNG for the model (`get_pixmap(dpi=...)`, default 125),
  and pulls embedded raster images out of a PDF directly (`extract_images()` — byte-for-byte
  copy, zero model involvement). `is_scanned = not source_text.strip()` is computed per page
  before the model is ever called.

- **Tier 1/2 — the model.** Qwen3-VL (2B by default, 4B available) reads a page image and
  emits JSON matching the `Page` schema — a list of `TextBlock`/`TableBlock` objects, via
  JSON-schema-constrained decoding. Two schema variants exist now (see §5): the full one
  (asks for `align`/`size`/`bold` too) and a trimmed one (content only).

- **Tier 3 — deterministic file writing only.** `to_docx.py` / `to_xlsx.py` turn `Page`
  objects into real Office files. **The model never writes Office bytes.** This boundary is
  intentional and load-bearing — keep it.

- **Post-extraction correction (digital PDFs only).** `measure_block_look()`
  (`test_pdf.py`) overwrites the model's `align`/`size`/`bold` guesses using real
  measurements from the text layer (font size, x-position, font name), and — new this
  session — also corrects `kind` in one specific case (see §5). This is the ONE case in the
  whole project where the model's guess about *appearance* is ever actually verifiable and
  correctable. It does not run for scanned pages or raw images, because there is no text
  layer to measure from there.

- **Validation — four checks, structural conformance, not business-schema matching.**
  1. `check_structure` — self-consistency (model vs. its own claims). Weak: catches
     contradiction, never catches something the model never mentioned at all.
  2. `check_grounding` — is every string the model wrote actually present in real ground
     truth (a PDF's text layer) or, new this session, in an independent OCR reading?
  3. `check_geometry` — advisory only; compares the model's table shape against PyMuPDF's
     own (unreliable) table detector.
  4. `check_coverage` — is everything in the ground truth (or OCR reading) present in the
     model's output? The one that catches silently dropped content.
  Checks 2 and 4 need *something* to compare against. A digital PDF has its own text layer
  (real ground truth). A scanned PDF or raw image has none — OCR (Tesseract) now fills that
  gap for BOTH cases as an independent second reading (new this session for raw images).
  **OCR is never ground truth** — it's a reading of pixels, same as the model, just a
  different method with different, hopefully uncorrelated mistakes. Never used to
  auto-repair, only to report disagreement.

---

## 3. Stack

- **Python**, project venv at `.venv/`.
- **PyMuPDF** (`pymupdf`/`fitz`) — PDF parsing, page rendering, text-layer access, raster
  image extraction, table-geometry detection (advisory only).
- **pydantic** — the `Page`/`TextBlock`/`TableBlock`/`PageNoLook`/`TextBlockNoLook` schema,
  JSON-schema-constrained decoding.
- **python-docx**, **openpyxl** — deterministic Office writers (Tier 3).
- **LibreOffice** (`soffice --headless`) — Word↔PDF conversion.
- **Pillow (PIL)** — reading raw image pixel dimensions (`image_to_word.py`'s `--mode
  image` path, no model needed for this).
- **pytesseract / Tesseract** (`ocr.py`) — independent second reading, used for: (a)
  scanned PDF pages (original use), (b) raw images through `image_to_word.py` and
  `image_to_pdf.py` (added this session).
> **Superseded (2026-09-28/29):** the default endpoint, server binary path, launch command
> and hardware below are out of date. `DEFAULT_BASE_URL` is now the remote 8B endpoint, and
> the project has moved machines. See §14.1 and §15 for the current setup.

- **Model serving — two sources, ONE is now the default everywhere:**
  1. **Raw `llama-server`** (`/usr/local/lib/ollama/llama-server`, invoked directly,
     bypassing Ollama's daemon/Modelfile system), serving manually-downloaded GGUF files
     from Qwen's official HF repo (`Qwen/Qwen3-VL-2B-Instruct-GGUF`) in `models_manual/`:
     `Qwen3VL-2B-Instruct-Q4_K_M.gguf` (LLM) + `mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf`
     (vision encoder). Reached via `http://127.0.0.1:8090`. **This is now the default
     everywhere** — `blocks.DEFAULT_BASE_URL`, and every script's `--base-url` flag
     defaults to it. See §6 for why this default was made explicit (a real incident).
  2. **Ollama's own bundled models** (`qwen3-vl:2b-instruct`, `qwen3-vl:4b-instruct`) —
     still available, still useful (the 4B model isn't in `models_manual/`, so reaching it
     means going through Ollama), but no longer the silent default. Opt in by passing an
     empty string for `--base-url`.
  Server launch command (CPU-only — see §7 for why no `-ngl`):
  ```bash
  nohup /usr/local/lib/ollama/llama-server \
    --model /home/aiteam/idox/models_manual/Qwen3VL-2B-Instruct-Q4_K_M.gguf \
    --mmproj /home/aiteam/idox/models_manual/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
    --port 8090 --host 127.0.0.1 \
    --no-webui --ctx-size 8192 --image-min-tokens 1024 \
    > /tmp/llama_manual_perf.log 2>&1 &
  disown
  ```
- **Hardware**: i7-1185G7 laptop, 4 cores/8 threads, 28W-class, LPDDR4x RAM. CPU-only.
  Governor: `powersave` (confirmed correct — see §7).

---

## 4. Every file, what it's for

| File | Role |
|---|---|
| `blocks.py` | `Page`/`TextBlock`/`TableBlock`/`PageNoLook`/`TextBlockNoLook` schema, `SYSTEM_PROMPT`/`USER_PROMPT`, `DEFAULT_BASE_URL`, `extract_page()` (routes to Ollama or raw server, applies `repeat_penalty`), `_extract_page_raw_server()`, `_add_look_placeholders()`, `drop_duplicate_blocks()`, the 4 `check_*` functions. |
| `test_pdf.py` | PDF → Word/Excel. `is_scanned` detection, `measure_block_look()` (appearance + kind correction), `check_geometry()`, `repair_missing_lines()`/`repair_table_rows()` (deterministic, from the PDF's own text layer, never a second model call), `--scan-mode` gate (text/image/both), the latency-breakdown summary. |
| `image_to_word.py` | **New this session.** Raw image → `.docx`. `--mode text` (model extraction, default) / `image` (no model, instant, embeds the picture as-is) / `both` (writes two separate files, `name.docx` + `name_scan.docx`). OCR-backed grounding/coverage checks (new). |
| `image_to_pdf.py` | Raw image → PDF, via `extract_page()` → `build_docx()` → LibreOffice. Updated this session: OCR-backed checks, `drop_duplicate_blocks`, `DEFAULT_BASE_URL`. Contains copied (not imported) `find_soffice()`/`docx_to_pdf()` — `word_to_pdf.py` runs its own `argparse` at import time, so importing it would hijack this script's CLI args; the project convention (stated in `word_to_pdf.py`'s own docstring) is to copy small reusable pieces instead. |
| `word_to_pdf.py` | Standalone LibreOffice-based Word→PDF converter. No model. Do not import it. |
| `to_docx.py` | Deterministic Word writer (Tier 3). Fixed a real crash bug this session (§6). |
| `to_xlsx.py` | Deterministic Excel writer (Tier 3). Had the identical crash bug, fixed alongside `to_docx.py`. |
| `pdf_to_txt.py` | PDF → plain text, added 2026-09-30 — see §21. Model on pages that have a text layer; scanned pages not yet converted. |
| `tiff_to_pdf.py` | (Multi-page) TIFF → searchable PDF, added 2026-09-30 — see §20. No model: lossless picture + Tesseract text layer. |
| `pdf_to_tiff.py` | PDF → one multi-page TIFF, added 2026-09-30 — see §19. No model; lossless, verified pixel-exact. |
| `pdf_to_jpg.py` | PDF → JPG, added 2026-09-29 — see §17. No model: PyMuPDF draws each page. One subfolder per PDF, one JPG per page. |
| `to_pptx.py` | Deterministic PowerPoint writer (Tier 3), added later — see §14.4. One PDF page = one slide, first heading = slide title. |
| `image_to_excel.py` | Raw image → `.xlsx`, added later — see §14.4. No `--mode` flag by design. Refuses to write a file when no table is detected. |
| `ocr.py` | Tesseract wrapper. `ocr_page(pdf_page)` for a scanned PDF page, `ocr_image(path)` for a raw image file (used by the two raw-image scripts now). Role is verifier only, never extractor, never used to auto-repair. |
| `bench.py`, `show_docx.py`, `test_json.py`, `test.py` | Small pre-existing dev/utility scripts (latency benchmarking, printing a `.docx`'s real structure to the terminal, an early single-image smoke test). Not part of the main pipeline; not touched this session. |
| `models_manual/` | Manually downloaded HF GGUF files (see §3). |

### Folders (all created this session except the first two)

| Folder | Purpose |
|---|---|
| `pdf_to_word_input/` / `pdf_to_word_output/` | `test_pdf.py` I/O. |
| `word_to_pdf_input/` | `.docx` files for `word_to_pdf.py`. |
| `scanned_pdf_input/` / `scanned_pdf_output/` | Scanned-PDF-specific `test_pdf.py` testing. |
| `image_to_word_input/` / `image_to_word_output/` | `image_to_word.py` I/O. |
| `image_input/` / `pdf_out_img/` | `image_to_pdf.py` I/O (pre-existing). |
| `pdf_to_ppt_input/` / `pdf_to_ppt_output/` | `test_pdf.py --pptx` I/O (added later). |
| `image_to_excel_input/` / `image_to_excel_output/` | `image_to_excel.py` I/O (added later). |
| `pdf_to_txt_input/` / `pdf_to_txt_output/` | `pdf_to_txt.py` I/O (added 2026-09-30). |
| `tiff_to_pdf_input/` / `tiff_to_pdf_output/` | `tiff_to_pdf.py` I/O (added 2026-09-30). |
| `pdf_to_tiff_input/` / `pdf_to_tiff_output/` | `pdf_to_tiff.py` I/O (added 2026-09-30). |
| `jpg_to_pdf_input/` / `jpg_to_pdf_output/` | For JPG → PDF via `image_to_pdf.py --outdir` (§18). |
| `pdf_to_jpg_input/` / `pdf_to_jpg_output/` | `pdf_to_jpg.py` I/O (added 2026-09-29). |
| `pdf_input_xlsx/` / `xlsx_output/` | Early PDF → Excel testing. |
| `test_output/` | Disposable outputs from the 256/512 image-token experiments (§14.3). Safe to delete when no longer needed. |
| `render/` | Scratch space: `test_pdf.py` overwrites it on every run (page PNGs, extracted images). |
| `.kilo/worktrees/` | Two identical, older snapshots of the project, left by another tool (Kilo). Not used by the pipeline. |
| `models_manual/` | See §3. |

---

## 5. Bugs found and fixed this session (in the order discovered, all verified, not assumed)

### 5.1 Duplicate blocks — model re-emitting already-written content
**Symptom:** a real digital-PDF page came back with the same sentence 2-3 times, verbatim,
though the source had it once.
**Root cause, isolated by calling the raw model directly (bypassing all repair/checks):**
the model wrote the page's real content correctly, then kept going and re-emitted already
written blocks. Request had `repeat_penalty` at its default of 1.0 (off) and `temperature: 0`
(pure greedy) — nothing discouraged re-emitting recent tokens. `blocks: List[...]` must stay
unbounded (page content varies), so unlike an earlier, similar bug with `block_kinds`
(fixed by removing that array entirely), this one couldn't be fixed by changing the schema.
**Fix:**
1. Root cause: added `repeat_penalty: 1.15` to both the Ollama and raw-server request paths
   in `blocks.py`. Verified directly — re-ran the identical page, same server: 4 real blocks,
   zero repeats.
2. Deterministic safety net (per the project owner's explicit "must not happen again"):
   `drop_duplicate_blocks()` in `blocks.py` — removes any `TextBlock` whose normalized text
   (over 12 characters, to avoid false positives on short legitimate repeats like "Total")
   exactly matches an earlier block on the same page. Wired into `test_pdf.py`,
   `image_to_pdf.py`, `image_to_word.py`, right after every extraction call.
   Threshold note: originally set at >20 characters, immediately found to be too high —
   "Monthly Active Users" (exactly 20 chars) slipped through as a real duplicate
   (classified once as a heading, once as a paragraph) on a different real document.
   Lowered to >12.

### 5.2 False "center" alignment on ordinary full-width paragraphs (digital PDFs)
**Symptom:** normal body paragraphs came out centered in the `.docx`.
**Root cause, confirmed with real bbox numbers:** `measure_block_look()`'s center-detection
only checked whether a line's midpoint was near the page's midpoint. An ordinary paragraph
line that happens to nearly fill the column (common with symmetric left/right margins) has
its midpoint land near the page's true center by pure coincidence — nothing to do with real
alignment. Real example: a line with `left_gap=78, right_gap=78` (page width 612pt) is
trivially "centered" by this test even though it's ordinary ragged-right body text.
**Fix:** added `col_width` (the page's widest line, a proxy for the real text-column width)
and require a line's own width to be meaningfully narrower than that (`< col_width * 0.85`)
before allowing a "center" classification. A genuinely centered line (a title) is narrower
than the column with space either side; an ordinary paragraph line fills most of it.
Verified on `sample_digital_document.pdf`: false centers corrected to `LEFT`, the real title
still correctly `CENTER`.

### 5.3 Schema/token waste — asking for appearance fields that get discarded anyway
**Not a bug, a real inefficiency:** on digital pages, the model was asked to guess
`align`/`size`/`bold` for every block, then `measure_block_look()` throws every one of
those guesses away and re-measures from the real text layer. 100% wasted generation work
on digital pages.
**Fix:** added `TextBlockNoLook`/`PageNoLook` (same schema, minus those 3 fields) and an
`include_look: bool` parameter on `extract_page()`. `test_pdf.py` calls it with
`include_look=is_scanned` — scanned pages still get the full schema (only source of that
info there); digital pages get the trimmed one. Measured real per-block token cost of the 3
fields via the server's own `/tokenize` endpoint: 17 tokens/block. Projected saving on the
canonical benchmark: ~22.6s (154.10s → ~131.2s) — this was a projection from real per-token
measurements, not yet re-confirmed with a full fresh benchmark run after everything else
that's stacked on top since (repeat_penalty, dedup, kind-correction all add or remove some
generation work too).

### 5.4 Caption/heading misclassification (digital PDFs)
**Symptom:** an italic closing line (`"Generated by Claude for demo/testing use."`) came
out as plain, non-italic, body-sized text in the `.docx`, though visibly italic and smaller
in the source.
**Root cause:** `kind` (heading/paragraph/caption/list/image) is a pure model guess, with
zero deterministic correction — unlike `align`/`size`/`bold`, `measure_block_look()` never
touched `kind` at all. Italics are the ONLY thing that makes a caption look different in
`to_docx.py`, and italics are applied ONLY when `kind == "caption"` — so a caption
misclassified as `"paragraph"` silently loses all its distinguishing style.
**Fix:** `measure_block_look()` now also detects italic fonts from the real text layer (same
technique already used for bold: checking the font name string for `"italic"`/`"oblique"`),
and reclassifies a block the model called generic `"paragraph"` into `"heading"` (measured
size ≥ body×1.25) or `"caption"` (italic, or measured size ≤ body×0.88) — reusing the exact
thresholds already trusted elsewhere in this file (`repair_missing_lines()`). Deliberately
one-directional: never overrides a heading/list/caption/table the model already got right,
only upgrades the generic fallback. Verified on `demo1.pdf`: the caption now comes out
italic, 9pt, correctly styled.
**A real crash happened while building this fix**, caught immediately: added a 5th element
(`italic`) to an internal tuple and missed updating one other place that unpacked it
(`col_width = max((bbox[2]-bbox[0] for _, _, bbox, _ in lines), ...)` — expected 4, got 5).
Fixed by updating that unpacking to match; re-verified with a clean run before considering
the fix done.

### 5.5 Silent fallback to a different, less reliable model
**Symptom, a real user-facing incident:** a run of `image_to_word.py` on a dense document
image (`demo2.png`) dropped a whole table and merged two headings into body paragraphs.
**Root cause, confirmed via the raw server's own log (zero matching entries for that
timestamp):** the command that produced it omitted `--base-url`, which silently fell back
to Ollama's own bundled `qwen3-vl:2b-instruct` — a different build from the manually
downloaded HF model this whole session's testing has been based on. Reproduced the
unreliability directly: re-ran the same image through Ollama's model and got real quality
problems again (headings merged into paragraphs, a stray empty hallucinated block); re-ran
through the raw server and got a clean, complete result.
**Fix:** `DEFAULT_BASE_URL = "http://127.0.0.1:8090"` added to `blocks.py`, used as the
default for `extract_page()`'s `base_url` parameter AND every script's `--base-url` argparse
default (`test_pdf.py`, `image_to_pdf.py`, `image_to_word.py`). Omitting the flag now safely
uses the intended model instead of silently switching to a different one. Opt into Ollama on
purpose by passing an empty string (e.g. to reach the 4B model, which isn't in
`models_manual/`).

### 5.6 Real crash: comparing dicts in `sorted()` (image placement)
**Symptom:** `TypeError: '<' not supported between instances of 'dict' and 'dict'`,
crashing `--scan-mode both` on a real document.
**Root cause:** `build_docx()`/`build_xlsx()` sort `(position, image_dict)` tuples to keep
several images on a page in order. When a page has **zero text blocks** (exactly the blank
"scan" page written under `--scan-mode both`) **and 2+ images**, every image's computed
position collapses to `0` — every tuple ties on its first element, so Python falls back to
comparing the second element (the image dict) to break the tie, and dicts have no ordering.
Reproduced directly with a real 2-image scanned page.
**Fix:** added `key=lambda t: t[0]` to both `sorted()` calls (`to_docx.py` AND `to_xlsx.py`
— identical latent bug in both, `to_xlsx.py`'s just hadn't been hit yet), restricting
comparison to the position only; Python's sort is stable, so tied images keep their original
order. Verified by re-running the exact crashing command — completes cleanly now, both
output files written.

---

## 6. New capability: OCR-backed verification for raw images

Before this session, `image_to_word.py`/`image_to_pdf.py` had **no way at all** to detect
dropped or hallucinated content — only `check_structure` (self-consistency) ran, and that
only catches the model contradicting itself, never content it silently never mentioned.

**Fix:** both scripts now run `ocr.ocr_image(path)` (Tesseract, already existed in `ocr.py`
for a different case — reading images embedded *inside* a digital PDF page) and run
`check_grounding`/`check_coverage` against that OCR text instead of reporting N/A. Same
rule as scanned PDFs: OCR is never ground truth, never used to auto-repair, only to report
disagreement — because either the model or OCR could be the one that's wrong, and nothing
in this system can tell which.

**A real, honest limit surfaced immediately and needs to stay understood:** if both the
model AND OCR fail at the same spot (plausible on a genuinely hard image — blurry, small
font, unusual glyphs — since both are reading the same pixels), nothing catches it. This
isn't a gap to code around; a raw image has no ground truth anywhere in this system, unlike
a digital PDF's real text layer. OCR raises confidence, it never proves correctness.

---

## 7. Hard constraints on THIS machine — do not violate

- **Never re-enable iGPU offload** (`-ngl`, `GGML_BACKEND_PATH`, any Vulkan flag on
  `llama-server`). Caused a genuine full-system freeze requiring a forced reboot, for a
  benefit already known to be limited (helps prefill only, not generation, since the iGPU
  shares the CPU's own memory bus).
- **Never set the CPU governor to `performance`.** Measured, not assumed: it produced a
  SLOWER run (192.94s vs. 154.10s) than `powersave`, because this thin-chassis chip cannot
  sustain the clocks `performance` demands and thermally throttles — confirmed via
  `/sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count` showing 4504 throttle
  events during the run. `powersave` is already the correct, confirmed-best setting.
- **Generation cost is memory-bandwidth-bound, not compute-bound, not disk-bound.** The
  model is already resident in RAM after server startup — "warm mode" already exists.
  Every output token still requires streaming the full weight set fresh from RAM (no weight
  reuse across sequential single-token steps) — inherent to autoregressive batch-size-1
  decoding, not a configuration problem. CPU clock/thread tuning does not fix this; only
  prefill (compute-bound) benefits from raw compute.

---

## 8. The current, open, unresolved problem

**Alignment (and, closely related, block-`kind`) is fundamentally unreliable for any page
with no text layer — scanned PDFs AND raw images alike — and there is no deterministic fix
available in the current architecture.**

- `measure_block_look()` — the only correction mechanism that exists — is explicitly gated
  by `if not is_scanned:` in `test_pdf.py`. For a scanned page, or for anything going
  through `image_to_word.py`/`image_to_pdf.py`, it never runs. Both paths call the exact
  same `extract_page()`, with the exact same schema, so **there is no separate, better
  mechanism to port from one to the other** — this was directly checked and confirmed when
  the project owner suggested reusing "whatever the scanned-PDF path does" for raw images:
  the scanned-PDF path has the identical bug (`"Scanned Document - Demo"` guessed as
  `CENTER` when it's genuinely left-aligned in the source), it just hadn't been noticed yet
  on that particular (simpler) test document.
- **Confirmed, reproducible pattern:** the model has a learned bias toward guessing
  `"center"` for short, bold, heading-like text, even when the source is clearly
  left-aligned. Observed independently on two different documents (`demo2.png`,
  `demo_scanned_with_image.pdf`), via two different scripts, same underlying code path.
- **A detailed alternative was proposed and explicitly NOT adopted**: rewriting the system
  prompt with an elaborate XY-coordinate/bounding-box spatial-reasoning framing (origin at
  top-left, explicit axis directions, "preserve relative X/Y position" rules, etc.).
  Reasoning against it, given directly to the project owner:
  1. It's still "a better sentence," and this exact codebase's own documented history says
     that doesn't reliably work — constrained decoding enforces *shape*, never *semantics*,
     and the current prompt already has an explicit align rule that still fails.
  2. The specific failure looks like a learned statistical prior (real documents often do
     center titles), which better wording nudges probabilistically but doesn't reliably fix.
  3. Even if the model emitted coordinates, the schema has no numeric bbox field today —
     adopting this needs a real schema change plus new code to translate pixel coordinates
     into Word's alignment/indent model, and there would be no way to verify the emitted
     coordinates are accurate anyway (same "no ground truth" ceiling, just relocated).
- **Real options on the table, none yet decided or built:**
  1. Use the 4B model instead of 2B for raw images — already measured to help *partially*
     (recovered 3→6 blocks on `demo_image.png`), not a full fix.
  2. Extend the OCR check to flag alignment disagreement too, not just missing/hallucinated
     text — would make bad alignment *visible* automatically, would NOT fix it.
  3. A dedicated, separate document-layout-detection model (not the VLM) — the real
     structural fix for alignment specifically, analogous to how Tesseract is a dedicated
     tool for text-reading rather than asking the VLM to double as an OCR engine. Real new
     dependency and integration work; not scoped yet.
- **Project owner's stated bar:** "the output must be clean, aligned, and fully extracted"
  for any uploaded document. Told directly and honestly: not achievable as a 100% guarantee
  with the current architecture (no ground truth for raw images/scanned pages exists to
  check against), only as a target you can get closer to. Awaiting a decision on which of
  the three real options (or which combination) to pursue.

---

## 9. Git state

> **Superseded (2026-09-29):** everything is now committed. HEAD is `b240895` ("changes
> made") on `feature/testing_2b_Q8mmproj`, and the working tree was clean when checked. The
> remote is `https://github.com/sumukh-naidu/idox.git`. The text below is historical.

Branch `feature/testing_2b_Q8mmproj`. Commits since §5-§8 were written (all made by the
project owner directly, not by an assistant in this conversation — no commits should be
made without being explicitly asked): `a7266bb` "Add memory notes", `b8ea79e` "Added
requirement", `174329c` "some changes made" (bundled the §10 fixes below: the bullet-fold
fix, the table-spacing fix, and their supporting edits to `blocks.py`/`test_pdf.py`/
`to_docx.py`). **Currently uncommitted:** `blocks.py`, containing the §11 resolution fix
(`MIN_IMAGE_PIXELS`/`_ensure_min_resolution()`) — not yet committed as of this writing.

---

## 10. More bugs found and fixed (second wave, after §5-§8 were written)

### 10.1 "Duplicate" content that was actually our OWN repair logic causing it
**Symptom:** `sample.pdf`'s "Key points" bulleted list came out twice — once as a proper
list, once again as 3 separate standalone paragraphs.
**First hypothesis (wrong, corrected honestly mid-investigation):** assumed this was the
same model-repetition bug as §5.1. Direct testing disproved it — `drop_duplicate_blocks()`
did not catch it, and repeated raw extraction calls came back clean, 7 correct blocks, no
repeats at all.
**Real root cause, found by reading the full console output, not just the summary:** the
model's extraction was correct the whole time. The PDF's text layer stores each bullet
with a `•` character; the model's own list rendering always uses `-`. `check_coverage()`'s
line-match compares text after `normalize()`, which folds quote/dash typographic variants
but never touched bullet characters — so `"• Generated..."` and `"- Generated..."` looked
like unrelated strings, got reported as "missing entirely," and `repair_missing_lines()`
dutifully re-inserted them — creating a duplicate of content that was never actually
missing. **The repair step was the bug, not the model.**
**Fix:** extended `normalize()`'s existing `_PUNCT` translation table (the same mechanism
already folding curly-vs-straight quotes) to also fold common bullet characters (`•`, `◦`,
`▪`, `▫`, `‣`, `·`) into `-`. Foundational fix — `normalize()` is used everywhere
(grounding, coverage, dedup, repair), so this closes the gap wherever a bullet-character
mismatch could cause a false "missing" or false "duplicate" report, not just this document.
**Secondary cosmetic fix alongside it:** `render_block()` was producing `"- - text"`
(double dash) when the model's own list text already included a marker. Fixed by stripping
any existing leading bullet from each line before adding the canonical one.
**Also extended `drop_duplicate_blocks()` itself** (independent of the above, a real but
different failure shape confirmed separately): a "list" block followed by its own items
regenerated individually as separate blocks. Added per-line tracking (`seen_list_items`,
bullet-marker-stripped) so a later standalone block matching an already-seen list item gets
dropped too, not just whole-block exact repeats.

### 10.2 Tables sitting flush against surrounding text (no spacing)
**Symptom:** no visible gap between a table and the paragraph before/after it, on every
page of every document, not just one.
**Root cause:** a table (`<w:tbl>`) is a structurally different element from a paragraph in
Word's own format, and does not inherit the "Normal" style's `space_after` (2pt) the way
one paragraph inherits it from the paragraph before it. `_add_table()` never added spacing
explicitly, so tables ended up flush on both sides regardless of document.
**Fix:** `build_docx()`'s main loop now tracks the most recently written paragraph and
explicitly sets `space_after = Pt(6)` on it before writing a table, and `space_before =
Pt(6)` on whatever paragraph comes immediately after. Verified via real EMU values in the
written file (76200 EMU = exactly 6pt on both sides).
**Proposed, NOT built:** measuring the REAL gap from the source PDF's own geometry
(`find_tables()`'s bbox vs. the surrounding text blocks' bboxes) instead of a flat 6pt —
confirmed technically feasible and scoped in conversation (digital PDFs only, needs new
per-table spacing data threaded from `test_pdf.py` into `build_docx()`, which doesn't exist
today), but the project owner did not confirm building it — flat 6pt stands for now.

---

## 11. The raw-image resolution gap (found, measured, and fixed — LATER REMOVED)

> **Removed 2026-09-29.** With the server fixed at 512 image tokens (§15), it resizes every
> image to ~512 tokens whatever size arrives. Measured: `sample2.png` original (965x898) and
> upscaled (1248x1162) both gave 1,141 prompt tokens. So `MIN_IMAGE_PIXELS` and
> `_ensure_min_resolution()` were deleted from `blocks.py`. If the server goes back to
> `--image-min-tokens 1024` or higher, this upscaling matters again and can be restored from
> git history. The text below is kept as the historical record.

**How it was found:** the project owner directly challenged the assumption that dropped
content on raw images (`test.png`) was pure "model randomness," pointing out that scanned
PDFs (also just images, by the time the model sees them) don't show the same problem —
correctly suspecting a real, fixable difference rather than accepting the first
explanation. That challenge was right.

**What was actually different, measured directly:** `test_pdf.py` renders every PDF page
at a deliberately controlled 125dpi. `image_to_word.py`/`image_to_pdf.py` sent a raw image
at whatever resolution it happened to be uploaded at — no control at all. Measured on a
real file: `test.png` at its native 671×862px (578,402px) landed at **1,088 vision
tokens**; an equivalent PDF page at 125dpi (1063×1375px, 1,461,625px) landed at **1,427**.
A real ~31% detail gap. The server's `--image-min-tokens 1024` flag was already providing
*some* protection (stopping a raw image being sent at its full, even-lower native token
count) but that bare floor is still meaningfully below what `test_pdf.py` achieves through
deliberate DPI control.

**Fix:** `MIN_IMAGE_PIXELS = 1_450_000` (matching the 125dpi Letter-page baseline) and
`_ensure_min_resolution()` in `blocks.py` — upscales (PIL, LANCZOS, aspect-ratio
preserved) any image below that floor before sending it to the model; a no-op for anything
already at or above it, including every PDF page `test_pdf.py` itself renders (already
adequate). Wired into `extract_page()` by reassigning `image_path` once, near the top,
before branching to either transport — applies uniformly to both the Ollama and raw-server
paths, and to every caller (`image_to_word.py`, `image_to_pdf.py`, `test_pdf.py`).
Temporary upscaled copies are left in `/tmp` rather than explicitly cleaned up — a
deliberate, low-risk tradeoff against re-plumbing multiple return paths in `extract_page()`
for a cleanup the OS already handles.
**Verified end to end:** re-measured `test.png` after the fix — 1,434 image tokens, now
matching the PDF-equivalent target almost exactly (up from 1,088).
**Honest scope, set BEFORE building and confirmed true AFTER building, by direct repeated
testing:** this measurably helps (fewer legibility-driven misses) but does **not**
eliminate content-dropping — repeated test runs on the same (now properly-resized)
`test.png` still show different content dropped on different calls (the "Title of
Invention" line and `[0003]` have each been captured on some runs and dropped on others,
independently of each other, across several consecutive tests). This confirmed the
dropping has (at least) two separate causes: low resolution (now fixed) and genuine
run-to-run model inconsistency (still open, see §12). This fix also does **nothing** for
alignment — confirmed and stated explicitly before building it — completely different root
cause (see §8), untouched by resolution.

---

## 12. Why temperature=0 still isn't perfectly reproducible (real mechanism, not hand-waving)

Directly asked and answered in conversation, worth keeping precise: "temperature 0" means
the model always picks the highest-probability next token, but the probabilities
themselves come from floating-point matrix multiplication across multiple CPU threads
(this server is CPU-only), and floating-point addition is not strictly order-independent.
Combined with this server's own confirmed KV-cache slot reuse across requests (see the
earlier prompt-caching finding, pre-dating this doc), the exact numerical path can differ
very slightly between two calls on identical input. Almost always this changes nothing —
the top token wins by a wide margin regardless. But for a genuinely close call — "keep
transcribing this block" vs. "move on" — that tiny noise is enough to flip which token
wins, and once the decode path diverges early in the JSON array, everything downstream in
that generation differs too.

This is the mechanism behind the run-to-run content-dropping seen throughout this session
(sample.pdf's list, test.png's Title line / `[0003]`, demo2.png's dropped table). It also
explains WHY some content is far more exposed than other content: a plain, unambiguous
heading (e.g. `"Technical Field"`) has come through consistently across every run tested;
a visually unusual line (`test.png`'s `"Title of Invention : [Sample Application]"`, with
odd bracket-style formatting, sitting in an ambiguous position between a real heading and
numbered body paragraphs) is exactly the kind of near-tie case most exposed to this noise.

**This is also why retry (not a code patch) is the correct lever for what's left of the
content-dropping problem**: a deterministic bug fails the same way every time and a patch
can target it directly; this doesn't — different runs drop different content, sometimes in
opposite directions on the same two pieces of content across consecutive tests — which
means a fresh retry has a real, non-zero chance of landing on the other side of whatever
near-tie caused the previous drop.

---

## 13. Still open / proposed but not yet built

- **Auto-retry on low OCR coverage** — proposed, not yet built or confirmed. If
  `check_coverage()` (OCR-backed, for raw images) scores below some threshold, automatically
  re-run extraction (once, maybe twice) and keep whichever attempt scores highest. Directly
  motivated by §12: different calls drop different content, so retrying has real expected
  value here, unlike retrying a deterministic bug. This is the strongest remaining lever
  specifically for the "content sometimes missing" half of the project owner's stated bar
  ("clean, aligned, fully extracted"); it does nothing for alignment.
- **Real measured table spacing** (§10.2) instead of the flat 6pt — feasible, scoped,
  not built, no confirmation to proceed yet.
- **Alignment** — unchanged from §8, still the single biggest open decision, still none of
  the three options chosen (bigger model / OCR-alignment-flagging / dedicated
  layout-detection model). The resolution fix in §11 does not touch this; confirmed
  explicitly before that fix was built, so it isn't mistaken for progress on alignment.
- **Human review of disagreements only** (proposed after the OCR-restoration revert, §14.5):
  a focused report showing just the disputed lines, with the model's reading, OCR's
  reading and a cropped image of that region side by side. Never auto-applied. Not built.
- **Skipping the model on digital pages.** `is_scanned` is computed but `extract_page()`
  still runs on every page (`test_pdf.py`, the Tier 1 call), even when the text layer
  already has the content. Still the biggest known latency lever for digital PDFs. Not built.

---

## 14. Third wave (2026-09-25 → 2026-09-29)

### 14.1 Default endpoint is now the remote 8B, with the local 2B kept
A colleague proposed a remote `Qwen3-VL-8B-Instruct` (Q4_K_M) endpoint at
`http://10.0.3.33:8080`, on the same llama-server / OpenAI-compatible stack. Verified
directly: it was reachable, `/props` confirmed the model, and one real extraction worked
with zero code changes. At the owner's explicit choice it became the project-wide default:
`blocks.DEFAULT_BASE_URL = "http://10.0.3.33:8080"`. The local 2B setup is kept as
`blocks.LOCAL_BASE_URL = "http://127.0.0.1:8090"`.

This is a real dependency change: every run that omits `--base-url` needs the remote
machine to be up. It has gone down several times. The pattern each time was that the host
answered but the port refused connections, meaning the server process wasn't running, not a
network fault. **Whenever the 8B endpoint is down, pass `--base-url http://127.0.0.1:8090`.**
As of 2026-09-29 the 8B endpoint is down and the owner is using the local 2B.

### 14.2 8B vs 2B, measured
- **Speed:** on the same test page, 8B was faster on both phases despite being the larger
  model. Prefill took 14.07s vs about 68s, and generation ran at about 19.25 vs 12-13 tok/s.
  The remote machine's hardware decided this, not model size.
- **Accuracy at 256 image tokens:** word-for-word diffs showed neither model was uniformly
  better. Each made different character misreads and different table-structure mistakes.

### 14.3 Image-token reduction (256 / 512 tokens): a caching artifact, caught and redone
The first result, "no latency difference", was wrong. The server log showed the fixed
643-token text prefix was being reused from the previous request's cache. The "512-token"
test had therefore evaluated only 526 tokens, not 1,178. Every test was redone with a full
server restart in between to guarantee a cold cache. Latency then scaled roughly in
proportion to token count. **Rule: restart the server (or vary the input) between any
latency comparison.** Outputs are in `test_output/`.

### 14.4 New writers, scripts and fixes
- **`to_pptx.py` + `test_pdf.py --pptx`.** One PDF page is always one slide, and the first
  heading becomes the slide title. Two real layout bugs were found and fixed with measured
  positions:
  - Text height was estimated per block, so paragraphs overlapped tables. It now uses an
    estimated wrapped-line count (`_wrapped_line_count`).
  - Images were capped only against a flat 5in, so one ran 1in past the slide. They are now
    also capped by the space actually left (`_remaining_height`).
  - Body text starts below the real title placeholder's bottom edge, not below a guessed
    1.4in.
- **`image_to_excel.py`.** Deliberately has no `--mode image`: a snapshot is pointless in a
  spreadsheet. It writes nothing, and returns failure, if the model finds no table.
- **`fix_trailing_heading_after_table()`** (`blocks.py`). The model *consistently* (not
  randomly) emitted a table's own heading after the table. The fix is deterministic and only
  fires on one shape: a table, then a single heading, and the heading is the last block.
- **`merge_nested_tables()`** (`blocks.py`). The schema cannot represent a table inside a
  cell, so the model emits each nesting level as its own table. This merges them: a
  **blank last cell in the previous table's last row** triggers the merge, deepest level
  first, repeating until no trigger is left. Column-width comparison was tried and rejected,
  because a doubly-nested table can be as wide as its parent. Verified live on
  `image_to_excel_input/complex.png` (3 levels deep).
- **`drop_duplicate_blocks()` now checks tables too.** It drops an exact repeat of a table,
  and a table whose every substantive row (>12 characters) already appeared in an earlier
  table. Found while testing nested tables: a stray duplicate fragment had tricked the merge
  into pairing the wrong tables.
- **Still open, not fixed:** a third nested-table failure, where rows arrive crammed
  unevenly into one single table block instead of as separate blocks. Neither fix above
  handles it.

### 14.5 OCR-restoration: built, then fully reverted
Automatically inserting lines that OCR found but the model missed was built and tested for
raw images, then reverted at the owner's request. It made outputs worse: OCR's misread
bullets (`+`, `'`, `©`, `*`) went into documents as if verified, and a heading landed in
the middle of garbled restored fragments. `ocr.py`, `image_to_word.py` and
`image_to_pdf.py` went back to their last committed state, and the one added function was
removed from `blocks.py`. **Standing rule: OCR only reports, it never writes into the
output.** The accepted direction is the human-review report in §13.

---

## 15. Machine migration (2026-09-29)

The project moved from `/home/aiteam/idox` to `/home/sumukh/Downloads/idox`, on an Ubuntu
26.04 machine with an Intel Tiger Lake CPU, Iris Xe graphics, 8 threads and 14 GB RAM. Paths
in older sections and in `handover.md` / `status.md` still say `/home/aiteam/...`.

What was set up, all in user space (no sudo):
- **Python 3.13.15**, installed with `uv` (`~/.local/bin/uv`). The old `.venv` was built
  for Python 3.12, which this machine doesn't have, so it couldn't import anything. It was
  moved (not deleted) to `~/.local/share/idox-old-venv-py312`.
- **New `.venv`** (Python 3.13), built from `requirements.txt` with
  `uv pip install --python .venv/bin/python -r requirements.txt`. `uv` venvs have no `pip`
  inside, so use `uv pip` to add packages.
- **llama.cpp `llama-server` b11247, CPU-only build** (`llama-b11247-bin-ubuntu-x64`), at
  `~/.local/opt/llama.cpp/llama-b11247/llama-server`. The Vulkan build was deliberately not
  used (§7). This replaces `/usr/local/lib/ollama/llama-server`, which doesn't exist here.

Starting the local 2B server:
```bash
setsid nohup ~/.local/opt/llama.cpp/llama-b11247/llama-server \
  --model /home/sumukh/Downloads/idox/models_manual/Qwen3VL-2B-Instruct-Q4_K_M.gguf \
  --mmproj /home/sumukh/Downloads/idox/models_manual/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
  --port 8090 --host 127.0.0.1 --no-webui --ctx-size 8192 \
  --image-min-tokens 512 --image-max-tokens 512 \
  > ~/.local/share/idox/llama-server.log 2>&1 < /dev/null & disown
```
Health check: `GET http://127.0.0.1:8090/health` returns `{"status":"ok"}`. To stop it:
`kill $(pgrep -x llama-server)`. Don't use `pkill -f` with the path: it also matches the
shell running the command and kills it.

**Image tokens fixed at 512 (2026-09-29, by the owner's choice, to cut latency).** Earlier
sections used `--image-min-tokens 1024`, which gave ~1,550 image tokens on `complex.png`.
Measured with a cold server each time:
- `hi.png`: 74.1s at 1,024 min (2,089 prompt tokens) vs 28.4s at 512 (1,138 tokens). Prefill
  61.0s -> 17.4s. Same 2 blocks, same text.
- `complex.png` (3-level nested table): at 512 the model hit the 4,000-token output limit
  after 9m40s and produced no file. It ran once, so this isn't proof it always fails.
- The server warns that Qwen-VL needs at least 1,024 image tokens for grounding tasks.
To go back, use `--image-min-tokens 1024` and drop `--image-max-tokens`.

**Smoke test (one run, not a benchmark):**
`image_to_word.py image_to_word_input/hi.png --base-url http://127.0.0.1:8090` extracted
2 blocks and passed self-consistency. Prefill took 61.0s for 2,089 tokens and generation
12.9s for 131 tokens, 74.1s in total.

System packages still needed (need sudo):
`sudo apt-get install -y git curl tesseract-ocr libreoffice-writer`. Without tesseract,
the OCR checks report N/A. Without LibreOffice, `image_to_pdf.py` and `word_to_pdf.py`
fail. Ollama is not installed, so the `--base-url ""` Ollama path won't work here.

---

## 16. Suspected bugs, from reading the code (NOT verified by running it)

Found in a full read-through on 2026-09-29. Each still needs a real test case to confirm,
the same bar as everything else in this document.

1. **`merge_nested_tables()` with a parent table that has no header.** The parent's header
   part becomes `[]`, so the merged header and `width` come from the child only. The
   parent's earlier rows are then cut down to that width, losing columns, and the expanded
   rows (parent prefix + child row) come out longer than the header.
2. **`merge_nested_tables()` false merges.** Any table whose last cell is `""`, followed
   directly by another table, is merged. Two separate tables where the first ends in a row
   with an empty last cell (a totals row, say) would be joined.
3. **`fix_trailing_heading_after_table()` on multi-page PDFs.** A heading at the bottom of
   a page usually introduces the next page's content. It would be moved in front of the
   table above it.
4. **Bullet stripping in `to_docx.py`.** `line.strip(" •◦▪-–\t")` trims both ends of each
   list item, so "-5% change" loses its minus sign and a trailing dash is dropped.
5. **Minor:**
   - `_PUNCT` folds "·" (middle dot) to "-".
   - The raw-server request always labels the image `image/png`, even for a JPEG that
     skipped upscaling.
   - `test.py`, `test_json.py` and `bench.py` reference files not in this tree
     (`test_page.png`, `real_business_doc.pdf`).
   - `xlsx_output/` is in `.gitignore` but its files are tracked.

---

## 17. PDF → JPG (2026-09-29)

`pdf_to_jpg.py`. Chosen by the owner from two options: render each page directly, versus
extract with the model, rebuild a Word file, convert to PDF, then render. Direct rendering
was picked: PyMuPDF draws each page exactly as it looks, so **no model is involved**, nothing
can be dropped or misread, and it works the same on digital and scanned PDFs.

- **Layout:** each PDF gets its own subfolder, one JPG per page:
  `pdf_to_jpg_input/report.pdf` -> `pdf_to_jpg_output/report/page_001.jpg`, `page_002.jpg`, ...
- **Defaults:** 150 dpi, JPG quality 90. Flags: `--dpi`, `--quality`, `--pages` (e.g. `1-3`),
  `--outdir`. Accepts several PDFs or globs.
- **Guards:** a page whose declared size would render above 5,000 px on its longest side is
  drawn at a lower dpi (some scanners declare the page box in pixels, see `_sane_page_rect()`
  in `test_pdf.py`). Password-protected PDFs are skipped with a message. The written files
  are read back with PIL to check they are valid images.
- **Command:** `.venv/bin/python pdf_to_jpg.py pdf_to_jpg_input/report.pdf`
- **Verification, added the same day at the owner's request** (to show everything was
  converted). Each saved JPG is read back and checked against the PDF (`check_page()`):
  1. *Dimensions*: exactly the page size at the chosen dpi.
  2. *Not blank*: a page with text, images or drawings must not be a white JPG.
  3. *Text lines* (digital pages): every line of the PDF's text layer is looked up by its
     coordinates and that region of the JPG must contain ink. No OCR, so no false alarms
     from OCR misreads.
  4. *OCR words* (digital pages, tesseract needed, `--no-ocr` skips): an OCR reading of the
     JPG must contain at least 90% of the PDF's own words (over 3 characters).
  The run prints a per-page result, a summary, and `VERDICT: PASS` or `FAIL` (exit code 1 on
  FAIL). **Scanned pages (no text layer) only get checks 1 and 2**, and the report says so:
  they are verified as "drawn", not as "every word present". A truly blank page passes
  when the JPG is blank too.
- **The checks were shown to fail, not just pass.** On a real page, tampering with the JPG
  gave FAIL each time: all white (0% OCR, 0/25 lines), middle band erased (2 lines with no
  ink, OCR 71%), top erased (OCR 36%), half-size image (size mismatch). An untouched page
  gave 25/25 lines and 100% OCR.
- **Tested** on 4 PDFs (digital, one with an embedded image, two scanned): all pages written
  and verified, 0.1-2.0s per PDF with OCR, 25-350 KB per page at the defaults. A page with a
  table, paragraphs and an embedded logo was inspected visually and looked correct. `--pages 9` on a
  2-page PDF is skipped with a message and exit code 1. A missing file prints "skipping ...
  not found" and does not count as a failure, like the other scripts.

---

## 18. JPG → PDF (started 2026-09-29)

Folders `jpg_to_pdf_input/` and `jpg_to_pdf_output/` were created. **The conversion already
exists**: `image_to_pdf.py` (model reads the image -> `build_docx()` -> LibreOffice -> PDF)
takes a JPG as-is. No new script has been written yet. Command:
`.venv/bin/python image_to_pdf.py jpg_to_pdf_input/x.jpg --base-url http://127.0.0.1:8090 --outdir jpg_to_pdf_output`

**First test** (page 2 of `Free_Test_Data_100KB_PDF.pdf` rendered to JPG by `pdf_to_jpg.py`,
local 2B at 512 image tokens): 62s (prefill 12.4s, generate 47.4s / 556 tokens). The PDF has
real selectable text and a real table. Problems seen:
- **Character misreads in the text:** "nascetur" -> "nescetur", "inceptos" -> "inceptus",
  "vulputate" -> "voluptate". OCR coverage reported FAIL (96%, 7 lines differ). Not yet known
  whether the 512-token cap causes this; it was not tested at 1,024.
- **The logo picture became the text "FreeTestData".** A raw image has no embedded picture
  objects to copy (unlike a PDF), so the model can only read it, not preserve it.
- **A headerless table got a bold first row** (the model treated row 1 as a header, and its
  own check reported "claims 4 rows but returned 3").
- The final PDF is not read back or verified (unlike `pdf_to_jpg.py`).

---

## 19. PDF → TIFF (2026-09-30)

`pdf_to_tiff.py`. Planned first, then built to the owner's choices: **one multi-page TIFF
per PDF, colour, lossless LZW, 300 dpi.** No model: PyMuPDF draws each page, Pillow writes
the frames. Same for digital and scanned PDFs.

- **Output:** `pdf_to_tiff_input/report.pdf` -> `pdf_to_tiff_output/report.tiff`.
- **Command:** `.venv/bin/python pdf_to_tiff.py pdf_to_tiff_input/report.pdf`
- **Flags:** `--dpi` (default 300), `--compression lzw|deflate` (deflate is about 30%
  smaller, a few old viewers can't open it), `--pages 1-3`, `--outdir`, `--no-ocr`.
- **Sizes measured** for one page at 150 dpi: uncompressed 6,163 KB, LZW 355 KB, Deflate
  249 KB. Black-and-white Group 4 (27 KB) was offered but not chosen, since it loses colour.
- **Memory:** frames are generated one at a time and handed to the writer, because a
  300 dpi Letter page is about 25 MB and a 100-page PDF would otherwise need about 2.5 GB.
- **Oversized pages:** a page declared over 5,000 px on its longest side is drawn at lower
  dpi (e.g. a 23.6x30.6in box drew at 163 dpi), and each frame stores its own dpi.
- **Verification** (the saved TIFF is reopened; ends in `VERDICT: PASS` or `FAIL`, exit 1 on
  FAIL): frame count; per-frame size; stored dpi; **each frame byte-for-byte equal to a
  fresh render** (only valid because LZW/Deflate are lossless); not blank; every PDF text
  line sits on ink (by coordinates, no OCR); OCR reading contains at least 90% of the PDF's
  words. Scanned pages (no text layer) skip the last two and the report says so: they are
  verified as pixel-exact to the PDF, not as "every word present".
- **Shown to fail, not just pass:** a single pixel changed by 1 (1 of 25,245,000 bytes),
  all-white frame, an erased heading band, wrong stored dpi, half-size frame, and a frame
  dropped from the saved file each gave FAIL.
- **Bug found and fixed while testing:** the first version failed one PDF at 88% OCR. Cause:
  the OCR copy of the frame was saved through Pillow with no dpi, so tesseract guessed the
  resolution and missed words. Passing `dpi=` fixed it (100%). Same page, same content.
  `pdf_to_jpg.py` was not affected: it OCRs the JPG PyMuPDF wrote, which carries a dpi.
- **Tested** on 3 real PDFs (digital, one blank page, scanned), a synthetic mixed-size PDF,
  deflate and `--pages`, and bad `--pages`. All passed; 0.3-1.0s to write, 2-6s with checks.

---

## 20. TIFF → PDF (2026-09-30)

`tiff_to_pdf.py`. Planned first; the owner chose a **searchable PDF, one PDF per TIFF (every
frame a page), page size = the frame's physical size.** Options considered and not chosen:
picture-only PDF (kept as `--no-text`), and a model-rebuilt PDF (`image_to_pdf.py`, about a
minute per page, re-typeset and can change letters).

- **No model.** Each frame is embedded as a lossless picture, unchanged, so the page looks
  exactly like the scan. An invisible text layer from Tesseract sits on top
  (`-c textonly_pdf=1` gives a text-only PDF that is overlaid with `show_pdf_page`), so the
  PDF can be searched and copied.
- **Output:** `tiff_to_pdf_input/report.tiff` -> `tiff_to_pdf_output/report.pdf`.
- **Command:** `.venv/bin/python tiff_to_pdf.py tiff_to_pdf_input/report.tiff`
  Flags: `--no-text` (picture only), `--outdir`. Needs tesseract unless `--no-text`; it stops
  with an install hint rather than quietly writing a PDF with no text.
- **Page size:** pixels / dpi * 72, using the dpi stored in the TIFF (a 300 dpi A4 scan is an
  A4 page). No usable dpi (missing, or below 10) is assumed to be 300 and the report says so.
  A genuine centimetre-unit TIFF converts correctly (Pillow turns it into dpi).
- **Colour modes:** 1-bit, greyscale, palette, CMYK, RGBA (flattened onto white) and 16-bit
  are converted to RGB, and the report says which frames were converted.
- **Verification** (the saved PDF and the original TIFF are both read again): page count;
  each page's size; exactly one picture covering the page; **that picture pulled back out of
  the PDF is byte-for-byte the frame**; the words OCR read from the frame are really in the
  PDF's text layer; and a check that a colour conversion didn't turn a real scan flat. Ends
  in `VERDICT: PASS` or `FAIL` (exit 1).
- **Honest limits of the text layer:** it is only as good as Tesseract, which misses text
  printed white on dark backgrounds and reads accented letters as plain ones (§19). The
  text-layer check compares the PDF text with OCR of the same frame, so it proves the text
  was embedded, **not** that OCR read the whole page. The picture is always exact.
- **Gap found and closed while testing:** the pixel-exact check compares the PDF with the
  already-converted RGB image, so a conversion that blackened a page would still pass. A
  fake 16-bit file (values 0-255 instead of the real 0-65535) exposed this. Fixed with the
  "conversion left one flat colour" check; a real 16-bit file converts correctly.
- **Shown to fail, not just pass:** one pixel changed, a wrong picture in the PDF, a wrong
  page size, an extra image on the page, a missing text layer, a flat-black conversion, and a
  page missing from the saved PDF each gave FAIL.
- **Tested** on `pdf_to_tiff_output/ten_page.tiff` (10 frames, 10/10 PASS, 15s to write,
  28s with checks; 1.27 MB PDF from a 3.3 MB TIFF), a scanned 2-frame TIFF, and 13 synthetic
  TIFFs (fax 1-bit Group 4, greyscale, CMYK, RGBA, 16-bit, palette, no dpi, cm unit, mixed
  sizes and dpi in one file, blank, single frame). `--no-text`, a missing file (skipped, exit
  code 0 like the other scripts) and a non-TIFF file ("COULD NOT OPEN") also behave.
- **Still pending from §19:** whether to make the OCR word check advisory in `pdf_to_tiff.py`
  and `pdf_to_jpg.py`. Left unchanged at the owner's request until they test.

### 20.1 Speed-up (2026-09-30): OCR reused, and run in parallel
The owner noticed `sample10.tiff` (10 frames, 2550x3300 px, dense text) was slow. Two
changes were made, both at the owner's request:
1. **The verification no longer runs OCR a second time.** It used to OCR every frame again
   just to get a word list. Tesseract is deterministic, so that repeated the first reading.
   The text of the first reading (the text-only PDF that becomes the text layer) is kept and
   the check is whether that text survived into the saved PDF.
2. **OCR runs on several pages at once** (`--workers`, default `min(4, cpu count)`; 1 = one
   page at a time). Each tesseract process is limited to one thread (`OMP_THREAD_LIMIT=1`),
   and at most `2 x workers` frames are in flight, so memory stays flat (peak about 600 MB
   on this file).

**Measured today, old and new run back to back on `sample10.tiff`:** old 40.5s wall,
new 11.7s wall with 4 workers (21s with `--workers 1`). Output identical to the old version's
on all 10 pages: same text layer text, same embedded picture bytes, for 1 and 4 workers.
**Correction:** an earlier timing in this session put the old run at about 145s (65s OCR +
63s repeated OCR). That figure did not reproduce; single-page tesseract measures about 1.6s
per page with or without the thread limit, so 145s was inflated by something else on the
machine at the time (cause not found). Don't quote 145s.

**Two problems this change caused, both caught by testing and fixed:**
- *Stale page objects.* With OCR results arriving later, holding the `Page` object from
  `doc.new_page()` failed ("page is None"): PyMuPDF invalidates earlier Page objects whenever
  a page is added. 9 of 10 text layers failed. Fix: look the page up by number when the
  result arrives (`doc[i]`).
- *A hole in the verdict.* That same run still printed `PASS 10/10`, because with the second
  OCR gone, a failed text layer looked identical to a page with no text. Fix: a page whose
  text layer could not be built now FAILS explicitly (`layer_error`). Shown by simulating a
  tesseract crash on page 2: `pages verified 9/10`, `VERDICT: FAIL`.
- Lesson: the repeated OCR looked redundant but was also the only independent check that the
  text layer existed. When removing a "duplicate" check, ask what else it was catching.

Regression after the change: 17 of 18 test TIFFs pass; the one FAIL is the deliberately
malformed synthetic `e_16bit.tiff` (fake 16-bit values that convert to black), which the
flat-conversion check is meant to reject. The genuine 16-bit file, `--no-text`, `--workers 8`
and all eight damage tests behave as before.

---

## 21. PDF → TXT (2026-09-30)

`pdf_to_txt.py`. The owner asked to focus on **the model for unscanned (digital) pages**. So
each page that has a text layer is read by the vision model (same one call per page as
`test_pdf.py`, trimmed schema without align/size/bold), returned as blocks, corrected, and
written as text. The PDF's text layer stays the ground truth: it checks the model and
restores dropped lines. The other decisions were left open by the owner, so these are
DEFAULTS chosen by the assistant and easy to change: tables as columns padded with spaces
(a dashed rule under a real header), lists as `- item`, pages separated by a line reading
`----- Page N -----`, UTF-8.

- **Command:** `.venv/bin/python pdf_to_txt.py pdf_to_txt_input/x.pdf --base-url http://127.0.0.1:8090`
  Flags: `--pages`, `--dpi` (default 125), `--outdir`, `--model`, `--base-url`.
  Output: `pdf_to_txt_output/<name>.txt`. Not importable (runs at import, like the others).
- **Scanned pages (no text layer) are NOT converted in this version.** They are listed by
  page number and the verdict is `PARTIAL` (exit 1), never PASS. A page with nothing on it
  is written blank without a model call. A PDF made only of scans writes nothing.
- **A failed model call is not silent** (unlike `test_pdf.py`): the page falls back to the
  PDF's own text in reading order, the report says so, and the verdict notes how many pages
  used the fallback.
- **Pipeline per digital page:** model call -> `drop_duplicate_blocks` -> heading fix ->
  `merge_nested_tables` -> drop text read out of a picture (only if the page has pictures)
  -> `check_coverage` -> `repair_missing_lines` and `repair_table_rows` (**copied** from
  `test_pdf.py`, following the project convention, because that file cannot be imported)
  -> render. Pictures on a page are not in the text and the report says how many.
- **Verification (the saved .txt is read back):** every page has its marker in order;
  each page's words (over 3 chars, containing a letter or digit) are compared with the PDF's
  text layer both ways (coverage at least 95%, words not in the PDF at most 5%); every PDF
  line is present; the file's word count with repeats must be 0.85x-1.15x the PDF's (catches
  duplication); and **any number that differs, in either direction, fails regardless of the
  percentages**. Verdicts: `PASS`, `PARTIAL`, `FAIL`.
- **Shown to fail, not just pass:** deleting a line, inserting invented words, removing a
  page marker, duplicating a paragraph, emptying a page and changing one figure
  (`84,200` -> `84,700`) each gave FAIL. Two gaps were found and closed while testing:
  (1) the dashed table rule was counted as extra words (a bug in this script's own check);
  (2) a single misread number passed because it was 2% of the page, so figures now fail on
  any difference.
- **Measured (local 2B, 512 image tokens, 7 digital pages, final rules): 4 PASS, 3 FAIL.**
  Time per page 22-103 s (generation 10-89 s, 123-981 tokens), roughly the same formula as
  in the handover doc.

  | Page | Result | Why |
  |---|---|---|
  | `sample_digital_document` p1, p2 | PASS | 100% words, nothing extra |
  | `ten_page` p1, p2 | PASS | |
  | `Free_Test_Data` p1 | **FAIL** | model misread dense Latin prose (`etos`, `ibibendum`, `integre`); 10 words not in the PDF |
  | `Free_Test_Data` p2 | **FAIL** | content repeated, 1.43x the PDF's word count |
  | `ten_page` p3 | **FAIL** | model wrote `$1,250.00`; the PDF's text layer says `I1,250.00` (the rupee glyph is broken: it shows as a black box on the page) |

- **Root cause of the repeated content, a design flaw shared with `test_pdf.py`:** when the
  model misreads a paragraph, `check_coverage` sees the correct line as "missing" and
  `repair_missing_lines` INSERTS the exact PDF line as a new block, while the misread
  paragraph stays. The file ends up with both, interleaved. Coverage then reads 100% and
  hides it. The new size check exposes it; nothing fixes it yet.
- **Proposed, not built:** "snap to the text layer": when a model block closely matches a
  span of the PDF's text (similarity above a threshold), replace the block's text with the
  exact PDF text instead of inserting a duplicate. This would fix misreads and duplication
  on digital pages deterministically, and would also benefit `test_pdf.py`. It needs the
  owner's go-ahead.
- **Also untested:** whether 1,024 image tokens removes most of the misreads (the page that
  failed worst was Latin prose at 512). It needs a server restart, so it was not done.
- **Verdict interpretation:** a FAIL here is the check working. It reports that the text
  file differs from the PDF's own text; it does not mean the script crashed.

### 21.1 Update, same day: snap to the text layer, and scanned pages with the model
Both were approved by the owner (retesting at 1,024 tokens was declined; the server stays at 512).
**This supersedes the statements above that scanned pages are skipped, that the verdict can be
PARTIAL, and the 4-of-7 result.**

**Scanned pages now go to the model too.** There is no text layer, so nothing is checked or
restored: the page is marked UNVERIFIED in the report and never counted as verified. Tesseract's
agreement with the model is printed for information only (it cannot fail a page: OCR misses
white-on-dark text and accents). A page is a FAIL only if the model could not read it (a line
`[Page N could not be read: ...]` is written in its place, no OCR is ever written into the
output) or the page came out nearly empty while OCR reads 15+ words. Verdict: `PASS` (with the
number of unverified scanned pages) or `FAIL`; `PARTIAL` no longer exists. Page images sent to
the model are capped at 2,600 px on the long side. Real run, `sample_scanned_document.pdf`
(2 pages): read cleanly, 18.0 s and 20.2 s (203 and 225 tokens), OCR agreement 92% / 100% and
100% / 100%. The server's prompt cache can hide prefill time on repeated inputs.

**`blocks.snap_to_text_layer(page, source_text)` (new, shared, wired only into `pdf_to_txt.py`).**
Each block is matched to the passage of the PDF's own text it was read from, and a close match
is replaced by the PDF's exact words. Three levels: (1) a stretch match of 3+ words (difflib on
words, 75% similarity, length within 0.7-1.4x, never onto a passage already used); (2) the
closest whole PDF line for 1-2 words and for table cells (cutoff 0.8, only if the text is not
already an exact PDF line); (3) a **case-sensitive** word pass for words of 6+ characters that
are not in the PDF but are 88%+ similar to one that is. Invented text with no close match, and
real-but-different words, are left alone (tested). Symbols are not corrected (the text layer's
own symbols can be garbage, e.g. a check mark stored as `\x13`). Result: the model's misread no
longer makes `check_coverage` call the correct line "missing", so the repair step no longer
inserts a second copy beside it.

**`drop_duplicate_blocks(page, source_text=None)`:** with a source, a paragraph may appear as many
times as the PDF has it (counted from the start and end of the passage, because the model's copy
usually carries a misread in the middle). Found because `Free_Test_Data` page 1 genuinely repeats
a paragraph and the old rule dropped the second copy (0.74x the PDF). With no source the
behaviour is unchanged for every other caller.

**Result on the same pages, live model, 512 image tokens: 7 of 7 digital pages PASS** (was 4 of 7):
`sample_digital_document` p1-2, `Free_Test_Data` p1-2 (p3 blank), `ten_page` p1-3. Notable: on
`ten_page` p3 the model's `$1,250.00` is now written as the PDF's exact `I1,250.00` (the PDF's
broken rupee glyph; a faithful copy of the text layer, not of what a viewer shows).
`test_pdf.py` does NOT use the snap yet and still has the duplicate-insertion flaw.

**Bugs found in my own work while building this (all fixed, each caught by a test):** the word
pass first matched case-insensitively and turned a lowercase alphabet into the uppercase one;
the "never snap onto a passage twice" guard stopped the second copy of a genuinely repeated
paragraph from being corrected (fixed by hiding used passages while matching); the repeat
count found 0 occurrences of a paragraph the PDF has twice.
**Not verified:** multi-column layouts, PDFs beyond the seven pages above, and how often the
snap would wrongly "correct" a legitimately different word on documents that were not tested.

### 21.2 Why the model misses things on scans: findings (2026-09-30)
Asked by the owner: the model read everything on `txt_test.pdf` except one hyphen and, in one
run, the label "Date". All numbers below are from the local 2B at 512 image tokens. Small
samples: read them as evidence, not as rates.

**1. The lost hyphen (`SCAN-TEST-001` read as `SCAN-TEST001`) is a resampling effect, not a
recognition limit and not randomness.**
- Full page read from a render: hyphen lost on **9 of 9** reads (renders at 123-129, 200 and 300
  dpi). Raising the render dpi did NOT help.
- The same line read from a tight crop: correct, with the plain "transcribe" prompt and with the
  page-extraction schema prompt alike; also correct from crops up to 1,500 px tall. So neither the
  prompt nor JSON output is the cause, and the model CAN read it.
- Full page read from the scan's own embedded image: hyphen correct on **4 of 4** reads.
- Likely cause (inferred, not proven): a render resamples the scan, the server shrinks it again to
  its fixed token budget, and two resamplings blur a 1-2 px mark. Cutting the page into 3 bands
  rendered at 125 dpi did not help either (hyphen still lost).

**2. But the fix is not a clear win.** `pdf_to_txt.py --scan-image` sends the scan's own image
(only when the page is exactly one upright, full-page image, no drawings, no mask). On
`sample_scanned_document.pdf` page 1 that route lost a whole line on **4 of 4** reads (21 OCR
words missing in total) against **1 of 4** for the render (3 words). A dropped line is the
worse error, so the flag is **off by default** and marked experimental. The routing itself works:
9 of the 11 scanned pages in the repo qualify; the other two (a second image; vector drawings)
would keep the render.

**3. "Date" and other dropped lines are NOT a reading problem: the model skips content.**
Read directly from a strip, the model gets these lines exactly right every time. In full-page
reads the dropped item varies from run to run and often is the LAST item (the "Date" label; the
final paragraph "You can use this file..."), or a middle sentence ("Typical characteristics...").
Observed: "Date" lost in 1 of about 11 reads of `txt_test.pdf`; the "Typical characteristics"
sentence lost in 2 of 3 early reads and 1 of 4 later reads of the other page. Consistent with the
model closing its list early, but that mechanism is a guess. More resolution does not address it.

**4. What does address both:** the OCR-flag then model-re-read step tried on `txt_test.pdf`
(not built into the script). OCR locates a line missing from the output, the MODEL reads only
that strip, and the result is accepted only if it agrees with OCR (at least 0.85). It replaced
the wrong `SCAN-TEST001` with `SCAN-TEST-001` and re-read all 5 lines deliberately removed, every
one exactly (6 of 6). Cost: OCR about 3 s per page plus 16-19 s per flagged line (a strip costs
the full 512-token image read). Its placement was not good enough to ship: a re-read table row
was split over 3 lines, a mid-paragraph fragment landed after the paragraph, and side-by-side
labels caused a duplicate ("Date" twice). It needs (a) per-segment comparison, (b) table rows
re-formatted into the existing table's columns, (c) inline insertion when the neighbouring lines
sit in one paragraph. Not built; awaiting the owner's decision.
