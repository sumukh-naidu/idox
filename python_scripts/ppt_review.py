"""
ppt_review.py -- render each slide of a .pptx to a picture and have the model
review what it can SEE.

The PPTX builder estimates text heights (python-pptx cannot actually render), so
it cannot tell whether text was cut off, overlapped, or ran off the slide. This
renders the deck to one picture per slide and asks a vision model to report only
problems it can see: cut-off text, overlaps, text off the slide, hard-to-read
text, blank slides. It never changes the deck -- it reports.

Usage:
    .venv/Scripts/python python_scripts/ppt_review.py output/pdf_to_ppt_output/sample_digital_document.pptx
    .venv/Scripts/python python_scripts/ppt_review.py output/pdf_to_ppt_output/sample_digital_document.pptx --json
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from blocks import LOCAL_BASE_URL  # noqa: E402

REVIEW_PROMPT = """You check ONE presentation slide that a program has drawn as a picture. You
get the picture and the text the slide is supposed to hold. You never change the slide.

1. title: a short title (at most 8 words) built from words that are on the slide. If the
   slide shows a title, use it.
2. issues: only problems you can SEE in the picture:
   - text_cut_off    text that is cut at an edge of its box or of the slide
   - text_overlaps   text drawn over other text or over a picture so it cannot be read
   - text_off_slide  text that runs past the edge of the slide
   - hard_to_read    text too small or too faint against its background
   - blank_slide     nothing on the slide at all
   Use [] when the slide looks fine. Do not guess: list an issue only if you see it.
3. note: one short sentence on the layout."""

# Slide text is passed alongside the picture so the reviewer knows what was
# meant to be there -- but it only REPORTS what it sees, never what it expected.
from pptx import Presentation  # noqa: E402


def slide_texts(pptx_path):
    """One string of text per slide, in slide order."""
    prs = Presentation(pptx_path)
    out = []
    for slide in prs.slides:
        parts = []
        for sh in slide.shapes:
            if sh.has_text_frame:
                parts.append(sh.text_frame.text)
            if sh.has_table:
                for r in sh.table.rows:
                    parts.append(" ".join(c.text for c in r.cells))
        out.append("\n".join(parts))
    return out


def render_slides(pptx_path, out_dir, dpi=150):
    """PPTX -> PDF (LibreOffice) -> one PNG per slide."""
    import pymupdf
    os.makedirs(out_dir, exist_ok=True)
    from doc_pipeline import find_soffice
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(
            [find_soffice(), f"-env:UserInstallation={Path(profile).as_uri()}", "--headless",
             "--norestore", "--convert-to", "pdf", "--outdir", out_dir, pptx_path],
            capture_output=True, text=True, timeout=300)
    stem = Path(pptx_path).stem
    pdf = os.path.join(out_dir, stem + ".pdf")
    if not os.path.exists(pdf):
        raise RuntimeError(f"LibreOffice produced no PDF for {pptx_path}")
    pngs = []
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc, start=1):
            p = os.path.join(out_dir, f"{stem}_slide{i}.png")
            page.get_pixmap(dpi=dpi).save(p)
            pngs.append(p)
    return pngs


def ask(image_path, slide_text):
    import requests
    with open(image_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode()
    mime = "image/jpeg" if raw[:2] == b"\xff\xd8" else "image/png"
    body = {
        "model": "manual",
        "messages": [
            {"role": "system", "content": REVIEW_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": f"Slide text:\n{slide_text}"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]},
        ],
        "temperature": 0,
        "max_tokens": 512,
    }
    resp = requests.post(LOCAL_BASE_URL + "/v1/chat/completions", json=body, timeout=300)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def geometric_flags(pptx_path):
    """Exact per-shape bounds vs the slide edge and vs each other.

    Catches real overflow and overlaps that the rendered picture cannot show.
    The visual reviewer is stricter and over-reports, so this is the tiebreaker
    when the two disagree. The empty title placeholder is ignored: it is cleared
    but kept for its geometry, and overlapping it with the full-width title
    textbox is deliberate, not a defect.
    """
    prs = Presentation(pptx_path)
    sw, sh = prs.slide_width / 914400, prs.slide_height / 914400
    out = {}
    for i, slide in enumerate(prs.slides, start=1):
        flags = []
        shapes = []
        for shp in slide.shapes:
            l, t, w, h = (shp.left / 914400, shp.top / 914400,
                          shp.width / 914400, shp.height / 914400)
            empty = shp.has_text_frame and not shp.text_frame.text.strip()
            shapes.append((empty, l, t, w, h))
            if l < -0.01:
                flags.append("left_off")
            if t < -0.01:
                flags.append("top_off")
            if l + w > sw + 0.01:
                flags.append("right_off")
            if t + h > sh + 0.01:
                flags.append("bottom_off")
        for a in range(len(shapes)):
            for b in range(a + 1, len(shapes)):
                ea, la, ta, wa, ha = shapes[a]
                eb, lb, tb, wb, hb = shapes[b]
                if ea or eb:                      # skip the cleared title placeholder
                    continue
                ox = min(la + wa, lb + wb) - max(la, lb)
                oy = min(ta + ha, tb + hb) - max(ta, tb)
                if ox > 0.05 and oy > 0.05:
                    flags.append("overlap")
        out[i] = sorted(set(flags))
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pptx")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--outdir", default="ppt_review")
    args = ap.parse_args()

    texts = slide_texts(args.pptx)
    pngs = render_slides(args.pptx, args.outdir)
    bounds = geometric_flags(args.pptx)
    if len(pngs) != len(texts):
        print(f"warn: {len(pngs)} slides rendered but {len(texts)} text entries")

    results = []
    for i, (png, text) in enumerate(zip(pngs, texts), start=1):
        raw = ask(png, text)
        geo = bounds.get(i, [])
        if args.json:
            results.append({"slide": i, "image": png, "geometric": geo, "review": raw.strip()})
        else:
            print(f"=== slide {i} ({png}) ===")
            print(raw.strip())
            print(f"  geometric: {geo if geo else 'shapes all inside the slide'}")
            print()

    if args.json:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()