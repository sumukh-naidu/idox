# idox — Handover

Read this first if you are a new AI/developer picking this project up. It explains what
the project is, how it's built, what's been tried, what's currently broken/open, and the
hard constraints you must not violate on this specific machine. For the current-moment
snapshot (latest numbers, immediate next step), see `status.md`.

---

## 1. Goal

A **local, fully offline** pipeline that converts PDFs (and raw images) — containing mixed
prose, tables, headings, and images, both digital (text-layer) and scanned — into Office
formats: Word (`.docx`), Excel (`.xlsx`), and PDF-from-image. No cloud APIs; extraction is
done by a locally-run vision-language model (Qwen3-VL).

A major secondary goal, and the entire focus of the most recent work: **reduce
per-document extraction latency**, with every claim backed by real measurement — the
project owner has repeatedly rejected assumptions and unverified numbers in favor of
direct proof (server logs, tokenizer counts, `/sys` counters, etc.). Don't report a
result you haven't actually measured against the current code/running server.

---

## 2. Architecture overview (tiered)

```
PDF/image -> Tier 0 (deterministic) -> Tier 1/2 (model) -> Tier 3 (deterministic)
```

- **Tier 0 — deterministic parse.** PyMuPDF (`pymupdf`/`fitz`). Renders each page to a
  PNG (`get_pixmap(dpi=...)`, default 125 dpi) for the model, and independently reads the
  real embedded text layer (`get_text()`) when present. `is_scanned = not source_text.strip()`
  is computed per page (`test_pdf.py:749`) — a page with no text layer is "scanned."

- **Tier 1/2 — the model.** Qwen3-VL (2B or 4B, `-instruct`) reads the rendered page PNG
  and emits a JSON object matching the `Page` schema (`blocks.py`) — a list of `TextBlock`
  / `TableBlock` objects, via JSON-schema-constrained decoding. This is currently called
  **unconditionally on every page**, digital or scanned (`test_pdf.py:834`) — see §6,
  this is a known, not-yet-fixed architectural gap.

- **Tier 3 — deterministic file writing only.** `to_docx.py` (python-docx) and
  `to_xlsx.py` (openpyxl) turn `Page` objects into real Office files. **The model never
  writes Office bytes** — this boundary is intentional and load-bearing, keep it.

- **Post-extraction correction, digital pages only.** `measure_block_look()`
  (`test_pdf.py:394`, called at `test_pdf.py:1036` guarded by `if not is_scanned:`)
  **overwrites** the model's `align` / `size` / `bold` guesses using direct text-layer
  measurement (font size, position, weight) — the model's own guesses for these 3 fields
  are discarded on digital pages. This is the proof-of-concept that deterministic
  extraction is already trusted over the model for digital pages, on formatting at least.

- **Validation — structural conformance, not business-schema matching.** Four checks, run
  after extraction:
  - `check_structure` (`blocks.py:339`) — self-consistency (works even on raw images with
    no ground truth, e.g. in `image_to_pdf.py`).
  - `check_grounding` (`blocks.py:621`) — extracted text found in the real text layer.
  - `check_geometry` (`test_pdf.py:569`) — block/table geometry vs. Tier-0 geometry.
  - `check_coverage` (`blocks.py:431`) — fraction of source words present in the output
    (95% default threshold).
  Repair helpers exist (`repair_missing_lines`, `repair_table_rows`, `test_pdf.py:209,311`)
  to patch specific failure classes rather than re-running the model. Do **not** trust the
  model's self-reported confidence — a failed structural check is what should drive
  escalation/repair, not the model's own claim.

---

## 3. Stack

- **Python**, project venv at `.venv/`.
- **PyMuPDF** (`pymupdf`/`fitz`) — PDF parsing, page rendering, text-layer access.
- **pydantic** — the `Page` / `TextBlock` / `TableBlock` schema (`blocks.py`), and
  JSON-schema-constrained decoding (`response_format: json_schema` on the model call).
- **python-docx**, **openpyxl** — deterministic Office file writers (Tier 3).
- **LibreOffice** (`soffice --headless`) — used for Word<->PDF conversion
  (`word_to_pdf.py`, and a duplicated copy of the same logic inside `image_to_pdf.py` —
  see §5 for why it's duplicated, not imported).
- **Model serving — two parallel paths, only one currently in active use:**
  1. **Ollama daemon** — `qwen3-vl:4b-instruct` / `qwen3-vl:2b-instruct`, registry-bundled
     model+mmproj (mostly F16 mmproj). This is the *original/default* path
     (`extract_page(..., base_url=None)`), but is **currently out of scope** — the project
     owner explicitly said "remove ollama from the picture for now."
  2. **Raw `llama-server` binary** (`/usr/local/lib/ollama/llama-server` — same engine
     Ollama wraps, invoked directly, bypassing Ollama's daemon/Modelfile system).
     Pointed at manually-downloaded GGUF files from **Qwen's official HF repo**
     (`Qwen/Qwen3-VL-2B-Instruct-GGUF`), stored in `models_manual/`:
     - `Qwen3VL-2B-Instruct-Q4_K_M.gguf` (1.03GB) — the LLM.
     - `mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf` (424MB) — the vision encoder.
     Reached via `extract_page(..., base_url="http://127.0.0.1:8090")` ->
     `_extract_page_raw_server()` (`blocks.py:775`), which calls the raw
     `/v1/chat/completions` endpoint directly (OpenAI-compatible) and reads the
     llama.cpp-specific `timings` block (`prompt_ms`/`predicted_ms`) for real per-phase
     timing. **This is the currently-active path for all testing.**
  Launch command for the manual server (CPU-only — see §7 for why no `-ngl`):
  ```bash
  nohup /usr/local/lib/ollama/llama-server \
    --model /home/aiteam/idox/models_manual/Qwen3VL-2B-Instruct-Q4_K_M.gguf \
    --mmproj /home/aiteam/idox/models_manual/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
    --port 8090 --host 127.0.0.1 \
    --no-webui --ctx-size 8192 --image-min-tokens 1024 \
    > /tmp/llama_manual_perf.log 2>&1 &
  disown
  ```
- **Hardware**: i7-1185G7 laptop, 4 cores / 8 threads, 28W-class, LPDDR4x RAM (~50GB/s
  bandwidth estimate). CPU-only. See §7 for hard constraints on this specific machine.

---

## 4. Key files

| File | Role |
|---|---|
| `blocks.py` | `Page`/`TextBlock`/`TableBlock` pydantic schema, `SYSTEM_PROMPT`/`USER_PROMPT`, `extract_page()` (routes to Ollama or raw server), `_extract_page_raw_server()`, the 4 `check_*` validation functions. |
| `test_pdf.py` | Main PDF -> Word/Excel runner. CLI flags: `--dpi` (default 125), `--model`, `--base-url`, `--pages`, `--scan-mode`, `--no-ocr`. Contains `is_scanned` detection, `measure_block_look()`, `check_geometry()`, repair helpers, and the latency-breakdown summary printed at the end of a run. |
| `to_docx.py` | Deterministic Word writer (Tier 3). No model involvement. |
| `to_xlsx.py` | Deterministic Excel writer (Tier 3). One sheet, tables as grids, prose in column A, all cells stored as text (no silent type conversion), images embedded at their displayed PDF size. |
| `image_to_pdf.py` | Raw image -> PDF, chaining `extract_page()` -> `build_docx()` -> LibreOffice. Has its own `--base-url` flag. Contains copied (not imported) `find_soffice()`/`docx_to_pdf()` — see §5. No ground truth exists for a raw image (no text layer), so grounding/coverage checks can't run here; only `check_structure` (self-consistency) applies. |
| `word_to_pdf.py` | Standalone LibreOffice-based Word->PDF converter script. Runs its own `argparse` at import time — **do not import this module**, copy what you need (see §5). |
| `models_manual/` | Manually downloaded HF GGUF files (see §3). |
| `pdf_to_word_input/` / `pdf_to_word_output/` | New dedicated I/O folders for PDF->Word/Excel latency testing, created this session. |
| `image_input/` / `pdf_out_img/` | New dedicated I/O folders for `image_to_pdf.py` testing, created this session. |
| **Older/original test files** (`sample_digital_document.pdf`, `mixed_*.pdf`, `scanneds.pdf`, etc.) | Pre-existing test assets from before this session. **Currently showing as deleted in `git status`** — see §8, this needs the project owner's attention, not silent action. |

---

## 5. Project conventions worth knowing

- **Duplicate small reusable pieces across independent scripts rather than importing
  across them**, when the source script runs top-level `argparse` (i.e. is a script, not a
  library). Stated explicitly in `word_to_pdf.py`'s own docstring, and the reason
  `image_to_pdf.py` has its own copies of `find_soffice()`/`docx_to_pdf()` instead of
  `from word_to_pdf import convert` (which was tried first and broke `image_to_pdf.py`'s
  own CLI args, since importing a script runs its module-level `argparse.parse_args()`).
- **Previously-created input/output folders must be left untouched** when adding new ones
  for a new testing purpose — explicit project-owner instruction from earlier in the
  session.
- **Don't trust a WebFetch/third-party summary of model file sizes/specs without a direct
  `ls -la`/download check** — a real discrepancy (1.83GB vs actual 424MB for the Q8_0
  mmproj) was caught this way earlier in the project.
- **Prompt-cache artifacts**: repeating the identical prompt/image against a warm server
  produces artificially low `prefill_s` (KV-cache reuse), not a real speedup. Always vary
  the input (or accept only `generate_s`/token-count-normalized numbers) when comparing
  configurations.

---

## 6. Known, real, NOT-yet-fixed opportunities (in priority order)

1. **Skip model-driven content extraction on digital pages.** (Biggest identified lever,
   not yet built.) `test_pdf.py` already computes `is_scanned` (line 749) but calls
   `extract_page()` unconditionally on every page (line 834) — a digital page's *content*
   is re-derived by the model token-by-token (~78ms/token, memory-bandwidth-bound, see
   §7) even though `pdf_page.get_text()` already has the ground-truth text for free,
   instantly, zero tokens. `measure_block_look()` already proves the deterministic path is
   trusted over the model for *formatting* on digital pages — the same logic hasn't been
   extended to *content* or *block typing* yet. This would cut most/all of the generation
   phase (currently the dominant cost, ~55-62% of total latency) on any digital-PDF run.

2. **Schema-trimming.** Smaller, more mechanical: stop asking the model to *generate*
   `align`/`size`/`bold` fields at all on digital pages, since `measure_block_look()`
   discards and overwrites them anyway. Direct, proportional token-count cut to the
   generation phase. Not yet built — was about to be scoped when this handover was written
   (the discussion of "will the content be lost" for this specific change was interrupted
   mid-explanation — the project owner had NOT yet gotten a final answer on whether trimming
   these 3 fields from the schema risks losing any content; the honest answer, not yet
   delivered, is **no** — these 3 fields are pure formatting metadata, never used as block
   *content*, and are already fully overwritten post-hoc on digital pages, so omitting them
   from the schema on digital-page requests loses nothing that currently survives anyway).

3. **Further quantization below Q4_K_M.** Untested. Generation is memory-bandwidth-bound
   (see §7) — fewer bytes per weight = less to stream per output token = proportionally
   faster generation. Real lever, accuracy tradeoff unknown, not attempted.

4. **Speculative decoding** (`llama-server --model-draft`). A small draft model guesses
   several tokens ahead; the real model verifies them in one batched (compute-bound,
   weight-reused) pass instead of many sequential bandwidth-bound single-token passes.
   Mentioned, not scoped or attempted. Would need a smaller Qwen3-VL draft model.

---

## 7. Hard constraints on THIS machine — do not violate

- **Never re-enable iGPU offload** (`-ngl`, `GGML_BACKEND_PATH`, any Vulkan-device flag on
  `llama-server`) on this laptop. This was tried once — it caused a genuine full-system
  freeze requiring a forced reboot. It was empirically confirmed to help only the prefill
  phase (compute-bound: 68.62 tok/s vs CPU-only) and do nothing for generation (unchanged
  ~12.11 tok/s, since generation is memory-bandwidth-bound and the iGPU shares the same
  RAM bus as the CPU) — so the upside was already known to be limited even before the
  freeze. Not worth revisiting on this hardware.
- **Never set the CPU governor to `performance`** on this laptop. Measured, not assumed:
  `performance` produced a **slower** run (192.94s) than `powersave` (154.10s) on an
  identical prompt/image, because this thin-chassis i7-1185G7 cannot sustain the near-max
  turbo clocks `performance` demands — confirmed via `/sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count`
  showing 4504 package-level throttle events during the run. `powersave` is the correct,
  already-confirmed-best setting; do not re-test this without new evidence.
- Generation cost is **memory-bandwidth-bound**, not compute-bound, not disk-bound. The
  model is already resident in RAM after server startup (`load_s: 0.0` on every
  subsequent page) — "warm mode" already exists and is not the problem. Every output
  token still requires streaming the full weight set from RAM fresh (matrix-VECTOR
  multiply, no weight reuse across sequential single-token steps) — this is inherent to
  autoregressive batch-size-1 decoding, not a configuration issue. Don't propose CPU
  clock/thread tuning as a fix for generation latency; it won't work (see governor result
  above), and prefill (compute-bound) is the only phase clock speed genuinely helps.

---

## 8. Current known problems / loose ends

- **Working tree is dirty and nothing has been committed this session.** `git status`
  shows real edits to `blocks.py` and `test_pdf.py` (uncommitted), a large number of
  **deleted** tracked test-asset files (`sample_digital_document.pdf`, `mixed_hard.pdf`,
  `scanneds.pdf`, etc. — unclear if this was intentional cleanup or an accident from
  earlier in the project; **do not `git add`/commit these deletions without asking the
  project owner first**), and several new untracked directories/files
  (`models_manual/`, `image_input/`, `pdf_out_img/`, `pdf_to_word_input/`,
  `pdf_to_word_output/`, `image_to_pdf.py`, `.claude/`). Branch:
  `feature/testing_2b_Q8mmproj`.
- **154.10s for a single digital page is still considered too slow** by the project
  owner (target implicitly ~30s, referenced against phone and Mac Mini M4 benchmarks).
  Not yet achieved. The two highest-leverage unbuilt fixes are listed in §6 #1 and #2.
- **Validation pass/fail state on the raw-llama-server path hasn't been freshly
  re-verified** in the most recent session — the 4 checks are wired into `test_pdf.py`
  (confirmed by reading the code), but no one has re-confirmed all 4 currently pass
  cleanly against the raw-server extraction path on the latest test documents.
