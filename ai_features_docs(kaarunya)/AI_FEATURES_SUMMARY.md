# iDocx AI Features — Implementation Summary

Branch: `ai-features` · Spec: IMPACTO-IDOCX-FSD-2026-010 "iDocx Enterprise — AI Features"
Scope: AI-04 Translation, AI-05 Smart Redaction, AI-06 One-Click Cleanup, AI-06 Accessibility
(the brief numbers Cleanup and Accessibility both "AI-06" — kept as-is, folder names
`fr_prd_104_cleanup` and `fr_prd_105_accessibility` disambiguate them)

## AI-04 — Translation
`features/fr_ai_04_translation/`

- Translates between 5 languages, any direction: English, Hindi, Tamil, Spanish, German
  (previously: English source only, Hindi/Spanish targets only).
- Auto-detects the source language from script where possible (Hindi/Tamil reliably,
  via Unicode block match); for English/Spanish/German (shared Latin script) it makes
  a best guess and flags it as detected rather than asserted.
- Refuses a same-source/same-target translation request instead of silently no-op'ing.
- Flags text that had to shrink to fit its original box, and separately flags text at
  high risk of overflow.
- A note is shown for Tamil output explaining the known copy/search limitation of the
  Indic script.

## AI-05 — Smart Redaction
`features/fr_ai_05_redaction/`

- Numeric confidence scale (0-100) replacing the old high/medium/low strings.
- Checksum-validated detectors (GSTIN, Aadhaar, card numbers) now keep a near-miss at
  low confidence instead of dropping it outright — e.g. a number that looks like a
  card but fails the Luhn check digit is still surfaced, just marked low-confidence.
- New: signature detection — both PDF signature form fields and hand-drawn ink near a
  "Signature" label (distinguished from a flat underline by stroke shape).
- New: `categories` filter (government_id / financial / contact / personal / secret /
  signature) so a caller can scope what's searched for.
- Each found item now reports its page coordinates (`rects`), not just a text match.
- **Fixed a real redaction bug**: vector art (e.g. ink strokes) under a black redaction
  box was previously left physically present in the PDF, only visually covered.
  Redaction now strips vector content inside the box, so it survives neither visual
  inspection nor text/data extraction.

## AI-06 — One-Click Cleanup
`features/fr_prd_104_cleanup/`

- Blank-page detection now reports a `blank_score` (ink coverage) alongside the
  pass/fail classification.
- New: "similar but not identical" page detection (e.g. a reused template, a near-copy)
  reported separately from true duplicates, so it's surfaced without being auto-removed.
- New: estimated file-size savings if all flagged pages/metadata were removed.
- New: metadata is categorised (author / device-or-software / GPS / other) and GPS/
  device EXIF data embedded in scanned images is detected and can be stripped by
  re-encoding the image, not just the document's XMP/Info metadata.
- **Fixed a real bug**: metadata strip/size-report logic called a `Document`-level
  image API that doesn't exist; corrected to operate per-page as PyMuPDF requires.

## AI-06 — Accessibility
`features/fr_prd_105_accessibility/`

- Existing checks (tags, language, title, fonts, image descriptions, real text vs.
  scan, reading order, bookmarks, permissions) kept, each independently testable.
- New checks: heading structure (flags a skipped level, e.g. H1 straight to H3 with no
  H2), table headers (are header cells actually tagged), colour contrast (WCAG AA,
  4.5:1 / 3:1 ratios, computed from sampled background colour vs. text colour).
- New read-only tool `suggest_structure`: proposes heading levels and a top-to-bottom
  reading order from visible text size/position for an untagged PDF — explicitly a
  suggestion with a stated basis, never a decision, and never written into the file.
- Fixed heading-detection heuristic to weight "what counts as body text" by character
  count rather than line count, so a table with many short cells can no longer outvote
  a document's actual paragraph text.

## Quality
- `ruff` clean on every file touched: all outstanding findings in these files are
  pre-existing (same lines as `HEAD`), nothing introduced by this work.
- SonarLint-style issues (cognitive complexity, nested ternaries, blind excepts, type
  narrowing) swept and fixed across all touched files.
- Each feature has a standalone check script (`tests/checks/<feature>.py`) plus
  `pytest` coverage, all passing, with fixtures/ground truth extended to cover every
  new field and behaviour above.
- AI-04 and AI-05 additionally verified live against the running service and the real
  model, not just the offline check scripts.

## Not in this branch
- Keep-alive/heartbeat on the chat stream for long-running tool calls (translation on
  a large file can run ~70s with the stream silent, which can trip a browser's fetch
  timeout) — diagnosed, not yet fixed, pending a decision on whether to take it on.
