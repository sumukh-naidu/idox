# idox benchmark report

## Machine and settings

- CPU: 11th Gen Intel(R) Core(TM) i7-1165G7 @ 2.80GHz (8 logical CPUs; 4 cores x 2 threads; max 4700.0000 MHz), governor `powersave`
- RAM: 14622 MB total, 6072 MB available at start
- OS: Linux 7.0.0-34-generic; Python 3.13.15
- Server: version: 0.5.0-dev (build 11247, commit 0bc845d35) | built with GNU 11.4.0 for Linux x86_64
- Server flags: `/home/sumukh/.local/opt/llama.cpp/llama-b11247/llama-server --model Qwen3VL-2B-Instruct-Q4_K_M.gguf --mmproj mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf --port 8090 --host 127.0.0.1 --no-webui --ctx-size 8192 --image-min-tokens 512 --image-max-tokens 512 --parallel 1`
- Model files: Qwen3VL-2B-Instruct-Q4_K_M.gguf 1107409952 bytes sha256 089d75c52f4b7ffc; mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf 445053216 bytes sha256 f9a68fabba69c3b8
- Inputs: text_only_1page.pdf sha256 8178d16b5ad44fbe; sample.pdf sha256 e4564b2c1f656c8b; hi.png sha256 f26fed4eb5fd5a49; sample_125dpi.png sha256 9297a62569eb7f75
- Tesseract: tesseract 5.5.0; LibreOffice: LibreOffice 26.2.5.2 620(Build:2)
- At start: load 0.49, package 61 C, throttle events 4

## During the run

- 90 runs (42 with the model). Package temperature after each run: 64-73 C. Thermal throttle events: 4 after the first run, 50 after the last.
- Load average (1 min) just before model runs: 0.49-5.48. This includes the run that just finished and the server starting up, so it does not show what other programs were doing.
- Runs that hit the 4,000-token output cap: 0. Runs that produced no file: 3 (the three `hi.png` to Excel runs are expected: that image has no table).

## Latency, every run

Wall = the whole script, seconds. Prefill / gen from the server's log (`cached` = image served from the prompt cache, so prefill is not a real first read).

| input | conversion | model | run | state | wall s | prefill s | prefill tok | gen s | gen tok | tok/s | notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| text_only_1page.pdf | pdf_docx | yes | 1 | ok | 77.5 | 17.0 | 1133 | 59.7 | 693 | 11.6 | cold server;  |
| text_only_1page.pdf | pdf_docx | yes | 2 | ok | 54.5 | 0.1 | 1 | 53.7 | 637 | 11.9 | image cached;  |
| text_only_1page.pdf | pdf_docx | yes | 3 | ok | 56.0 | 0.1 | 1 | 55.1 | 637 | 11.6 | image cached;  |
| text_only_1page.pdf | pdf_xlsx | yes | 1 | ok | 79.8 | 18.5 | 1133 | 60.6 | 693 | 11.4 | cold server;  |
| text_only_1page.pdf | pdf_xlsx | yes | 2 | ok | 56.5 | 0.1 | 1 | 55.7 | 637 | 11.4 | image cached;  |
| text_only_1page.pdf | pdf_xlsx | yes | 3 | ok | 55.4 | 0.1 | 1 | 54.6 | 637 | 11.7 | image cached;  |
| text_only_1page.pdf | pdf_pptx | yes | 1 | ok | 78.3 | 18.2 | 1133 | 59.4 | 693 | 11.7 | cold server;  |
| text_only_1page.pdf | pdf_pptx | yes | 2 | ok | 55.2 | 0.1 | 1 | 54.4 | 637 | 11.7 | image cached;  |
| text_only_1page.pdf | pdf_pptx | yes | 3 | ok | 55.4 | 0.1 | 1 | 54.5 | 637 | 11.7 | image cached;  |
| text_only_1page.pdf | pdf_txt | yes | 1 | ok | 78.3 | 18.2 | 1133 | 59.7 | 693 | 11.6 | cold server;  |
| text_only_1page.pdf | pdf_txt | yes | 2 | ok | 56.6 | 0.1 | 1 | 55.9 | 637 | 11.4 | image cached;  |
| text_only_1page.pdf | pdf_txt | yes | 3 | ok | 57.6 | 0.1 | 1 | 57.0 | 637 | 11.2 | image cached;  |
| text_only_1page.pdf | pdf_jpg | no | 1 | ok | 1.3 | - | - | - | - | - |  |
| text_only_1page.pdf | pdf_jpg | no | 2 | ok | 1.2 | - | - | - | - | - |  |
| text_only_1page.pdf | pdf_jpg | no | 3 | ok | 1.3 | - | - | - | - | - |  |
| text_only_1page.pdf | pdf_jpeg | no | 1 | ok | 1.2 | - | - | - | - | - |  |
| text_only_1page.pdf | pdf_jpeg | no | 2 | ok | 1.3 | - | - | - | - | - |  |
| text_only_1page.pdf | pdf_jpeg | no | 3 | ok | 1.3 | - | - | - | - | - |  |
| text_only_1page.pdf | pdf_tiff | no | 1 | ok | 1.9 | - | - | - | - | - |  |
| text_only_1page.pdf | pdf_tiff | no | 2 | ok | 1.9 | - | - | - | - | - |  |
| text_only_1page.pdf | pdf_tiff | no | 3 | ok | 1.9 | - | - | - | - | - |  |
| sample.pdf | pdf_docx | yes | 1 | ok | 44.0 | 18.4 | 1133 | 25.0 | 300 | 12.0 | cold server;  |
| sample.pdf | pdf_docx | yes | 2 | ok | 27.5 | 0.1 | 1 | 26.8 | 316 | 11.8 | image cached;  |
| sample.pdf | pdf_docx | yes | 3 | ok | 27.5 | 0.1 | 1 | 26.7 | 316 | 11.8 | image cached;  |
| sample.pdf | pdf_xlsx | yes | 1 | ok | 44.3 | 18.4 | 1133 | 25.4 | 300 | 11.8 | cold server;  |
| sample.pdf | pdf_xlsx | yes | 2 | ok | 29.4 | 0.1 | 1 | 28.7 | 316 | 11.0 | image cached;  |
| sample.pdf | pdf_xlsx | yes | 3 | ok | 29.8 | 0.1 | 1 | 29.1 | 316 | 10.9 | image cached;  |
| sample.pdf | pdf_pptx | yes | 1 | ok | 46.4 | 20.1 | 1133 | 25.7 | 300 | 11.7 | cold server;  |
| sample.pdf | pdf_pptx | yes | 2 | ok | 27.8 | 0.1 | 1 | 27.0 | 316 | 11.7 | image cached;  |
| sample.pdf | pdf_pptx | yes | 3 | ok | 27.5 | 0.1 | 1 | 26.8 | 316 | 11.8 | image cached;  |
| sample.pdf | pdf_txt | yes | 1 | ok | 45.8 | 19.1 | 1133 | 26.2 | 300 | 11.5 | cold server;  |
| sample.pdf | pdf_txt | yes | 2 | ok | 29.4 | 0.1 | 1 | 28.8 | 316 | 11.0 | image cached;  |
| sample.pdf | pdf_txt | yes | 3 | ok | 27.8 | 0.1 | 1 | 27.2 | 316 | 11.6 | image cached;  |
| sample.pdf | pdf_jpg | no | 1 | ok | 0.7 | - | - | - | - | - |  |
| sample.pdf | pdf_jpg | no | 2 | ok | 0.7 | - | - | - | - | - |  |
| sample.pdf | pdf_jpg | no | 3 | ok | 0.7 | - | - | - | - | - |  |
| sample.pdf | pdf_jpeg | no | 1 | ok | 0.7 | - | - | - | - | - |  |
| sample.pdf | pdf_jpeg | no | 2 | ok | 0.7 | - | - | - | - | - |  |
| sample.pdf | pdf_jpeg | no | 3 | ok | 0.7 | - | - | - | - | - |  |
| sample.pdf | pdf_tiff | no | 1 | ok | 1.3 | - | - | - | - | - |  |
| sample.pdf | pdf_tiff | no | 2 | ok | 1.3 | - | - | - | - | - |  |
| sample.pdf | pdf_tiff | no | 3 | ok | 1.3 | - | - | - | - | - |  |
| hi.png | img_docx | yes | 1 | ok | 31.4 | 19.7 | 1170 | 11.3 | 131 | 11.6 | cold server;  |
| hi.png | img_docx | yes | 2 | ok | 13.1 | 0.1 | 1 | 12.4 | 131 | 10.5 | image cached;  |
| hi.png | img_docx | yes | 3 | ok | 11.7 | 0.1 | 1 | 11.1 | 131 | 11.8 | image cached;  |
| hi.png | img_pdf | yes | 1 | ok | 31.3 | 19.7 | 1170 | 10.7 | 131 | 12.3 | cold server;  |
| hi.png | img_pdf | yes | 2 | ok | 12.2 | 0.1 | 1 | 11.1 | 131 | 11.8 | image cached;  |
| hi.png | img_pdf | yes | 3 | ok | 12.0 | 0.1 | 1 | 11.0 | 131 | 11.9 | image cached;  |
| hi.png | img_xlsx | yes | 1 | failed | 30.8 | 19.8 | 1170 | 10.7 | 131 | 12.3 | cold server; No table was found in the image. |
| hi.png | img_xlsx | yes | 2 | failed | 11.6 | 0.1 | 1 | 11.1 | 131 | 11.8 | image cached; No table was found in the image. |
| hi.png | img_xlsx | yes | 3 | failed | 11.8 | 0.2 | 1 | 11.2 | 131 | 11.7 | image cached; No table was found in the image. |
| hi.png | img_docx_pic | no | 1 | ok | 0.3 | - | - | - | - | - |  |
| hi.png | img_docx_pic | no | 2 | ok | 0.3 | - | - | - | - | - |  |
| hi.png | img_docx_pic | no | 3 | ok | 0.3 | - | - | - | - | - |  |
| sample_125dpi.png | img_docx | yes | 1 | ok | 57.1 | 18.7 | 1133 | 37.8 | 420 | 11.1 | cold server;  |
| sample_125dpi.png | img_docx | yes | 2 | ok | 41.2 | 0.1 | 1 | 40.4 | 436 | 10.8 | image cached;  |
| sample_125dpi.png | img_docx | yes | 3 | ok | 41.6 | 0.1 | 1 | 40.8 | 436 | 10.7 | image cached;  |
| sample_125dpi.png | img_pdf | yes | 1 | ok | 57.0 | 19.8 | 1133 | 36.1 | 420 | 11.6 | cold server;  |
| sample_125dpi.png | img_pdf | yes | 2 | ok | 38.9 | 0.1 | 1 | 37.7 | 436 | 11.6 | image cached;  |
| sample_125dpi.png | img_pdf | yes | 3 | ok | 38.8 | 0.1 | 1 | 37.5 | 436 | 11.6 | image cached;  |
| sample_125dpi.png | img_xlsx | yes | 1 | ok | 56.6 | 18.5 | 1133 | 37.4 | 420 | 11.2 | cold server;  |
| sample_125dpi.png | img_xlsx | yes | 2 | ok | 42.2 | 0.1 | 1 | 41.4 | 436 | 10.5 | image cached;  |
| sample_125dpi.png | img_xlsx | yes | 3 | ok | 38.6 | 0.1 | 1 | 37.8 | 436 | 11.5 | image cached;  |
| sample_125dpi.png | img_docx_pic | no | 1 | ok | 0.3 | - | - | - | - | - |  |
| sample_125dpi.png | img_docx_pic | no | 2 | ok | 0.3 | - | - | - | - | - |  |
| sample_125dpi.png | img_docx_pic | no | 3 | ok | 0.3 | - | - | - | - | - |  |
| text_only_1page.pdf | docx_pdf@pdf_docx | no | 1 | check | 0.8 | - | - | - | - | - |  |
| text_only_1page.pdf | docx_pdf@pdf_docx | no | 2 | check | 0.7 | - | - | - | - | - |  |
| text_only_1page.pdf | docx_pdf@pdf_docx | no | 3 | check | 0.6 | - | - | - | - | - |  |
| text_only_1page.pdf | tiff_pdf@pdf_tiff | no | 1 | ok | 2.2 | - | - | - | - | - |  |
| text_only_1page.pdf | tiff_pdf@pdf_tiff | no | 2 | ok | 2.4 | - | - | - | - | - |  |
| text_only_1page.pdf | tiff_pdf@pdf_tiff | no | 3 | ok | 2.3 | - | - | - | - | - |  |
| text_only_1page.pdf | tiff_pdf_pic@pdf_tiff | no | 1 | ok | 0.9 | - | - | - | - | - |  |
| text_only_1page.pdf | tiff_pdf_pic@pdf_tiff | no | 2 | ok | 0.9 | - | - | - | - | - |  |
| text_only_1page.pdf | tiff_pdf_pic@pdf_tiff | no | 3 | ok | 1.0 | - | - | - | - | - |  |
| sample.pdf | docx_pdf@pdf_docx | no | 1 | ok | 0.7 | - | - | - | - | - |  |
| sample.pdf | docx_pdf@pdf_docx | no | 2 | ok | 0.7 | - | - | - | - | - |  |
| sample.pdf | docx_pdf@pdf_docx | no | 3 | ok | 0.6 | - | - | - | - | - |  |
| sample.pdf | tiff_pdf@pdf_tiff | no | 1 | ok | 1.6 | - | - | - | - | - |  |
| sample.pdf | tiff_pdf@pdf_tiff | no | 2 | ok | 1.5 | - | - | - | - | - |  |
| sample.pdf | tiff_pdf@pdf_tiff | no | 3 | ok | 1.5 | - | - | - | - | - |  |
| sample.pdf | tiff_pdf_pic@pdf_tiff | no | 1 | ok | 0.8 | - | - | - | - | - |  |
| sample.pdf | tiff_pdf_pic@pdf_tiff | no | 2 | ok | 0.8 | - | - | - | - | - |  |
| sample.pdf | tiff_pdf_pic@pdf_tiff | no | 3 | ok | 0.9 | - | - | - | - | - |  |
| hi.png | docx_pdf@img_docx | no | 1 | ok | 0.7 | - | - | - | - | - |  |
| hi.png | docx_pdf@img_docx | no | 2 | ok | 0.6 | - | - | - | - | - |  |
| hi.png | docx_pdf@img_docx | no | 3 | ok | 0.7 | - | - | - | - | - |  |
| sample_125dpi.png | docx_pdf@img_docx | no | 1 | ok | 0.7 | - | - | - | - | - |  |
| sample_125dpi.png | docx_pdf@img_docx | no | 2 | ok | 0.7 | - | - | - | - | - |  |
| sample_125dpi.png | docx_pdf@img_docx | no | 3 | ok | 0.7 | - | - | - | - | - |  |

## Latency summary (per conversion)

Cold = run 1 right after a server restart. Warm = mean of runs 2 and 3 (usually the image is cached).

| input | conversion | model | cold wall s | warm wall s | cold prefill s | gen s (mean) | gen tok (mean) | tok/s (mean) | runs ok |
|---|---|---|---|---|---|---|---|---|---|
| text_only_1page.pdf | pdf_docx | yes | 77.5 | 55.3 | 17.0 | 56.2 | 656 | 11.7 | 3/3 |
| text_only_1page.pdf | pdf_xlsx | yes | 79.8 | 56.0 | 18.5 | 57.0 | 656 | 11.5 | 3/3 |
| text_only_1page.pdf | pdf_pptx | yes | 78.3 | 55.3 | 18.2 | 56.1 | 656 | 11.7 | 3/3 |
| text_only_1page.pdf | pdf_txt | yes | 78.3 | 57.1 | 18.2 | 57.5 | 656 | 11.4 | 3/3 |
| text_only_1page.pdf | pdf_jpg | no | 1.3 | 1.2 | - | - | - | - | 3/3 |
| text_only_1page.pdf | pdf_jpeg | no | 1.2 | 1.3 | - | - | - | - | 3/3 |
| text_only_1page.pdf | pdf_tiff | no | 1.9 | 1.9 | - | - | - | - | 3/3 |
| sample.pdf | pdf_docx | yes | 44.0 | 27.5 | 18.4 | 26.2 | 311 | 11.9 | 3/3 |
| sample.pdf | pdf_xlsx | yes | 44.3 | 29.6 | 18.4 | 27.7 | 311 | 11.2 | 3/3 |
| sample.pdf | pdf_pptx | yes | 46.4 | 27.6 | 20.1 | 26.5 | 311 | 11.7 | 3/3 |
| sample.pdf | pdf_txt | yes | 45.8 | 28.6 | 19.1 | 27.4 | 311 | 11.4 | 3/3 |
| sample.pdf | pdf_jpg | no | 0.7 | 0.7 | - | - | - | - | 3/3 |
| sample.pdf | pdf_jpeg | no | 0.7 | 0.7 | - | - | - | - | 3/3 |
| sample.pdf | pdf_tiff | no | 1.3 | 1.3 | - | - | - | - | 3/3 |
| hi.png | img_docx | yes | 31.4 | 12.4 | 19.7 | 11.6 | 131 | 11.3 | 3/3 |
| hi.png | img_pdf | yes | 31.3 | 12.1 | 19.7 | 10.9 | 131 | 12.0 | 3/3 |
| hi.png | img_xlsx | yes | 30.8 | 11.7 | 19.8 | 11.0 | 131 | 11.9 | 0/3 |
| hi.png | img_docx_pic | no | 0.3 | 0.3 | - | - | - | - | 3/3 |
| sample_125dpi.png | img_docx | yes | 57.1 | 41.4 | 18.7 | 39.6 | 431 | 10.9 | 3/3 |
| sample_125dpi.png | img_pdf | yes | 57.0 | 38.8 | 19.8 | 37.1 | 431 | 11.6 | 3/3 |
| sample_125dpi.png | img_xlsx | yes | 56.6 | 40.4 | 18.5 | 38.9 | 431 | 11.1 | 3/3 |
| sample_125dpi.png | img_docx_pic | no | 0.3 | 0.3 | - | - | - | - | 3/3 |
| text_only_1page.pdf | docx_pdf@pdf_docx | no | 0.8 | 0.7 | - | - | - | - | 3/3 |
| text_only_1page.pdf | tiff_pdf@pdf_tiff | no | 2.2 | 2.3 | - | - | - | - | 3/3 |
| text_only_1page.pdf | tiff_pdf_pic@pdf_tiff | no | 0.9 | 0.9 | - | - | - | - | 3/3 |
| sample.pdf | docx_pdf@pdf_docx | no | 0.7 | 0.6 | - | - | - | - | 3/3 |
| sample.pdf | tiff_pdf@pdf_tiff | no | 1.6 | 1.5 | - | - | - | - | 3/3 |
| sample.pdf | tiff_pdf_pic@pdf_tiff | no | 0.8 | 0.9 | - | - | - | - | 3/3 |
| hi.png | docx_pdf@img_docx | no | 0.7 | 0.7 | - | - | - | - | 3/3 |
| sample_125dpi.png | docx_pdf@img_docx | no | 0.7 | 0.7 | - | - | - | - | 3/3 |

## Accuracy (per conversion, mean over runs; min in brackets for the headline)

Headline = word-sequence similarity vs the ground truth (see benchmark.py). Recall/precision use words longer than 3 characters.

| input | conversion | sequence % (min) | recall | precision | figures exact | table match | bullets found/expected | other |
|---|---|---|---|---|---|---|---|---|
| hi.png | docx_pdf@img_docx | 100.0 (100.0) | 100.0% | 100.0% | 3/3 | n/a | -/0 | Word words kept in PDF 100% |
| hi.png | img_docx | 100.0 (100.0) | 100.0% | 100.0% | 3/3 | n/a | 0/0 |  |
| hi.png | img_docx_pic | - | - | - | - | - | - | picture_identical True |
| hi.png | img_pdf | 100.0 (100.0) | 100.0% | 100.0% | 3/3 | n/a | -/0 |  |
| hi.png | img_xlsx | - | - | - | - | - | - | note no output file |
| sample.pdf | docx_pdf@pdf_docx | 100.0 (100.0) | 100.0% | 100.0% | 3/3 | 100% | -/3 | Word words kept in PDF 100% |
| sample.pdf | pdf_docx | 100.0 (100.0) | 100.0% | 100.0% | 3/3 | 100% | 0/3 |  |
| sample.pdf | pdf_jpeg | - | - | - | - | - | - | pages 1; psnr_db 51.47; size_ok True; ocr_recall 0.95 |
| sample.pdf | pdf_jpg | - | - | - | - | - | - | pages 1; psnr_db 51.47; size_ok True; ocr_recall 0.95 |
| sample.pdf | pdf_pptx | 100.0 (100.0) | 100.0% | 100.0% | 3/3 | 100% | -/3 |  |
| sample.pdf | pdf_tiff | - | - | - | - | - | - | frames 1; pixel_exact True |
| sample.pdf | pdf_txt | 98.6 (98.6) | 100.0% | 100.0% | 3/3 | 100% | 3/3 |  |
| sample.pdf | pdf_xlsx | 100.0 (100.0) | 100.0% | 100.0% | 3/3 | 100% | -/3 |  |
| sample.pdf | tiff_pdf@pdf_tiff | 94.4 (94.4) | 97.5% | 97.5% | 3/3 | 75% | -/- | picture exact 3/3 |
| sample.pdf | tiff_pdf_pic@pdf_tiff | - | - | - | - | - | - | pages 1 |
| sample_125dpi.png | docx_pdf@img_docx | 98.6 (98.6) | 100.0% | 100.0% | 3/3 | 100% | -/3 | Word words kept in PDF 100% |
| sample_125dpi.png | img_docx | 98.6 (98.6) | 100.0% | 100.0% | 3/3 | 100% | 0/3 |  |
| sample_125dpi.png | img_docx_pic | - | - | - | - | - | - | picture_identical True |
| sample_125dpi.png | img_pdf | 98.6 (98.6) | 100.0% | 100.0% | 3/3 | 100% | -/3 |  |
| sample_125dpi.png | img_xlsx | 98.6 (98.6) | 100.0% | 100.0% | 3/3 | 100% | -/3 |  |
| text_only_1page.pdf | docx_pdf@pdf_docx | 76.9 (76.9) | 100.0% | 97.1% | 3/3 | n/a | -/10 | Word words kept in PDF 100% |
| text_only_1page.pdf | pdf_docx | 77.1 (76.4) | 100.0% | 97.5% | 3/3 | n/a | 5/10 |  |
| text_only_1page.pdf | pdf_jpeg | - | - | - | - | - | - | pages 1; psnr_db 44.3; size_ok True; ocr_recall 1.0 |
| text_only_1page.pdf | pdf_jpg | - | - | - | - | - | - | pages 1; psnr_db 44.3; size_ok True; ocr_recall 1.0 |
| text_only_1page.pdf | pdf_pptx | 78.3 (78.0) | 100.0% | 97.5% | 3/3 | n/a | -/10 |  |
| text_only_1page.pdf | pdf_tiff | - | - | - | - | - | - | frames 1; pixel_exact True |
| text_only_1page.pdf | pdf_txt | 93.5 (91.0) | 100.0% | 100.0% | 3/3 | n/a | 5/10 |  |
| text_only_1page.pdf | pdf_xlsx | 77.6 (77.1) | 100.0% | 97.5% | 3/3 | n/a | -/10 |  |
| text_only_1page.pdf | tiff_pdf@pdf_tiff | 93.7 (93.7) | 99.4% | 100.0% | 3/3 | n/a | -/- | picture exact 3/3 |
| text_only_1page.pdf | tiff_pdf_pic@pdf_tiff | - | - | - | - | - | - | pages 1 |

## Accuracy, every run

Run 1 follows a fresh server restart; runs 2 and 3 usually reuse the cached image. The model can give a different answer for the same input, so look at each run, not only the mean.

| input | conversion | run | sequence % | recall | precision | figures exact | words out / truth | notes |
|---|---|---|---|---|---|---|---|---|
| hi.png | docx_pdf@img_docx | run1 | 100.0 | 100.0% | 100.0% | yes | 12 / 12 | |
| hi.png | docx_pdf@img_docx | run2 | 100.0 | 100.0% | 100.0% | yes | 12 / 12 | |
| hi.png | docx_pdf@img_docx | run3 | 100.0 | 100.0% | 100.0% | yes | 12 / 12 | |
| hi.png | img_docx | run1 | 100.0 | 100.0% | 100.0% | yes | 12 / 12 | |
| hi.png | img_docx | run2 | 100.0 | 100.0% | 100.0% | yes | 12 / 12 | |
| hi.png | img_docx | run3 | 100.0 | 100.0% | 100.0% | yes | 12 / 12 | |
| hi.png | img_pdf | run1 | 100.0 | 100.0% | 100.0% | yes | 12 / 12 | |
| hi.png | img_pdf | run2 | 100.0 | 100.0% | 100.0% | yes | 12 / 12 | |
| hi.png | img_pdf | run3 | 100.0 | 100.0% | 100.0% | yes | 12 / 12 | |
| sample.pdf | docx_pdf@pdf_docx | run1 | 100.0 | 100.0% | 100.0% | yes | 72 / 72 | |
| sample.pdf | docx_pdf@pdf_docx | run2 | 100.0 | 100.0% | 100.0% | yes | 72 / 72 | |
| sample.pdf | docx_pdf@pdf_docx | run3 | 100.0 | 100.0% | 100.0% | yes | 72 / 72 | |
| sample.pdf | pdf_docx | run1 | 100.0 | 100.0% | 100.0% | yes | 72 / 72 | |
| sample.pdf | pdf_docx | run2 | 100.0 | 100.0% | 100.0% | yes | 72 / 72 | |
| sample.pdf | pdf_docx | run3 | 100.0 | 100.0% | 100.0% | yes | 72 / 72 | |
| sample.pdf | pdf_pptx | run1 | 100.0 | 100.0% | 100.0% | yes | 72 / 72 | |
| sample.pdf | pdf_pptx | run2 | 100.0 | 100.0% | 100.0% | yes | 72 / 72 | |
| sample.pdf | pdf_pptx | run3 | 100.0 | 100.0% | 100.0% | yes | 72 / 72 | |
| sample.pdf | pdf_txt | run1 | 98.6 | 100.0% | 100.0% | yes | 74 / 72 | |
| sample.pdf | pdf_txt | run2 | 98.6 | 100.0% | 100.0% | yes | 74 / 72 | |
| sample.pdf | pdf_txt | run3 | 98.6 | 100.0% | 100.0% | yes | 74 / 72 | |
| sample.pdf | pdf_xlsx | run1 | 100.0 | 100.0% | 100.0% | yes | 72 / 72 | |
| sample.pdf | pdf_xlsx | run2 | 100.0 | 100.0% | 100.0% | yes | 72 / 72 | |
| sample.pdf | pdf_xlsx | run3 | 100.0 | 100.0% | 100.0% | yes | 72 / 72 | |
| sample.pdf | tiff_pdf@pdf_tiff | run1 | 94.4 | 97.5% | 97.5% | yes | 72 / 72 | |
| sample.pdf | tiff_pdf@pdf_tiff | run2 | 94.4 | 97.5% | 97.5% | yes | 72 / 72 | |
| sample.pdf | tiff_pdf@pdf_tiff | run3 | 94.4 | 97.5% | 97.5% | yes | 72 / 72 | |
| sample_125dpi.png | docx_pdf@img_docx | run1 | 98.6 | 100.0% | 100.0% | yes | 70 / 72 | |
| sample_125dpi.png | docx_pdf@img_docx | run2 | 98.6 | 100.0% | 100.0% | yes | 70 / 72 | |
| sample_125dpi.png | docx_pdf@img_docx | run3 | 98.6 | 100.0% | 100.0% | yes | 70 / 72 | |
| sample_125dpi.png | img_docx | run1 | 98.6 | 100.0% | 100.0% | yes | 70 / 72 | |
| sample_125dpi.png | img_docx | run2 | 98.6 | 100.0% | 100.0% | yes | 70 / 72 | |
| sample_125dpi.png | img_docx | run3 | 98.6 | 100.0% | 100.0% | yes | 70 / 72 | |
| sample_125dpi.png | img_pdf | run1 | 98.6 | 100.0% | 100.0% | yes | 70 / 72 | |
| sample_125dpi.png | img_pdf | run2 | 98.6 | 100.0% | 100.0% | yes | 70 / 72 | |
| sample_125dpi.png | img_pdf | run3 | 98.6 | 100.0% | 100.0% | yes | 70 / 72 | |
| sample_125dpi.png | img_xlsx | run1 | 98.6 | 100.0% | 100.0% | yes | 70 / 72 | |
| sample_125dpi.png | img_xlsx | run2 | 98.6 | 100.0% | 100.0% | yes | 70 / 72 | |
| sample_125dpi.png | img_xlsx | run3 | 98.6 | 100.0% | 100.0% | yes | 70 / 72 | |
| text_only_1page.pdf | docx_pdf@pdf_docx | run1 | 76.9 | 100.0% | 97.1% | yes | 367 / 278 | |
| text_only_1page.pdf | docx_pdf@pdf_docx | run2 | 76.9 | 100.0% | 97.1% | yes | 367 / 278 | |
| text_only_1page.pdf | docx_pdf@pdf_docx | run3 | 76.9 | 100.0% | 97.1% | yes | 367 / 278 | |
| text_only_1page.pdf | pdf_docx | run1 | 78.4 | 100.0% | 97.1% | yes | 362 / 278 | |
| text_only_1page.pdf | pdf_docx | run2 | 76.4 | 100.0% | 97.7% | yes | 345 / 278 | |
| text_only_1page.pdf | pdf_docx | run3 | 76.4 | 100.0% | 97.7% | yes | 345 / 278 | |
| text_only_1page.pdf | pdf_pptx | run1 | 78.8 | 100.0% | 97.1% | yes | 367 / 278 | |
| text_only_1page.pdf | pdf_pptx | run2 | 78.0 | 100.0% | 97.7% | yes | 350 / 278 | |
| text_only_1page.pdf | pdf_pptx | run3 | 78.0 | 100.0% | 97.7% | yes | 350 / 278 | |
| text_only_1page.pdf | pdf_txt | run1 | 98.6 | 100.0% | 100.0% | yes | 274 / 278 | |
| text_only_1page.pdf | pdf_txt | run2 | 91.0 | 100.0% | 100.0% | yes | 280 / 278 | |
| text_only_1page.pdf | pdf_txt | run3 | 91.0 | 100.0% | 100.0% | yes | 280 / 278 | |
| text_only_1page.pdf | pdf_xlsx | run1 | 78.8 | 100.0% | 97.1% | yes | 367 / 278 | |
| text_only_1page.pdf | pdf_xlsx | run2 | 77.1 | 100.0% | 97.7% | yes | 350 / 278 | |
| text_only_1page.pdf | pdf_xlsx | run3 | 77.1 | 100.0% | 97.7% | yes | 350 / 278 | |
| text_only_1page.pdf | tiff_pdf@pdf_tiff | run1 | 93.7 | 99.4% | 100.0% | yes | 260 / 278 | |
| text_only_1page.pdf | tiff_pdf@pdf_tiff | run2 | 93.7 | 99.4% | 100.0% | yes | 260 / 278 | |
| text_only_1page.pdf | tiff_pdf@pdf_tiff | run3 | 93.7 | 99.4% | 100.0% | yes | 260 / 278 | |
