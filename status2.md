# idox — Status 2

Snapshot of the project *right now*. For full background, every bug's root cause, and the
complete architecture, see `memory.md` — this file only covers current state and the
immediate open decision.

**Last updated:** 2026-09-25
**Branch:** `feature/testing_2b_Q8mmproj` — working tree clean, latest commit `58b4e1d`.
**Model in use:** raw `llama-server` on `http://127.0.0.1:8090` (Qwen3-VL-2B-Instruct,
Q4_K_M + Q8_0 mmproj, manually downloaded from Qwen's official HF repo). **This is now the
automatic default everywhere** — no script needs `--base-url` typed anymore; omitting it no
longer silently switches to a different model (that was a real bug, fixed this session).

---

## What's working, right now, verified

- **PDF → Word/Excel** (`test_pdf.py`) — digital pages: content extraction is trimmed
  (no wasted `align`/`size`/`bold` guessing), appearance AND now `kind`
  (heading/caption misclassification) are corrected from the real text layer. Scanned
  pages: OCR-checked, text/image/both modes all work.
- **Raw image → Word** (`image_to_word.py`, new script this session) — `--mode text` /
  `image` / `both` all working, OCR-backed grounding/coverage checks in place.
- **Raw image → PDF** (`image_to_pdf.py`) — same OCR checks added, same default-model fix.
- **Word → PDF** (`word_to_pdf.py`) — unchanged, deterministic, no issues.
- **No known crashes remaining.** The `sorted()`/dict-comparison crash (triggered by
  `--scan-mode both` on a page with 2+ images) is fixed and verified in both `to_docx.py`
  and `to_xlsx.py`.
- **No known duplicate-block issue remaining.** `repeat_penalty` fixes it at the source;
  `drop_duplicate_blocks()` is a deterministic backstop regardless.

---

## The one open, unresolved problem

**Alignment (and block-`kind` classification) is unreliable for any page with no text
layer — scanned PDFs and raw images both — and there's no deterministic fix available yet.**

Confirmed pattern: the model has a learned bias toward guessing a short, bold,
heading-like line is `center`-aligned, even when it's genuinely left-aligned in the source.
Seen on two separate documents, through two separate scripts (`image_to_word.py` on
`demo2.png`, `test_pdf.py`'s scanned path on `demo_scanned_with_image.pdf`) — same
underlying code path (`measure_block_look()` is skipped whenever there's no text layer to
measure from), so this isn't fixable by "reusing whichever script does it right" — neither
does, for this specific problem.

A detailed prompt-engineering alternative (explicit XY-coordinate/bounding-box spatial
reasoning) was proposed by the project owner and **explicitly turned down** — reasoning is
in `memory.md` §8, short version: this codebase's own documented history already proved
prose instructions are advisory and get dropped by the model, the failure looks like a
learned statistical prior rather than a missing instruction, and even a "successful" version
of this idea would need a real schema change plus new, unverifiable-against-anything code.

**Three real options are on the table, none chosen yet:**
1. Use the 4B model for raw images (partial improvement, already measured: 3→6 blocks
   recovered on a hard test page, still incomplete).
2. Extend the OCR check to flag alignment disagreement too (makes it visible, doesn't fix it).
3. A dedicated document-layout-detection model, separate from the VLM (the real fix,
   unscoped, new dependency).

**Immediate next step:** get a decision from the project owner on which of the three (or
what combination) to pursue. Nothing should be built here without that decision — this is
a genuine architecture choice, not a bug fix.

---

## Numbers worth keeping straight (don't re-derive, don't assume stale)

- Canonical latency baseline (pre-session, text-only 1-page digital PDF): **154.10s**
  (68.72s prefill, 85.08s generate). This was BEFORE `repeat_penalty`, `drop_duplicate_blocks`,
  the `include_look` schema trim, and the kind-correction fix all landed — every one of
  those changes the generation-phase token count somewhat. **Not re-benchmarked fresh since
  all of them stacked together** — if latency comes up again, get a new number rather than
  quoting 154.10s as still-current.
- Per-block token cost of `align`/`size`/`bold`: measured at 17 tokens/block (server's own
  `/tokenize`), directly informing the `include_look` schema trim.
- Governor and iGPU are closed questions — do not re-test (see `memory.md` §7 for why).
