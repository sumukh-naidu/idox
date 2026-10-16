# The ten newer conversions

Image, PDF and Word to the formats that were missing. Every one reads the document with the local **Qwen3-VL-2B**
model, except Word to Image, which is pure rendering (nothing to read).

| Command | From -> To | Folders |
|---|---|---|
| `image_to_ppt.py` | image -> PowerPoint | `input/image_to_ppt_input/` -> `output/image_to_ppt_output/` |
| `image_to_xml.py` | image -> XML | `input/image_to_xml_input/` -> `output/image_to_xml_output/` |
| `image_to_csv.py` | image -> CSV (text lines + tables) | `input/image_to_csv_input/` -> `output/image_to_csv_output/` |
| `pdf_to_xml.py` | PDF -> XML | `input/pdf_to_xml_input/` -> `output/pdf_to_xml_output/` |
| `pdf_to_csv.py` | PDF -> CSV (text lines + tables) | `input/pdf_to_csv_input/` -> `output/pdf_to_csv_output/` |
| `word_to_image.py` | Word -> PNG per page (no model) | `input/word_to_image_input/` -> `output/word_to_image_output/` |
| `word_to_xlsx.py` | Word -> Excel | `input/word_to_xlsx_input/` -> `output/word_to_xlsx_output/` |
| `word_to_xml.py` | Word -> XML | `input/word_to_xml_input/` -> `output/word_to_xml_output/` |
| `word_to_ppt.py` | Word -> PowerPoint | `input/word_to_ppt_input/` -> `output/word_to_ppt_output/` |
| `word_to_csv.py` | Word -> CSV (text lines + tables) | `input/word_to_csv_input/` -> `output/word_to_csv_output/` |

Each input folder already holds sample files. Drop your own next to them.

## Where things are

```
python_scripts\   every Python script (run them from anywhere: they find the folders below by themselves)
input\            the files to convert, one folder per conversion: input\<name>_input\
output\           what the conversions write, one folder per conversion: output\<name>_output\
tests\  api\  docs\  models_manual\
```

`input\dense_test_input\` holds the 3-page dense test document (the PDF, its three page pictures and a Word version).
The older test and experiment folders (`sample_local_*_test`, `benchmark_win`, `models35`) are kept outside the project, in
`D:\idox_test_records\`. They are a record of test runs, not part of the product.

## 1. One-time setup (already done on this laptop)

- Python 3.12 virtualenv: `py -3.12 -m venv .venv` then `.venv\Scripts\python -m pip install -r requirements.txt`
- Model files in `models_manual\` (Qwen3VL-2B-Instruct-Q4_K_M.gguf and mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf)
- llama.cpp b11247 in `%USERPROFILE%\.local\opt\llama.cpp\llama-b11247\`
- **Word conversions only:** LibreOffice (draws the pages). `winget install TheDocumentFoundation.LibreOffice` from
  an **administrator** terminal.
- **No OCR.** Everything is read by the AI model; nothing in these conversions uses OCR or needs Tesseract.

## 2. Start the model server (leave this window open)

PowerShell, from the repo folder:

```powershell
& "$env:USERPROFILE\.local\opt\llama.cpp\llama-b11247\llama-server.exe" `
  --model models_manual\Qwen3VL-2B-Instruct-Q4_K_M.gguf `
  --mmproj models_manual\mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf `
  --port 8090 --host 127.0.0.1 --no-webui --ctx-size 8192 `
  --image-min-tokens 512 --image-max-tokens 512 --parallel 1
```

Check it: `Invoke-RestMethod http://127.0.0.1:8090/health` prints `status ok`.

## 3. Run one conversion

Open a second terminal in the repo folder.

```powershell
.\.venv\Scripts\python python_scripts\image_to_csv.py                               # every image in input\image_to_csv_input\
.\.venv\Scripts\python python_scripts\image_to_csv.py input\image_to_csv_input\demo2.png  # one file
.\.venv\Scripts\python python_scripts\pdf_to_xml.py input\pdf_to_xml_input\sample.pdf
.\.venv\Scripts\python python_scripts\word_to_xlsx.py input\word_to_xlsx_input\price_list.docx
```

Output lands in the matching `*_output\` folder. Timing is about **30 to 75 s per page** on this CPU.
PDF and Word runs also write `<name>.md` (the model's clean extraction) and, for Word, `<name>.pdf` (the pages the
model actually saw).

Options on every script: `--outdir DIR`; PDF and Word: `--pages 1-3`; model scripts: `--base-url URL`
(default `http://127.0.0.1:8090`).

## 4. Test them

```powershell
.\.venv\Scripts\python python_scripts\test_new_conversions.py --list                        # what each case checks
.\.venv\Scripts\python python_scripts\test_new_conversions.py                               # all ten, one sample each (~10-15 min)
.\.venv\Scripts\python python_scripts\test_new_conversions.py --only image_to_csv,pdf_to_xml
.\.venv\Scripts\python python_scripts\test_new_conversions.py --all-samples                 # every sample file (slow)
.\.venv\Scripts\python python_scripts\test_new_conversions.py --only pdf_to_csv --verbose   # show the script's own output
```

Each case deletes its old output, runs the conversion, then **opens the written file again** and checks that it
parses, has the right shape (CSV columns and rows, XML `<page>`s, PowerPoint slides) and contains the words it must.
For PDF and Word the "must" is the source document's own text (at least 90 % of its words of 4+ letters).
`image_to_csv_text` checks that a picture with **no table** still gives a CSV of its text lines.
A case says SKIP, never PASS, when LibreOffice is missing. The exit code is 1 if any case FAILED.

Writer-only tests (no model, run in seconds):

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -p "test_new_writers.py" -v
```

## 5. What each output contains

- **CSV**: everything on the page, in reading order. Each heading or paragraph is **one row on one line** (line
  breaks become spaces); each list item is one row, bullet removed; a table is its header row then one row per
  data row, one cell per column. A blank row sits before and after every table. Rows with every cell empty are
  dropped. UTF-8 with a byte-order mark so Excel opens it correctly. A page with no table is fine; a page with
  nothing at all writes no file.
- **XML**: `<document><page number=".."><heading/><paragraph/><list><item/></list><caption/><table><header><cell/>
  </header><row><cell/></row></table></page></document>`. Always well-formed; control characters are dropped.
- **PowerPoint**: one slide per page, the first heading becomes the slide title (same writer as PDF to PowerPoint).
- **Excel (from Word)**: same writer as PDF to Excel: tables as grids, prose in column A.

## 6. Not wired in yet

These are command-line only. They are **not** registered as routes in `api/idox_api.py` or `idox_app.py`, so the
web app and HTTP API do not offer them. Add them there once the results are accepted.
