# idox: what we built, the tweaks, and how it was tested (2026-10-19)

For teammates and reviewers who have not followed the work. It says what idox is, who built what, what changed in this
round of work, how each change was measured, and what is still open. The longer record of the first round is in
`work_log_2026-10-16.md`; how to run each conversion is in `new_conversions.md`.

## 1. What idox is

idox converts documents between formats. A local AI vision model reads each page and returns its content as blocks
(headings, paragraphs, lists, tables); writers turn the blocks into Word, PowerPoint, Excel, CSV, XML and so on. For
digital PDFs the PDF's own text is the truth: the model decides order and structure, and repair code corrects its words
and puts back what it dropped. **No OCR is used anywhere in the new code.**

## 2. Who built what

| Part | Built by |
|---|---|
| The core pipeline: model reading (`blocks.py`), PDF pipeline (`test_pdf.py`), Word, PowerPoint and Excel writers | Sumukh, from September 2026 |
| Older conversions (Image to Excel, Word, PDF; PDF to Word, Excel, PowerPoint), the API, the web app, `pdf_look.py` (copies the PDF's fonts and spacing into Word), the 59 regression tests, the `idocx_ai` chat feature | Sumukh and Prakyath |
| **The ten new conversions:** Image to PowerPoint, XML, CSV; PDF to XML, CSV; Word to Excel, XML, PowerPoint, CSV, Image | this work |
| **New code:** `repair.py` (restore lost text, rows and order), `image_tiles.py` (reading a picture in strips, the title rule), `image_look.py` (table look from pictures), `paths.py`, `csv_plan.py`, `ppt_review.py` | this work |
| **Table look** (alignment, header colour, widths) from PDFs and ruled pictures into Word, PowerPoint, Excel; **the reorganization** into `input/`, `output/`, `python_scripts/`; the 4B-on-GPU setup and prompt work; about 100 more tests | this work |

Result: 19 of 43 cells of the 7x7 format matrix are built (all conversions from Image, PDF and Word). Excel, XML,
PowerPoint and CSV as sources belong to another team member.

## 3. The tweaks of this round

Each was first tried in a separate copy of the project, measured against the unchanged code, and only then applied.

| # | Problem | Change | Measured result |
|---|---|---|---|
| 1 | **A paragraph the PDF really repeats was shortened.** The guard that stops the model repeating itself dropped genuine copies, and the repair step only looked for lines missing entirely, not for missing copies. | The guard now receives the PDF's own text and keeps as many copies as the PDF has. When the output has fewer copies than the PDF, the repair step looks for every missing line (`test_pdf.py`). | 10-page test PDF: all words present, reading order 100 (was 87), size ratio 1.00 (was 0.77), 60 of 60 repeated copies (was 46). |
| 2 | **A whole page was lost on US-Letter PDFs.** A Letter page rendered at 125 dpi is 1375 px tall. Anything under 1400 px was treated as a "short picture" and read with the plain prompt, and the 4B model ran past its token limit on page 2 of the dense 3-page PDF. | The short-picture limit is 1200 px (`blocks.py`, one number). A4 pages (1462 px) were never affected. | Dense 3-page PDF: all pages present, recall and precision 100%, order 100, size 1.00 (before: page 2 lost, size 0.61, order 76). The small sample pictures (815 and 882 px) are unchanged. |
| 3 | **PowerPoint tables had PowerPoint's blue banding** instead of the PDF's plain rows. | A table with a measured look gets plain rows, thin borders and the PDF's header colour (`to_pptx.py`). Tables without a measured look keep the default style. | Compared side by side with the PDF: grey header, centred numbers, plain white rows. |
| 4 | **A title written inside the first paragraph** ("Title", line break, sentence) came out as one left-aligned paragraph. | The title is split into a heading and a paragraph, and centred when its line is centred in the picture. Pixels only, no OCR, and the extracted text does not change (`image_tiles.py`). | 8 sample pictures: text identical in all, the title became a heading in the 2 pictures where it was merged. Picture to Word and PDF now show a centred blue title; the PowerPoint slide title is the real title. |
| 5 | **The tests failed on a fresh copy** of the repository, because 22 of them look for sample files that are no longer versioned. | Those tests skip, with a note, when `input/` and `output/` are not present (2 test files, 2 lines each). | Fresh copy: 0 failures, 23 skipped. Your machine: unchanged. |

**Tried and not adopted**
- *A prompt that asks for titles.* It lost the title on one picture and cut a section off another. Prompt wording is fragile
  at this model size, which is why the title is fixed by a rule and not by the prompt.
- *A higher image budget for pictures (1024 tokens instead of 512).* One dense picture went from 92% to 100% of its
  phrases, another lost 37% of its words (63% kept); with the page prompt as well, a third lost its title (90% to 80%).
  No safe default, so it stays at 512.
- *A bigger model (35B).* Reads slightly better (99% against 97% on a dense page) but takes about 4 minutes a page on this
  laptop, so it is not practical here.

## 4. How it was tested

| Test | Result |
|---|---|
| Unit tests, no model | 155 pass, 19 skipped (they need the model). One test was added for the title rule. |
| Smoke test, every conversion once with the 4B model on the GPU (41 checks, run before these tweaks) | 41 of 41 pass: content of every output, table colours in Word, PowerPoint and Excel, page counts, no repeated rows, all columns kept, a 10-page document. |
| The tweaks, in a separate copy, with the model | The results in section 3. |

What the test of the final code did **not** include: after the tweaks were applied to the real project, only the unit tests
were run again. The model-based checks of the tweaks were done in the separate copy, with the same code.

## 5. What is still open

| Item | State |
|---|---|
| Picture accuracy | Small pictures reach 83-92% of the expected phrases. The remaining misses are model reading errors, for example the heading above a table, or a misread word. |
| Faint or borderless tables in pictures | Keep the default look. A test of a rule that finds the columns from gaps in the text got only 2 of 5 data columns right, so it was not built. |
| Nested tables | The content is kept but flattened into the outer table. |
| Broken PDF characters | Where the PDF's text layer stores a symbol as a replacement character, it cannot be recovered. |
| Word files over 10 pages | The long test used a Word file generated from a PDF. |
| API and web app | The ten new conversions are command-line only. The API and web app were not run with the model. |
| Housekeeping | `implementation_summary.pdf` is out of date; some loose files at the project root are not organised. |

## 6. Repository and git

- `python_scripts/` holds every script. `input/` holds the files to convert (`input/<name>_input/`), `output/` what the
  conversions write (`output/<name>_output/`). **`input/` and `output/` are not in git**: they stay on each machine. On a
  fresh clone, create `input/<name>_input/` and put your files there.
- Branch `testing2`, on top of `3d16c29` (the last commit the remote has): `6116019` stop tracking sample data,
  `41c6a11` reorganization, fixes and table look, `7828379` docs, `bd102d6` the tweaks of this document.
- The branch is on the remote as `origin/testing2`; `dev` was never changed. The reorganization moves almost every script,
  so anyone working on the old layout will hit conflicts when merging.

## 7. Try it

Run each command on its own from the project folder in PowerShell. Start the model first (command in
`work_log_2026-10-16.md`, section 4) for anything except the unit tests.

```powershell
.\.venv\Scripts\python -m unittest discover -s tests
```
```powershell
.\.venv\Scripts\python python_scripts\test_pdf.py input\pdf_to_tiff_input\sample10.pdf --docx output\my_test\x.docx --out output\my_test\x.md --base-url http://127.0.0.1:8090
```
```powershell
.\.venv\Scripts\python python_scripts\image_to_word.py input\image_to_csv_input\fol.png --outdir output\my_test --base-url http://127.0.0.1:8090
```

The first checks the code without the model. The second is the 10-page PDF with repeated paragraphs (all text, correct
order). The third shows the title rule: a centred blue title above the first sentence.
