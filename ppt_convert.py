"""
ppt_convert.py -- convert a PowerPoint deck (.pptx) into a PDF or into pictures
(PNG / JPG / JPEG, one per slide).

    --to pdf               one PDF, a page per slide, with a bookmark per slide
    --to png|jpg|jpeg      one picture per slide: <name>/slide_01_<title>.png, ...

THE SLIDES ARE DRAWN BY LIBREOFFICE IMPRESS, NEVER BY THE MODEL. Impress opens the deck
and draws every slide exactly (text, tables, charts, pictures, shapes, theme) into a PDF;
PyMuPDF turns its pages into the pictures. Hidden slides are left out, as in a slide show.

WHAT THE MODEL (the Qwen 2B, with its vision part) DOES. It LOOKS at each drawn slide,
together with the text the deck holds for that slide, and answers in a fixed JSON shape:

  1. TITLE   a short title for the slide. A slide's own title (its title placeholder) is
             always used when it has one; the model's title is used only for slides without
             one, and only if its words are on the slide (a made-up title falls back to
             "Slide N"). Titles become the PDF's bookmarks and the picture file names.
  2. CHECK   what is wrong with the slide as drawn: text cut off, text overlapping, text
             running off the slide, hard to read, a blank slide. Claims the program can
             check are checked against the deck and the drawing (a "blank" slide that has
             ink is rejected; "off the slide" is compared with the shapes' positions); the
             others are reported as the model's notes, not checked.

The verdict never rests on the model. If the model server is down (or --no-ai), titles
come from the slides' own titles or "Slide N", and there is no visual check.

HOW IT PROVES NOTHING WAS LOST (read back from the written files):
  1. COUNT       one page / picture per visible slide
  2. TEXT        the words of each slide's text (text boxes, placeholders, tables, groups)
                 are on that slide's PDF page
  3. PICTURES    each file is really that format and size, and not blank when the slide
                 has content
  Advisory: shapes that already run past the slide's edge in the deck itself.

Needs LibreOffice Impress:  sudo apt-get install -y libreoffice-impress
Usage:
    .venv/bin/python ppt_convert.py ppt_to_pdf_input/deck.pptx --to pdf
    .venv/bin/python ppt_convert.py ppt_to_image_input/deck.pptx --to png
    .venv/bin/python ppt_convert.py deck.pptx --to jpg --quality 85 --slides 1-3 --no-ai
"""

import argparse
import base64
import glob
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import List, Literal

import pymupdf
from PIL import Image
from pptx import Presentation
from pptx.util import Emu
from pydantic import BaseModel, Field

OUT_DIRS = {"pdf": "ppt_to_pdf_output", "png": "ppt_to_image_output",
            "jpg": "ppt_to_image_output", "jpeg": "ppt_to_image_output"}
FORMATS = {"png": {"dpi": 200}, "jpg": {"dpi": 150, "quality": 90}, "jpeg": {"dpi": 150, "quality": 90}}
DEFAULT_BASE_URL = os.environ.get("IDOX_BASE_URL", "http://127.0.0.1:8090")
AI_SIDE_PX = 1024          # the slide picture sent to the model, longest side
MAX_AI_SLIDES = 40
TEXT_COVERAGE = 0.97
INK_CONTRAST = 40


# ---------------------------------------------------------------------------
# what the model must answer
# ---------------------------------------------------------------------------
Issue = Literal["text_cut_off", "text_overlaps", "text_off_slide", "hard_to_read", "blank_slide"]


class SlideReview(BaseModel):
    title: str = Field(description="A title for this slide, at most 8 words, using words that are on the slide.")
    issues: List[Issue] = Field(max_length=4, description="Problems you can SEE on the drawn slide; [] if none.")
    note: str = Field(description="One short sentence on the slide's layout, at most 20 words.")


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


# ---------------------------------------------------------------------------
# reading the deck
# ---------------------------------------------------------------------------
def shape_texts(shape) -> list:
    """Every piece of text in a shape: its text frame, its table cells, the shapes in a group."""
    out = []
    if shape.shape_type == 6 and hasattr(shape, "shapes"):          # a group
        for s in shape.shapes:
            out += shape_texts(s)
        return out
    if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
        out.append(shape.text_frame.text)
    if getattr(shape, "has_table", False):
        for row in shape.table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    out.append(cell.text)
    return out


def read_deck(path: str) -> tuple:
    """(slide width pt, height pt, [one dict per slide])."""
    prs = Presentation(path)
    W, H = Emu(prs.slide_width).pt, Emu(prs.slide_height).pt
    slides = []
    for i, sl in enumerate(prs.slides, 1):
        hidden = sl._element.get("show") == "0"
        texts, off, pics, charts = [], [], 0, 0
        for sh in sl.shapes:
            texts += shape_texts(sh)
            if sh.shape_type == 13:
                pics += 1
            if getattr(sh, "has_chart", False):
                charts += 1
            if sh.left is not None and sh.width is not None and shape_texts(sh):
                x0, y0 = Emu(sh.left).pt, Emu(sh.top).pt
                x1, y1 = x0 + Emu(sh.width).pt, y0 + Emu(sh.height).pt
                if x0 < -0.02 * W or y0 < -0.02 * H or x1 > 1.02 * W or y1 > 1.02 * H:
                    off.append(getattr(sh, "name", "shape"))
        title = ""
        try:
            if sl.shapes.title is not None and sl.shapes.title.text.strip():
                title = sl.shapes.title.text.strip().splitlines()[0]
        except Exception:
            pass
        slides.append({"index": i, "hidden": hidden, "text": "\n".join(texts), "title": title,
                       "off_slide": off, "pictures": pics, "charts": charts,
                       "has_content": bool(texts) or pics > 0 or charts > 0 or len(sl.shapes) > 0})
    return W, H, slides


# ---------------------------------------------------------------------------
# LibreOffice Impress
# ---------------------------------------------------------------------------
def find_soffice() -> str:
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    raise SystemExit("LibreOffice not found. Install it with:\n    sudo apt-get install -y libreoffice-impress")


def have_impress() -> bool:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        return False
    folder = os.path.dirname(os.path.realpath(exe))
    return os.path.exists(os.path.join(folder, "libsdlo.so"))


def to_pdf(pptx: str, out_dir: str, timeout: int = 300) -> str:
    """The deck as a PDF, a page per visible slide. Throwaway profile, so an open
    LibreOffice window cannot swallow the call."""
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        proc = subprocess.run([find_soffice(), f"-env:UserInstallation=file://{profile}", "--headless",
                               "--norestore", "--convert-to", "pdf:impress_pdf_Export", "--outdir", out_dir, pptx],
                              capture_output=True, text=True, timeout=timeout)
    pdf = os.path.join(out_dir, os.path.splitext(os.path.basename(pptx))[0] + ".pdf")
    if not os.path.exists(pdf):
        why = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError("LibreOffice wrote no PDF: " + (why[-1] if why else f"exit {proc.returncode}"))
    return pdf


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------
def review_slide(base_url: str, png_path: str, slide_text: str):
    """The model's look at one drawn slide: (SlideReview, seconds). Raises if it cannot be had."""
    import requests

    with Image.open(png_path) as im:
        im = im.convert("RGB")
        scale = AI_SIDE_PX / max(im.size)
        if scale < 1:
            im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    api_key = os.environ.get("IDOX_API_KEY", "").strip()
    thinking = os.environ.get("IDOX_THINKING", "").strip().lower()
    body = {
        "model": os.environ.get("IDOX_MODEL", "manual") or "manual",
        "messages": [
            {"role": "system", "content": REVIEW_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "TEXT THE SLIDE HOLDS:\n" + (slide_text[:1500] or "(no text)")},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]},
        ],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "slide_review", "schema": SlideReview.model_json_schema()}},
        "temperature": 0, "repeat_penalty": 1.1, "n_predict": 300,
        **({"chat_template_kwargs": {"enable_thinking": thinking == "on"}} if thinking in ("on", "off") else {}),
    }
    t0 = time.time()
    resp = requests.post(f"{base_url.rstrip('/')}/v1/chat/completions", json=body,
                         headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                         timeout=int(os.environ.get("IDOX_TIMEOUT", "600") or 600))
    if not resp.ok:
        raise RuntimeError(f"the server answered {resp.status_code}: {resp.text[:160].strip()}")
    content = resp.json()["choices"][0]["message"].get("content") or ""
    return SlideReview.model_validate_json(content), time.time() - t0


def words(s: str) -> list:
    return re.findall(r"\w+", s.lower())


def grounded(title: str, slide_text: str) -> bool:
    """At least 60% of the title's words (3+ letters) are on the slide."""
    ws = [w for w in words(title) if len(w) >= 3]
    have = set(words(slide_text))
    return bool(ws) and sum(w in have for w in ws) / len(ws) >= 0.6


def page_lines(page) -> list:
    """[(text, Rect)] for every line of text drawn on the page."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] == 0:
            for ln in b["lines"]:
                t = "".join(sp["text"] for sp in ln["spans"]).strip()
                if t:
                    out.append((t, pymupdf.Rect(ln["bbox"])))
    return out


def overlapping_lines(lines: list) -> list:
    """Pairs of text lines drawn over each other (more than 30% of the smaller one)."""
    hits = []
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            a, b = lines[i][1], lines[j][1]
            inter = a & b
            if not inter.is_empty and inter.get_area() > 0.3 * min(a.get_area(), b.get_area()) > 0:
                hits.append((lines[i][0][:25], lines[j][0][:25]))
    return hits


def has_ink(path: str) -> bool:
    with Image.open(path) as im:
        lo, hi = im.convert("L").getextrema()
    return hi - lo > INK_CONTRAST


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._")[:40]


def parse_slides(spec: str, n: int) -> list:
    if spec in ("all", ""):
        return list(range(1, n + 1))
    out = []
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        if not a.isdigit() or (b and not b.isdigit()):
            raise SystemExit(f"--slides should be all, or like 1, 1-3 or 2,4 (got {spec!r})")
        out.extend(range(int(a), int(b or a) + 1))
    return sorted({i for i in out if 1 <= i <= n})


# ---------------------------------------------------------------------------
def run(pptx: str, to: str, out_dir: str, dpi: int, quality: int, slides_spec: str,
        base_url: str, use_ai: bool) -> bool:
    name = os.path.splitext(os.path.basename(pptx))[0]
    print("=" * 72)
    print(pptx)
    print("=" * 72)
    started = time.time()
    try:
        W, H, slides = read_deck(pptx)
    except Exception as exc:
        print(f"  COULD NOT OPEN: {exc}")
        return False
    visible = [s for s in slides if not s["hidden"]]
    wanted = set(parse_slides(slides_spec, len(slides)))
    targets = [s for s in visible if s["index"] in wanted]
    hidden = len(slides) - len(visible)
    if not targets:
        print(f"  SKIPPED: no slide to convert (slides: {len(slides)}, hidden: {hidden})")
        return False
    if not have_impress():
        print("  CONVERSION FAILED: LibreOffice Impress is not installed. Install it with:\n"
              "      sudo apt-get install -y libreoffice-impress")
        return False
    os.makedirs(out_dir, exist_ok=True)
    what = "PDF" if to == "pdf" else f"{to.upper()} at {dpi} dpi"
    print(f"  {len(slides)} slide(s)" + (f" ({hidden} hidden, left out)" if hidden else "")
          + f", converting {len(targets)} as {what} -> {out_dir}/")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            doc = pymupdf.open(to_pdf(os.path.abspath(pptx), tmp))
        except Exception as exc:
            print(f"  CONVERSION FAILED: {exc}")
            return False
        if doc.page_count != len(visible):
            print(f"  CONVERSION FAILED: the PDF has {doc.page_count} page(s) for {len(visible)} visible slide(s)")
            return False
        page_of = {s["index"]: doc[i] for i, s in enumerate(visible)}

        results, problems, titles, ai_used, files = [], [], {}, 0, []
        img_dir = os.path.join(out_dir, name)
        if to != "pdf":
            os.makedirs(img_dir, exist_ok=True)
        for s in targets:
            page = page_of[s["index"]]
            sp, info = [], []
            # 1. draw it (also what the model looks at)
            preview = os.path.join(tmp, f"slide_{s['index']:03d}.png")
            page.get_pixmap(dpi=96, alpha=False).save(preview)
            ink = has_ink(preview)
            # 2. the model's look
            review, why = None, "turned off (--no-ai)"
            if use_ai and ai_used < MAX_AI_SLIDES:
                ai_used += 1
                try:
                    review, secs = review_slide(base_url, preview, s["text"])
                    why = f"{secs:.1f}s"
                except Exception as exc:
                    why = (f"the model server at {base_url} could not be reached"
                           if "Max retries" in str(exc) or "ConnectionError" in type(exc).__name__
                           else str(exc).splitlines()[0][:120])
            elif use_ai:
                why = f"only the first {MAX_AI_SLIDES} slides are checked by the model"
            # title: the slide's own, else the model's if it is grounded, else "Slide N"
            if s["title"]:
                title, src = s["title"], "slide's own title"
            elif review and review.title.strip() and grounded(review.title, s["text"]):
                title, src = review.title.strip(), "model's title"
            else:
                title, src = f"Slide {s['index']}", ("model's title not on the slide, not used" if review
                                                     and review.title.strip() else "no title")
            titles[s["index"]] = title
            # the model's issues, checked where the program can check them
            # 3. the deterministic checks (also what the model's claims are checked against)
            page_words = set(words(page.get_text()))
            sw = [w for w in words(s["text"]) if len(w) > 1]
            cov = (sum(w in page_words for w in sw) / len(sw)) if sw else 1.0
            missing = sorted({w for w in sw if w not in page_words})[:6]
            lines = page_lines(page)
            past_edge = [t for t, r in lines if r.x1 > page.rect.x1 + 1 or r.y1 > page.rect.y1 + 1 or r.x0 < -1]
            overlaps = overlapping_lines(lines)
            checked, unchecked, rejected = [], [], []
            for iss in (review.issues if review else []):
                if iss == "blank_slide":
                    (rejected if ink else checked).append(iss)
                elif iss == "text_off_slide":
                    (checked if s["off_slide"] or past_edge else rejected).append(iss)
                elif iss == "text_cut_off":
                    (checked if missing or past_edge else rejected).append(iss)
                elif iss == "text_overlaps":
                    (checked if overlaps else rejected).append(iss)
                else:
                    unchecked.append(iss)
            if cov < TEXT_COVERAGE:
                sp.append(f"only {cov:.0%} of the slide's words are on its page; missing e.g. {', '.join(missing)}")
            if s["has_content"] and not ink:
                sp.append("the drawn slide is blank but the slide has content")
            if overlaps:
                info.append(f"~ {len(overlaps)} pair(s) of text lines are drawn over each other, e.g. {overlaps[0]}")
            if s["off_slide"]:
                info.append(f"~ in the deck itself, {len(s['off_slide'])} text shape(s) run past the slide's edge: "
                            + ", ".join(s["off_slide"][:3]))
            # 4. the picture
            if to != "pdf":
                fn = f"slide_{s['index']:02d}_{safe_name(title) or 'slide'}.{to}"
                path = os.path.join(img_dir, fn)
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                if to == "png":
                    img.save(path, "PNG", dpi=(dpi, dpi))
                else:
                    img.save(path, "JPEG", quality=quality, dpi=(dpi, dpi), subsampling=0, optimize=True)
                with Image.open(path) as back:
                    if back.format != ("PNG" if to == "png" else "JPEG") or back.size != (pix.width, pix.height):
                        sp.append(f"{fn}: read back as {back.format} {back.size}, expected {pix.width}x{pix.height}")
                if s["has_content"] and not has_ink(path):
                    sp.append(f"{fn}: the picture is blank")
                files.append(path)
                info.insert(0, f"{fn}: {pix.width}x{pix.height}px {os.path.getsize(path) / 1024:.0f} KB")
            problems += [f"slide {s['index']}: {p}" for p in sp]
            ai_used_here = review is not None
            results.append((s, not sp, ai_used_here))
            print(f"  slide {s['index']}: {title!r} ({src}); text {cov:.0%} on the page"
                  + (f"; {s['pictures']} picture(s)" if s["pictures"] else "")
                  + (f"; {s['charts']} chart(s)" if s["charts"] else "")
                  + f"  [{'PASS' if not sp else 'FAIL'}]")
            if review:
                seen = "; ".join(filter(None, [
                    f"confirmed: {', '.join(checked)}" if checked else "",
                    f"model notes (not checked against the deck): {', '.join(unchecked)}" if unchecked else "",
                    f"rejected (the file says otherwise): {', '.join(rejected)}" if rejected else ""]))
                print(f"       AI check ({why}): {seen or 'no issues seen'}" + (f" -- {review.note}" if review.note else ""))
            else:
                print(f"       AI check: not used ({why})")
            for i in info:
                print(f"       - {i}")
            for p in sp:
                print(f"       ! {p}")

        # 5. the PDF: the slides chosen, with a bookmark per slide
        if to == "pdf":
            out = pymupdf.open()
            for s in targets:
                out.insert_pdf(doc, from_page=page_of[s["index"]].number, to_page=page_of[s["index"]].number)
            out.set_toc([[1, titles[s["index"]], n] for n, s in enumerate(targets, 1)])
            out.set_metadata({"title": titles[targets[0]["index"]], "creator": "idox ppt_convert.py"})
            path = os.path.join(out_dir, f"{name}.pdf")
            out.save(path, garbage=3, deflate=True)
            back = pymupdf.open(path)
            if back.page_count != len(targets):
                problems.append(f"the PDF has {back.page_count} pages, expected {len(targets)}")
            if len(back.get_toc()) != len(targets):
                problems.append(f"the PDF has {len(back.get_toc())} bookmarks, expected {len(targets)}")
            files.append(path)
            print(f"  {name}.pdf: {back.page_count} page(s), {len(back.get_toc())} bookmark(s), "
                  f"{os.path.getsize(path) / 1024:.0f} KB")

    ok = not problems and len(results) == len(targets)
    print()
    print("  --- verification (read back from the written files) ---")
    print(f"  slides converted:  {len(results)}/{len(targets)}")
    print(f"  slides verified:   {sum(1 for _, good, _ in results if good)}/{len(results)}")
    print(f"  files written:     {len(files)}")
    print(f"  AI check:          {sum(1 for *_, a in results if a)}/{len(results)} slide(s) looked at by the model")
    print(f"  {time.time() - started:.1f}s")
    print(f"  VERDICT: {'PASS -- every slide converted and verified' if ok else 'FAIL -- see the problems above'}")
    print()
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", help="PowerPoint file(s) (.pptx), globs allowed")
    parser.add_argument("--to", choices=("pdf", "png", "jpg", "jpeg"), required=True)
    parser.add_argument("--format", choices=("png", "jpg", "jpeg"), default=None,
                        help="the picture format; overrides --to when given (used by the API and web app)")
    parser.add_argument("--outdir", default=None, help="default ppt_to_pdf_output or ppt_to_image_output")
    parser.add_argument("--dpi", type=int, default=None, help="pictures: default 200 for png, 150 for jpg / jpeg")
    parser.add_argument("--quality", type=int, default=None, help="jpg / jpeg quality 1-100 (default 90)")
    parser.add_argument("--slides", default="all", help="e.g. 1, 1-3, 2,4 (default: all visible slides)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"the model server (default {DEFAULT_BASE_URL})")
    parser.add_argument("--no-ai", action="store_true", help="skip the model's look at each slide")
    args = parser.parse_args()
    if args.format and args.to != "pdf":
        args.to = args.format
    fmt = FORMATS.get(args.to, {})
    dpi = args.dpi if args.dpi is not None else fmt.get("dpi", 150)
    quality = args.quality if args.quality is not None else fmt.get("quality", 90)
    if not 30 <= dpi <= 600:
        raise SystemExit("--dpi must be between 30 and 600")
    if not 1 <= quality <= 100:
        raise SystemExit("--quality must be between 1 and 100")
    paths = []
    for pattern in args.files:
        paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern])
    ok = fail = 0
    for path in paths:
        if not os.path.exists(path):
            print(f"skipping {path}: not found")
            continue
        if run(path, args.to, args.outdir or OUT_DIRS[args.to], dpi, quality, args.slides, args.base_url, not args.no_ai):
            ok += 1
        else:
            fail += 1
    if len(paths) > 1:
        print("=" * 72)
        print(f"{ok} succeeded, {fail} failed, {len(paths)} total")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
