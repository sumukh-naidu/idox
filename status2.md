# idox — Status 2

Snapshot of the project *right now*. For full background, every bug's root cause, and the
complete architecture, see `memory.md` — this file only covers current state and the
immediate open decisions.

**Last updated:** 2026-09-25 (later revision — several fixes landed since this file was
first written)
**Branch:** `feature/testing_2b_Q8mmproj`. **Uncommitted right now:** `blocks.py`, holding
the resolution-upscaling fix (`memory.md` §11) — not yet committed.
**Model in use:** raw `llama-server` on `http://127.0.0.1:8090` (Qwen3-VL-2B-Instruct,
Q4_K_M + Q8_0 mmproj). Still the automatic default everywhere — no `--base-url` needed.

---

## What's working, right now, verified

- **PDF → Word/Excel** (`test_pdf.py`) — digital pages: trimmed extraction, appearance +
  `kind` correction from the real text layer, tables now have real spacing (6pt) instead of
  sitting flush against text, bullet-character mismatches no longer trigger false
  "duplicate" repairs. Scanned pages: OCR-checked, text/image/both modes all work.
- **Raw image → Word/PDF** (`image_to_word.py`, `image_to_pdf.py`) — OCR-backed checks in
  place, AND (new) low-resolution uploads now get upscaled to the same detail budget a
  PDF page gets, closing a real, measured ~31% visual-detail gap that was making raw images
  more error-prone than PDFs for reasons that had nothing to do with the model itself.
- **No known crashes remaining.**
- **Portability is set up**: `requirements.txt` (Python deps, pinned) in the repo, and a
  separate migration-notes doc at `/home/aiteam/Documents/idox-migration-notes.md` (outside
  the project, deliberately) covering everything that does NOT travel with `git clone` —
  the model weights, `llama-server`, Tesseract, LibreOffice.

---

## Two open problems, not one — don't conflate them

**1. Alignment — unchanged, still no fix chosen.** Model-guessed alignment (raw images,
scanned PDFs) has a confirmed bias toward guessing "center" for heading-like text even when
it's genuinely left-aligned. Three real options on the table (bigger model / OCR-alignment-
flagging / dedicated layout-detection model), none chosen. **The resolution fix does
nothing for this** — confirmed explicitly before it was built, different root cause
entirely (see `memory.md` §8, §11).

**2. Content sometimes still missing on raw images — partially improved, not solved.**
Root cause turned out to be TWO separate things, not one:
  - **Low image resolution** (fixed, verified) — raw images were getting meaningfully less
    visual detail than PDF pages because nothing controlled their resolution. Now upscaled
    to match. Real, measured fix.
  - **Genuine run-to-run model inconsistency** (still open, not fixable by a code patch) —
    even after the resolution fix, repeated tests on the same image show *different*
    content dropped on different calls (not the same gap every time). Traced to a real
    mechanism: floating-point non-associativity in multi-threaded CPU inference plus
    KV-cache reuse means temperature=0 isn't perfectly bitwise-reproducible — a near-tie
    probability decision can flip between calls. Most exposed on visually ambiguous content.
  - **Proposed, not built:** auto-retry when OCR coverage scores low. Directly motivated by
    the mechanism above — a fresh retry has real expected value here, unlike retrying a
    deterministic bug. This is the next concrete thing to build if "full extraction"
    reliability is the priority.

---

## Numbers worth keeping straight (don't re-derive, don't assume stale)

- Canonical latency baseline (154.10s) is from BEFORE most of this session's fixes and is
  explicitly flagged stale in `memory.md` — don't quote it as current.
- Resolution fix: raw image detail budget raised from ~1,088 to ~1,434 vision tokens on a
  real test file, matching test_pdf.py's own 125dpi PDF-page baseline (~1,427).
- Governor and iGPU are closed questions — do not re-test (see `memory.md` §7).
