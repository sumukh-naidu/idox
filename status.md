# idox — Status

Snapshot of the project *right now*. For full background, architecture, and stack, see
`handover.md` — this file only covers current state, latest numbers, and the immediate
next step.

**Last updated:** 2026-09-21
**Active branch:** `feature/testing_2b_Q8mmproj`
**Active model path:** raw `llama-server` (NOT Ollama — explicitly out of scope for now)
serving manually-downloaded Qwen3-VL-2B-Instruct GGUF files from Qwen's official HF repo.

---

## What's currently running

```
/usr/local/lib/ollama/llama-server \
  --model /home/aiteam/idox/models_manual/Qwen3VL-2B-Instruct-Q4_K_M.gguf \
  --mmproj /home/aiteam/idox/models_manual/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
  --port 8090 --host 127.0.0.1 \
  --no-webui --ctx-size 8192 --image-min-tokens 1024
```
CPU-only. No `-ngl`/iGPU flags (permanently ruled out on this hardware — see
`handover.md` §7). CPU governor: `powersave` (confirmed best; `performance` was tested
and measured slower — see below).

---

## Canonical latest benchmark (the reference number for all comparisons)

Test file: `pdf_to_word_input/text_only_1page.pdf` — 1 digital page, rendered 1063x1375px
@125dpi.

```
python test_pdf.py pdf_to_word_input/text_only_1page.pdf \
  --base-url http://127.0.0.1:8090 \
  --docx pdf_to_word_output/text_only_1page_perf.docx \
  --out pdf_to_word_output/text_only_1page_perf.md
```

```
PHASE 1  extraction  (PDF -> data)     154.10s
      reading page (prefill):           68.72s  -- 2077 input tokens (1434 image + 643 text)
      generating (writing output):      85.08s  -- 1089 output tokens
```

Input token breakdown verified directly against the running server's `/tokenize`
endpoint (not estimated): `SYSTEM_PROMPT` = 630 tok, `USER_PROMPT` = 13 tok, image =
1434 tok (2077 total prefill minus the 643 text tokens).

This is currently judged **too slow** — no explicit numeric target was set, but ~30s has
come up repeatedly as a comparison point (phone-class and Mac Mini M4 benchmarks).

---

## Investigated and CLOSED (do not re-test without new evidence)

| Lever | Result | Verdict |
|---|---|---|
| Q8_0 vs F16 mmproj (vision encoder precision) | 107.9s vs 108.83s — no meaningful difference | Closed. Vision encoder read once/image; LLM dominates. |
| iGPU offload (`-ngl`, Vulkan) | Helped prefill (68.62 tok/s) only, not generation (unchanged ~12.11 tok/s) — then caused a full system freeze/forced reboot | **Permanently ruled out on this machine.** |
| CPU governor `performance` vs `powersave` | `performance` measured SLOWER (192.94s vs 154.10s) — 4504 thermal-throttle events on this thin-chassis chip | Closed. Stay on `powersave`. |

---

## Open / not yet built (in priority order)

1. **Skip model content-extraction on digital pages.** `is_scanned` is already computed
   (`test_pdf.py:749`) but not used to gate the model call (`test_pdf.py:834` calls
   `extract_page()` unconditionally). Biggest identified remaining lever — could remove
   most of the 85s generation phase on digital-PDF runs. Not started.
2. **Schema-trimming** — stop generating `align`/`size`/`bold` on digital pages, since
   `measure_block_look()` already discards and overwrites them post-hoc. Smaller,
   mechanical, proportional cut. Not started. (The project owner asked "will the content
   be lost" right before this handover was written — answer is **no**, these are
   formatting-only fields already overwritten on digital pages; this still needs to be
   said back to them directly as the first message of the next session.)
3. Further quantization below Q4_K_M (untested, accuracy tradeoff unknown).
4. Speculative decoding via `llama-server --model-draft` (untested, not scoped).

---

## Immediate next step

Answer the project owner's pending question directly ("will the content be lost?" — no),
then get a decision on whether to build #1 or #2 above first (#1 is bigger impact but
bigger change; #2 is smaller and safe to do first as a quick win).

---

## Git state (needs owner attention, not silent action)

Uncommitted: `blocks.py`, `test_pdf.py` (modified). A large number of tracked test-asset
PDFs show as **deleted** in `git status` — unclear if intentional; do not stage/commit
these without asking first. New untracked dirs: `models_manual/`, `image_input/`,
`pdf_out_img/`, `pdf_to_word_input/`, `pdf_to_word_output/`, `image_to_pdf.py`, `.claude/`.
