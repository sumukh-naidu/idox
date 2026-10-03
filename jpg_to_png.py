"""
jpg_to_png.py -- convert JPG / JPEG images to PNG.

NO MODEL IS INVOLVED. Both are ordinary pictures, so this decodes the JPG and writes the
same pixels out as PNG (Pillow). It takes about 0.1 s per image.

WHAT YOU GET, AND WHAT YOU DO NOT
  The PNG holds EXACTLY the pixels the JPG shows. It does not bring back detail the JPG
  already lost, and it is larger (about 3x on a page of text, often more on a photo). The
  gains are that it can be edited and re-saved without further loss, and that the format
  supports transparency.

THINGS A STRAIGHT CONVERSION GETS WRONG, AND WHAT IS DONE
  CMYK JPEGs   PNG cannot store CMYK (a straight save fails). They are converted to RGB and
               the report says so. Their colour profile is dropped, since it describes CMYK.
  Rotation     A phone photo is often stored sideways with a hidden "orientation" tag. The
               tag is applied to the pixels, so the PNG looks the way the photo looked.
  dpi, colour profile   Kept (for rotated-by-90 photos the two dpi values are swapped).
  EXIF         NOT carried over (camera details, and possibly GPS location). The report says so.
  Output name  photo.jpg -> photo.png in the output folder. An existing file is never
               overwritten: the new one becomes photo_2.png, photo_3.png, and so on.

HOW IT PROVES THE RESULT (see verify_png()). The saved PNG is opened again and the JPG is
decoded again from disk, then:
  1. FORMAT   the file really is a PNG.
  2. SIZE     width and height match.
  3. MODE     RGB stays RGB, grey stays grey, anything else became RGB.
  4. PIXELS   every pixel is identical to the decoded JPG (after rotation / RGB conversion).
  5. DPI      the stored resolution matches, when the JPG had one.
  6. PROFILE  the colour profile is byte-identical when one was carried, and absent when none
              should be (a converted CMYK JPEG must not keep its CMYK profile).
This catches a faulty write or read. Both decodes use Pillow, so it does not test Pillow's own
JPEG decoder.

Usage:
    .venv/bin/python jpg_to_png.py jpg_to_png_input/photo.jpg
    .venv/bin/python jpg_to_png.py jpg_to_png_input/*.jpg
    .venv/bin/python jpg_to_png.py a.jpg b.jpeg --outdir somewhere
"""

import argparse
import glob
import os
import sys
import time

from PIL import Image, ImageChops, ImageOps

JPG_DIR = "jpg_to_png_input"
PNG_OUT_DIR = "jpg_to_png_output"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
KEEP_MODES = ("RGB", "L")          # modes saved as they are; everything else becomes RGB


def load_expected(path: str):
    """Decode a JPG into what its PNG must contain.

    Returns (image, info) where info records what was done: rotation applied, CMYK converted,
    and the dpi and colour profile that should be carried.
    """
    with Image.open(path) as im:
        if im.format != "JPEG":
            raise ValueError(f"not a JPEG file (it is {im.format or 'unrecognised'})")
        im.load()
        info = {"source_mode": im.mode, "dpi": im.info.get("dpi"),
                "icc": im.info.get("icc_profile"), "orientation": 1, "converted": False}
        try:
            info["orientation"] = int(im.getexif().get(0x0112, 1) or 1)
        except Exception:
            pass
        out = ImageOps.exif_transpose(im)       # applies the tag; the result has no tag
        if out is None:
            out = im.copy()
        out.load()
    if info["orientation"] in (5, 6, 7, 8) and info["dpi"]:
        info["dpi"] = (info["dpi"][1], info["dpi"][0])      # the picture was turned a quarter-turn
    if out.mode not in KEEP_MODES:
        out = out.convert("RGB")
        info["converted"] = True
        info["icc"] = None                      # a CMYK (or other) profile does not describe RGB
    if info["dpi"] is not None and min(info["dpi"]) < 1:
        info["dpi"] = None
    return out, info


def size_text(n: int) -> str:
    return f"{n} B" if n < 1024 else (f"{n / 1024:.1f} KB" if n < 10240 else f"{n / 1024:.0f} KB")


def unique_path(out_dir: str, stem: str) -> str:
    path = os.path.join(out_dir, f"{stem}.png")
    n = 2
    while os.path.exists(path):
        path = os.path.join(out_dir, f"{stem}_{n}.png")
        n += 1
    return path


def verify_png(png_path: str, jpg_path: str):
    """Reopen the PNG, decode the JPG afresh, compare. Returns (problems, details)."""
    problems = []
    try:
        with open(png_path, "rb") as fh:
            head = fh.read(8)
        if head != PNG_MAGIC:
            return ["the saved file is not a PNG (wrong file signature)"], {}
        want, info = load_expected(jpg_path)
        with Image.open(png_path) as got:
            got.load()
            if got.format != "PNG":
                problems.append(f"the saved file reads back as {got.format}, not PNG")
            if got.size != want.size:
                problems.append(f"size is {got.size[0]}x{got.size[1]} but should be {want.size[0]}x{want.size[1]}")
            elif got.mode != want.mode:
                problems.append(f"mode is {got.mode} but should be {want.mode}")
            else:
                diff = ImageChops.difference(got, want).getbbox()
                if diff is not None:
                    problems.append(f"pixels differ from the decoded JPG (first difference inside {diff})")
            if info["dpi"]:
                gd = got.info.get("dpi")
                if not gd or abs(gd[0] - info["dpi"][0]) > 0.5 or abs(gd[1] - info["dpi"][1]) > 0.5:
                    problems.append(f"resolution is {gd} but the JPG had {info['dpi']}")
            if info["icc"] and got.info.get("icc_profile") != info["icc"]:
                problems.append("the colour profile was not carried over unchanged")
            if not info["icc"] and got.info.get("icc_profile"):
                problems.append("the PNG carries a colour profile it should not have "
                                "(for a converted CMYK JPEG that profile is the wrong kind)")
    except Exception as exc:
        problems.append(f"could not verify: {type(exc).__name__}: {exc}")
        return problems, {}
    return problems, info


def run(jpg_path: str, out_dir: str) -> bool:
    print("=" * 72)
    print(jpg_path)
    print("=" * 72)
    if os.path.splitext(jpg_path)[1].lower() not in (".jpg", ".jpeg"):
        print("  SKIPPED: this is not a .jpg / .jpeg file")
        return False

    started = time.time()
    try:
        img, info = load_expected(jpg_path)
    except Exception as exc:
        print(f"  COULD NOT READ: {exc}")
        print("  VERDICT: FAIL -- no PNG written")
        print()
        return False

    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(jpg_path))[0]
    png_path = unique_path(out_dir, stem)
    # The profile is always passed explicitly, even when it is None. Left out, Pillow quietly
    # re-attaches whatever profile the decoded image still carries, which for a converted CMYK
    # JPEG is the CMYK profile: wrong for an RGB picture.
    kwargs = {"icc_profile": info["icc"]}
    if info["dpi"]:
        kwargs["dpi"] = info["dpi"]
    try:
        img.save(png_path, "PNG", **kwargs)
    except Exception as exc:
        print(f"  COULD NOT WRITE: {exc}")
        print("  VERDICT: FAIL -- no PNG written")
        print()
        return False

    notes = []
    if info["orientation"] not in (1, 0):
        notes.append(f"rotated to upright (EXIF orientation {info['orientation']})")
    if info["converted"]:
        notes.append(f"{info['source_mode']} converted to RGB (PNG cannot store {info['source_mode']})")
    notes.append(f"dpi {info['dpi'][0]:.0f}x{info['dpi'][1]:.0f} kept" if info["dpi"] else "no resolution stored in the JPG")
    notes.append("colour profile kept" if info["icc"] else "no colour profile carried")
    notes.append("EXIF not carried over")

    problems, _ = verify_png(png_path, jpg_path)
    a, b = os.path.getsize(jpg_path), os.path.getsize(png_path)
    print(f"  -> {png_path}")
    print(f"  {img.size[0]}x{img.size[1]}px, {img.mode}; {size_text(a)} -> {size_text(b)} ({b / a:.1f}x)")
    for n in notes:
        print(f"       - {n}")
    print()
    print("  --- verification (the saved PNG reopened, the JPG decoded again) ---")
    if problems:
        for p in problems:
            print(f"  - {p}")
    else:
        print("  PNG signature, size, mode, every pixel, resolution and profile all match")
    print(f"  {time.time() - started:.2f}s")
    print(f"  VERDICT: {'PASS -- converted and verified' if not problems else 'FAIL -- see the problems above'}")
    print()
    return not problems


parser = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("images", nargs="+", help="JPG / JPEG file(s), globs allowed")
parser.add_argument("--outdir", default=PNG_OUT_DIR,
                    help=f"where the PNG files go (default {PNG_OUT_DIR})")
args = parser.parse_args()

paths = []
for pattern in args.images:
    paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern])

ok = fail = 0
for path in paths:
    if not os.path.exists(path):
        print(f"skipping {path}: not found")
        continue
    if run(path, args.outdir):
        ok += 1
    else:
        fail += 1

if len(paths) > 1:
    print("=" * 72)
    print(f"{ok} succeeded, {fail} failed, {len(paths)} total")

sys.exit(1 if fail else 0)
