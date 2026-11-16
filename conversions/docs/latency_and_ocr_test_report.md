# idox: tests performed on machine B (i7-1185G7, 4 physical cores, 15 GB, no NVIDIA, CPU only), 2026-10-01

Purpose of this document: so you can (1) understand what was already tested so you do not repeat it, (2) reproduce any test with the same method, and (3) compare your machine's numbers like-for-like. Nothing was changed in the repo; every experiment used scratch scripts that call the repo's unmodified scripts.

## 0. Setup used for every test
- Repo branch `feature/testing_2b_Q8mmproj` at de9c634 (remote branch NOT merged).
- Model: Qwen3-VL-2B-Instruct, Q4_K_M weights + Q8_0 mmproj, checksum-verified. Server: Ollama's bundled llama-server build 8c146a836 (other machine: llama.cpp b11247, so builds differ).
- Server command (one slot, CPU only, 512 image tokens, never the iGPU):
  `llama-server --model Qwen3VL-2B-Instruct-Q4_K_M.gguf --mmproj mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf --port 8090 --host 127.0.0.1 --no-webui --ctx-size 8192 --image-min-tokens 512 --image-max-tokens 512 --device none -ngl 0 --parallel 1`
- Every model script needs `--base-url http://127.0.0.1:8090` (the repo default 10.0.3.33:8080 is dead). Run scripts from a scratch dir (they write `render/` relative to cwd).
- Standard inputs: `text_only_1page.pdf` (287 words, no tables, the benchmark page), `sample.pdf` (a page with a table), `hi.png`, and `sample.pdf` rendered at 125 dpi (`sample_125dpi.png`).
- Accuracy gate agreed with the user: word precision >= 90% AND recall >= 90%, numbers exact (no number added or missing), and no worse than the unchanged baseline (1-point tolerance). Scorer (multiset word counts, so duplicated words lower precision; this differs from the other machine's formula, which gave 97% where this gave 82.9% on the same page):
  `inter = sum(min(ref[w], out[w]))`, precision = inter/len(out), recall = inter/len(ref); numbers scored the same way on `re.findall(r"\d[\d,.]*")`.

## 1. Latency model (established first)
time per page = prefill (image + 630-token system prompt, about 17-20 s) + output tokens / about 12-14 tok/s. The model call is 99.9% of PDF to Word time (validation 0.05 s, docx writing 0.04 s). Generation is 68-77% of the time on a page, so cutting output tokens or skipping the model is what matters.

Cold baseline: `hi.png` to Word 30.3 s (prefill 20.6 s / 1170 tok, generation 9.6 s / 132 tok). Benchmark page to Word 66.7 s (prefill 20.4 s / 1133 tok, generation 45.6 s / 582 tok at 12.8 tok/s). The handover's 154 s figure for that page is stale.

## 2. Baseline of all 12 implemented routes (this machine)
8 call the model; 4 do not. JPG to PDF and PNG to PDF go through `image_to_pdf.py`, which calls the model.

| Route | Time | Notes |
|---|---|---|
| PDF to Word, benchmark page | 66.7 s cold; 47.8 / 45.6 / 48.4 s in 3 suite baselines | suite baselines were not fully cold (see section 7) |
| PDF to Word, sample.pdf (table) | 38.9 / 37.6 / 40.0 s | 100/100 accuracy |
| PDF to TXT, benchmark page | 47.7 / 47.3 / 49.1 s | 100/100 (uses snap_to_text_layer) |
| Image to Word, hi.png | 26.7 / 27.2 / 27.9 s | 100/100 |
| Image to Word, sample page image | 48.9 / 45.8 / 48.2 s | P 100, R 97 |
| PDF to Excel, sample | 48.7 s | 100/100 |
| PDF to PPT, sample | 25.8 s | cache-assisted (same page as the Excel run) |
| Image to Excel, sample page | 35.6 s | P 100, R 97 |
| Image to PDF, hi.png / sample page | 28.7 s / 49.5 s | 100/100 / P 100, R 97 |
| Word to PDF (1-page docx) | 1.4 s | no model. The script's FAIL verdict is a verifier artifact on the "->" character |
| PDF to JPG, 4 pages | 2.7 s | no model |
| PDF to TIFF, 10 pages | 10.5 s | no model. The FAIL verdict is an OCR word-check weakness; the pages are pixel-exact |
| TIFF to PDF, 10 frames | 12.6 s searchable, 6.2 s picture-only | no model |

## 3. Tweak tests and verdicts (fresh server per config, 5 real routes, noise about +-3-5% judged from repeated baselines)

| # | Test | Method | Result | Verdict |
|---|---|---|---|---|
| T1 | Thread count / pinning (micro-bench, 150-token generation, cache off, 3 runs each) | default auto vs pinned 4 (`--threads 4 --cpu-mask 0xF --cpu-strict 1`) vs 3 vs 8 vs split 4 gen/8 prefill | default (auto = 4, one per physical core) 51.9 prefill tok/s, 13.94 gen tok/s. Pinned +3% prefill, +5% gen (inside noise). 3 threads: prefill -15%, gen -5%. 8 threads: prefill -2%, gen -13%. 4/8 split: -5% / +4%. Drift check of default: -2% / -3% | default is optimal; do not spend more time on threads |
| T2 | Pinned threads on the 5 real routes | same flags | +5%, +6%, +6%, +7%, -3% wall vs baseline | rejected |
| T3 | Speculative decoding, n-gram (no extra model), micro-bench with prompt cached, 300 tokens | `--spec-type ngram-simple / ngram-map-k / ngram-mod` | micro: simple -3% to +6%, map-k +11%, mod +130% (the +130% is a replay of text the previous request had just produced) | looked promising in the micro-bench only |
| T4 | Speculative decoding on the 5 real routes | map-k n=5 m=16; mod | map-k: -6%, -3%, -6%, +1%, -9% with identical accuracy (inside noise). mod: +8%, +10%, -64% (replay artifact), -1%, -4% | no proven gain |
| T5 | Parallel pages (client sends N pages concurrently, 150 tokens each, full prefill each) | `--parallel 1/2/4` | 35.2 s per page sequential, 31.2 s with 2 slots (-12%), about 30 s with 4 slots (no meaningful extra gain, 6.8 GB RAM, per-request speed 2-6 tok/s). An overlapping apt install tainted one 4-slot run, which was re-run clean (30.7 s per page) | real, but needs client-side concurrency code |
| T6 | JSON grammar overhead | schema-constrained vs unconstrained | 13.78 vs 13.43 tok/s | no cost; removing the grammar gains nothing |
| T7 | Token audit | tokenised the real output | 578 output tokens, 31% scaffolding (page text about 396). Compact re-encoding would be 480 tokens (-17%) | basis for T8 |
| T8 | Compact JSON, ZERO whitespace (`":"`, `","`) via a GBNF grammar built from the request's own schema, injected by a request-rewriting proxy | 5 routes | 37.0 s (-23%), 31.0 (-20%), 38.8 (-19%), 23.8 (-11%), 52.9 (+8%). BUT the image table route lost every number (number precision 0%, recall 94%, JSON junk in cells) and PDF table precision fell 100 to 96% | FAILS the gate |
| T9 | Light compact JSON (ONE space after `:` and `,`, no newlines, no indentation) | same proxy, `--light` | see table below | passes the WORD gate, but changed block structure on hi.png (see correction) |
| T10 | Model-free picture embed (JPG/PNG to PDF with no model call) | prototype | 47 s to 24 ms. Pixel-identical, but a JPG source ends up stored as PNG, so a JPEG passthrough check is still open | biggest win, changes behaviour, owner decision |
| T11 | Closed, do not retry | iGPU offload (froze the previous laptop; the build ships a Vulkan backend), performance governor, vision-encoder precision, render dpi (moot under the 512-token cap) | | |

T9 results (3 baselines vs light JSON, each config on a fresh server):

| Route | Baselines (s) | Light JSON (s) | Change | Accuracy |
|---|---|---|---|---|
| PDF to Word, benchmark page | 47.8 / 45.6 / 48.4 | 44.7 | -5% | unchanged (P 83, R 100) |
| PDF to Word, sample (table) | 38.9 / 37.6 / 40.0 | 34.7 | -11% | unchanged (100/100) |
| PDF to TXT | 47.7 / 47.3 / 49.1 | 44.5 | -7% | unchanged (100/100) |
| Image to Word, hi.png | 26.7 / 27.2 / 27.9 | 25.0 | -8% | unchanged (100/100) |
| Image to Word, sample page | 48.9 / 45.8 / 48.2 | 44.7 | -6% | recall 97 to 100 |

**CORRECTION (added after the first version of this report): light JSON is NOT yet safe to enable.** The word-level gate cannot see block structure. Re-tested on `hi.png` with the repo's own `image_to_word.py`, restarting the server before every run: with light JSON the model returned the two lines as ONE block (`"Hello, this is a demo!\nGenerated as a PNG image with text"`) plus an empty block, 5 of 5 cold runs, so the headline lost its own size; without light JSON it returned two separate blocks 3 of 3 cold runs. Warm (prompt-cached) light runs returned two blocks, so the effect depends on cache state. The words are identical, which is why the gate and the suite scored it 100/100. Treat the 5-11% as a speed measurement only: before enabling it, compare block counts and kinds against the normal request on cold runs for every test image (hi.png, fol.png, demo2/3, Demo_image, page, sample_image, test, the table pages). In the code it stays OFF by default (`IDOX_LIGHT_JSON=1`).

Every route beat every baseline; one run per config, so repeat once before relying on it. Mean gain about 7.5%. The server accepted the `grammar` field on the first request. It has NOT been verified on llama.cpp b11247.

## 4. Cold-protocol benchmark vs the other machine (its CSV protocol: fresh server, run 1 cold, runs 2-3 prompt-cache replays)

| Input to Word | This machine cold / cached / cached | Other machine cold / cached / cached |
|---|---|---|
| benchmark page | 66.7 s (separate earlier cold run; no cached repeats run here) | 77.5 / 54.6 / 56.0 s (693 output tokens, 11.6 tok/s) |
| sample.pdf | 47.4 / 25.5 / 28.0 s | 44.0 / 27.5 / 27.5 s |
| hi.png | 30.3 s (separate cold run) | 31.5 / 13.1 / 11.7 s |
| sample_125dpi.png | 53.7 / 33.7 / 33.4 s | 57.1 / 41.2 / 41.6 s |

- Cold times agree within 4-16%. The 16% gap on the benchmark page is mostly output length (582 vs 693 tokens, same page and prompt), not hardware: generation is 12.8 vs 11.6 tok/s here (about 10% faster), prefill about 9% faster there.
- Replay (runs 2-3) saves 17-23 s because the whole prompt and image come from cache (prefill 17-20 s becomes 0.1 s). Do NOT compare a cached run with a cold one.
- Their picture-only Word embed takes 0.3 s vs 31-57 s with the model.
- Their accuracy: recall 100% everywhere, precision 97-100%. Their `sequence_pct` is 78% for Word/Excel/PPT but 98.6% for TXT, which shows the same repair-step duplicate-line problem as here (section 5). Their `hi.png` to Excel is marked failed (the image has no table, so probably an empty workbook; not tested here).

## 5. Accuracy findings
- Benchmark page, raw model output alone: precision 99.3%, recall 96.9%, numbers exact (passes).
- `test_pdf.py` PDF to Word on the same page: precision 82.9%, numbers 71%, recall 100%. Cause: the repair step inserts the exact PDF lines next to the model's slightly different versions, creating duplicates. `pdf_to_txt.py` reaches 100% because it also calls `snap_to_text_layer`. Fix = call `snap_to_text_layer` in `test_pdf.py` (a repo edit, owner decision).
- `simple_2page.pdf` PDF to Word (user run): precision 89.4%, recall 92.3%, number recall 96.9%, so it FAILS the gate. Page 1 table came out shifted: header `[blank,1,2,3]`, then an all-empty row, then correct rows (PDF has a clean 4x4 grid, which `find_tables()` reads exactly; page 2's identical table was correct). Causes: (a) the model misread the header; (b) `repair_table_rows` skips the header row when `has_header` and only re-inserts missing data rows, never removes blank ones; (c) `check_geometry` only prints a warning. Proposed fix: when the model's table shape differs from `find_tables()` and the PDF has a text layer, replace the model's table with the detector's cells; drop all-empty rows.
- Layout vs source for that file: margins match exactly (digital route measures them), but body text is justified in the source and left-aligned in the Word file, because the schema's `align` only allows left/center/right (blocks.py, TextBlock). Lines are also split mid-sentence into separate paragraphs and duplicated, giving 3 pages instead of 2.

## 6. OCR-geometry layout fix for IMAGE to Word (formatting)
Problem found on `fol.png`: the model labels every heading `center` + `large`, the writer turns that into 22 pt centered headings (source: one centered title, left-aligned smaller subheads), and an all-empty table row is kept. There is no layout measurement for images.

Fix prototyped OUTSIDE the repo (`~/idox_runs/ocr_layout_test/`): Tesseract `image_to_data`, match each model block to its OCR line, classify alignment from line centre vs text-area centre (text area = true min x0 to max x1), size from descender-discounted line height with a character-weighted body size, drop all-empty table rows. Text is never changed.

Tests:
- `fol.png`: title centre is 0.5 px off the area centre (center), subheads start at the left margin (left). 5/5 blocks correct.
- 8 images in `image_to_word_input/` (demo2, demo3, Demo_image, fol, hi, page, sample_image, test), fresh model run: **53/53 matched blocks aligned correctly**. 9 wrong model guesses fixed (demo3: 3, page: 4, fol: 1, hi: 1; all were left-aligned headings the model centred), none broken. Only 3 real centred blocks exist in this set (fol title; test.png "Description" and "Title of Invention : Sample Application"), so the set has few positives.
- Synthetic python-docx documents with known alignment (centered, right, left, justified; margins 1.0/1.0, 1.5/0.75, 2.0/2.0 in): 35/36. The miss: a centred title that fills the entire column, which is geometrically identical to left-aligned. The tool now keeps the model's value for such headings.
- Timing: layout step 0.12-1.02 s per image (mean about 0.6 s) vs 28-103 s model time (0.8%). Tesseract is already run by the route for its grounding check, so one pass could serve both.
- Page counts: page.png 2 pages to 1 (source is 1 page); the others unchanged.
- Sizing bug found and fixed during testing: a 1.29x first line made the rule enlarge a body paragraph to 14 pt (body 11 pt). Fixes: never resize long paragraphs, discount descender letters, character-weighted body size, merge heading measurements within 0.12 into one level. hi.png headline now 2.06x (22.5 pt).
- Tried and rejected: detecting bold headings from ink density (body lines on one page ranged 0.61-1.45, bold headings 1.04-2.35; unusable). Body-sized bold headings therefore lose their bold.
- Not fixed (model content errors): dropped bullets and lines (demo3, test, page), unread table (Demo_image), missing "Sample table"/"Key points" headings and a misread bullet on fol.png, which vary run to run even at temperature 0.

## 7. Confounds that cost time (avoid)
1. Prompt-cache replay: a second request with the same page and prompt is 17-23 s faster and may produce a different number of tokens (693 vs 637 on the benchmark page on the other machine). Restart the server between measurements; the suite's later routes share the system-prompt prefix, which also understates cold times by about 8 s.
2. First request after a restart is about 6 s slower (model page-in); warm up and discard it.
3. Two servers at once pushed RAM to 80% of 15 GB and corrupt timings; run one job at a time (the model server is about 3.7 GB, 4 slots about 6.8 GB).
4. Thermal drift and model nondeterminism: about +-5%.
5. A shell wait-loop using `pgrep -f <script>` matches its own command line and never ends. Poll a log file or use `until grep -q ...` instead.

## 8. Not done yet
Repeat of the light-JSON run; image-token count (384 / 768) vs accuracy; ubatch size and flash-attn; skip-the-model for digital PDF pages with no tables; structure-only model output; `n_predict` cap and retry for runaway pages (`complex.png` once wasted 9m40s); a smaller alternative VLM; extraction cache keyed by file hash (so Word, Excel, PPT and TXT of one PDF cost one model call; the other machine's CSV shows four separate 78 s model calls for one page); JPEG passthrough check for model-free picture embed.

## 9. Ranking of levers by measured effect
1. Skip the model / model-free picture mode: 30-60 s saved per page, behaviour change (owner).
2. Extraction cache across output formats: the 2nd to 4th conversion of the same file becomes nearly free (behaviour-neutral).
3. Light compact JSON: about -5 to -11% wall time, gate passes.
4. Two parallel pages: about -12% per page, needs client code.
5. Threads, speculative decoding, 4 parallel slots, pinning: no proven gain.

## 10. Reproducing
`~/idox_runs/latency_tests/` on machine B holds: `scripts/acc.py` (scorer), `evalset.py` (the 5-route run and compare), `suite.py` and `suite_light.py` (one fresh server per config, a discarded warm-up, then the 5 routes), `gbnf_proxy.py` (request-rewriting proxy: `--compact` zero whitespace, `--light` one space; it replaces `response_format` with a GBNF grammar built from the request's own schema), `bench_cold.py` (cold + 2 cached runs, the CSV protocol), `extra_pass.py` (the other 7 routes), `par4.py`, `experiments.py`, `thread_bench.py`, `embed_probe.py`. `logs/` has every raw log. The scripts hard-code scratch-dir and repo paths (`S`, `REPO`, port numbers); edit those first. The OCR work is in `~/idox_runs/ocr_layout_test/`.
