# idox web app

A local page (127.0.0.1 only) where you upload a file, pick a conversion and download the result. It runs the scripts in
`python_scripts/` as subprocesses, one job folder each (`~/.local/share/idox/jobs/`). Standard library only.

Start the local model first (see `docs/new_conversions.md`, section 2), then from the repo folder:

```powershell
.\.venv\Scripts\python web\idox_app.py
```

Open http://127.0.0.1:8765, go to **Settings**, choose the **local model** (`http://127.0.0.1:8090`), pick a file and a
target format. Port: `--port N`.

All 25 conversions are listed, including the ten newer ones (Image/PDF/Word to XML, CSV, PowerPoint, Excel; Word to PNG).
The Word ones need LibreOffice and show as unavailable without it.
