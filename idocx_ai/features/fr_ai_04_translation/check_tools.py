"""Check FR-AI-04's layout handling with a FAKE translator, without the model.

    .venv/bin/python features/fr_ai_04_translation/check_tools.py

The fake translator marks each block "TR[es] ..." (or returns fixed Hindi), so the checks
can see exactly which text was replaced and where. Files go to output/check_tools/.
"""

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

import pymupdf  # noqa: E402

from core import store  # noqa: E402
from core.pdfutil import ToolError  # noqa: E402
from features.fr_ai_04_translation import translate as tr  # noqa: E402

INPUT = HERE / "input"
OUT = HERE / "output" / "check_tools"
shutil.rmtree(OUT, ignore_errors=True)
store.set_root(OUT / "files")

ALL = json.loads((INPUT / "ground_truth.json").read_text())
truth = ALL["agreement.pdf"]
failures, report = 0, []


def log(line=""):
    print(line)
    report.append(line)


def check(label, ok, detail=""):
    global failures
    failures += not ok
    log(f"  {'PASS' if ok else 'FAIL'}  {label}{'  -- ' + detail if detail else ''}")


def opened(fid):
    _, path = store.get_file(fid)
    return pymupdf.open(path)


src = store.save_file((INPUT / "agreement.pdf").read_bytes(), "agreement.pdf", 2, source="test")["id"]
with opened(src) as d:
    before = {"images": sum(len(p.get_images()) for p in d), "lines": sum(len(p.get_drawings()) for p in d),
              "blocks": [b["text"] for p in d for b in tr._blocks(p)]}

log("layout with a fake Spanish translator")
r = tr.translate_document(src, "Spanish", translator=lambda texts, lang, keep=None: [f"TR[{lang}] {t}" for t in texts])
with opened(r["new_file_id"]) as d:
    text = " ".join(" ".join(p.get_text().split()) for p in d)
    after = {"images": sum(len(p.get_images()) for p in d), "lines": sum(len(p.get_drawings()) for p in d)}
    lang = tr.catalog_lang(d) if hasattr(tr, "catalog_lang") else d.xref_get_key(d.pdf_catalog(), "Lang")[1]
    cells = {a: [tuple(round(v) for v in rect) for rect in d[0].search_for(a)] for a in truth["number_only_cells"]}
with opened(src) as d:
    cells_before = {a: [tuple(round(v) for v in rect) for rect in d[0].search_for(a)] for a in truth["number_only_cells"]}
check("every text block replaced in place (no original English left)",
      r["blocks_translated"] == text.count("TR[es]") and r["blocks_translated"] + r["blocks_kept_as_is"] == len(before["blocks"]),
      f"{r['blocks_translated']} translated, {r['blocks_kept_as_is']} number-only kept")
check("the logo image and the table lines survive", after == {"images": before["images"], "lines": before["lines"]},
      f"{after['images']} image, {after['lines']} lines")
check("number-only table cells untouched, in the same place", cells == cells_before)
check("every protected value still present", all(tr.present(v, text) for v in truth["must_survive"]))
check("document language set to 'es'", lang == "es", lang)

log("a translation that loses a number is retried, then flagged")
calls = []


def lossy(texts, lang, keep=None):
    calls.append((len(texts), list(texts), keep))
    return [t.replace("45", "forty-five") for t in texts]


r2 = tr.translate_document(src, "es", pages="2", translator=lossy)
check("the block that lost '45' is flagged, after one retry",
      [f["missing"] for f in r2.get("check_these", [])] == [["45"]] and calls[-1][0] == 1,
      json.dumps(r2.get("check_these")))
retry_text, retry_keep = calls[-1][1][0], calls[-1][2]
check("the retry sends the original text unchanged; the reminder goes in the instructions",
      retry_text.startswith("The Buyer shall pay") and "must" not in retry_text.lower() and retry_keep == ["45"],
      f"keep={retry_keep}")

log("long translations shrink to fit; Hindi renders with an embedded Devanagari font")
r3 = tr.translate_document(src, "hi", pages="2", translator=lambda texts, lang, keep=None: [
    t if not any(c.isalpha() for c in t) else
    "यह परीक्षण के लिए एक लंबा हिंदी अनुवाद है जो मूल पाठ से अधिक लंबा है। " * 3 + " ".join(tr.protected(t))
    for t in texts])
with opened(r3["new_file_id"]) as d:
    fonts = [f[3] for f in d[1].get_fonts()]          # r3 translated page 2 only
    text3 = d[1].get_text()
check("Devanagari font embedded", any("Devanagari" in f for f in fonts), ", ".join(sorted(set(fonts))))
check("over-long text was shrunk to fit rather than spilling", bool(r3.get("shrunk_to_fit")),
      f"{len(r3.get('shrunk_to_fit', []))} block(s) shrunk, smallest scale "
      f"{min((s['scale'] for s in r3.get('shrunk_to_fit', [])), default=1)}")
check("IDs, emails and amounts inside Hindi still copy correctly",
      all(v in text3 for v in ("legal@example.com", "27ABCPE1234F1ZB")))
check("the Hindi copy/search caveat is reported", "note" in r3)

log("text copy: plain text that copies and searches correctly (unlike Devanagari in the PDF)")
copy_meta, copy_path = store.get_file(r3["text_copy_file_id"])
copy_text = copy_path.read_text(encoding="utf-8")
check("a .txt copy is made, with the exact Hindi text and the protected values",
      copy_meta["ext"] == "txt" and "यह परीक्षण के लिए एक लंबा हिंदी अनुवाद है" in copy_text
      and "legal@example.com" in copy_text, f"{len(copy_text)} characters, ext {copy_meta['ext']}")
check("both files are reported as created", [f["ext"] for f in r3["files_created"]] == ["pdf", "txt"])

log("a scanned page: OCR, then the translation on a clean page")
scan = store.save_file((INPUT / "agreement_scanned.pdf").read_bytes(), "agreement_scanned.pdf", 1, source="test")["id"]
r4 = tr.translate_document(scan, "es", translator=lambda texts, lang, keep=None: [f"TR[{lang}] {t}" for t in texts])
with opened(r4["new_file_id"]) as d:
    pg = d[0]
    text4 = " ".join(pg.get_text().split())
    images4 = len(pg.get_images())
check("the scanned image is gone and the translated text is real text on the page",
      images4 == 0 and text4.count("TR[es]") == r4["blocks_translated"] > 5,
      f"{r4['blocks_translated']} blocks translated, images left: {images4}")
check("number-only values from the scan are written back unchanged",
      all(a in text4 for a in ALL["agreement_scanned.pdf"]["number_only_cells"]),
      ", ".join(ALL["agreement_scanned.pdf"]["number_only_cells"]))
check("the scanned-page note is reported", "scanned_pages" in r4, r4.get("scanned_pages", "")[:70])

log("refusals")
try:
    tr.translate_document(src, "French")
    check("an unsupported language is refused", False)
except ToolError as e:
    check("an unsupported language is refused", "Hindi (hi), Spanish (es)" in str(e), str(e))

log(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
sys.exit(1 if failures else 0)
