"""
jpg_to_tiff.py -- convert JPG / JPEG images to TIFF, one file each or several in one file.

NO MODEL IS INVOLVED. Pillow decodes the JPG and writes the same pixels as a TIFF, in a
fraction of a second per image.

WHAT YOU GET, AND WHAT YOU DO NOT
  The TIFF holds EXACTLY the pixels the JPG shows. It does not bring back detail the JPG already
  lost, and it is usually larger. Size depends on the picture: measured on two JPGs, a page of
  text grew to about 2.8x with LZW, while a flat-colour graphic came out smaller than its JPG.
  The compression is lossless (LZW by default, or Deflate, which is smaller but unreadable in a
  few old viewers). TIFF's own JPEG compression is deliberately not offered: it would compress
  the picture a second time and change its pixels.

THINGS A STRAIGHT CONVERSION GETS WRONG, AND WHAT IS DONE
  CMYK JPEGs   TIFF could store CMYK, but ordinary viewers show it badly, so it is converted to RGB
               (the same choice as jpg_to_png.py) and the report says so. Its colour profile is
               dropped, since it describes CMYK, not RGB.
  Rotation     A phone photo is often stored sideways with a hidden "orientation" tag. The tag is
               applied to the pixels, so the TIFF looks the way the photo looked.
  dpi, colour profile   Kept (for rotated-by-90 photos the two dpi values are swapped).
  EXIF         NOT carried over (camera details, and possibly GPS location). The report says so.
  Output name  photo.jpg -> photo.tiff in the output folder. An existing file is never overwritten:
               the new one becomes photo_2.tiff, photo_3.tiff, and so on.

SEVERAL IMAGES IN ONE FILE. --combine NAME writes every input, in the order given (globs are
sorted by name), as the pages of ONE multi-page TIFF called NAME.tiff. Each page keeps its own
size, colour mode, dpi and profile. A JPG that cannot be read is left out, the report says which,
and the verdict is FAIL.

HOW IT PROVES THE RESULT (see verify_tiff()). The saved TIFF is opened again, every JPG is
decoded again from disk, and for each page:
  1. FILE      it starts with a TIFF signature, and the page count is right.
  2. SIZE      width and height match.        3. MODE   RGB stays RGB, grey stays grey.
  4. PIXELS    every pixel equals the decoded JPG (after rotation / RGB conversion).
  5. DPI       the stored resolution matches, when the JPG had one.
  6. PROFILE   byte-identical when carried, and absent when none should be.
  7. COMPRESSION  the page really uses the compression that was asked for.
Both decodes use Pillow, so this catches a faulty write or read, not a fault in Pillow's JPEG decoder.

Usage:
    .venv/bin/python jpg_to_tiff.py jpg_to_tiff_input/photo.jpg --outdir jpg_to_tiff_output
    .venv/bin/python jpg_to_tiff.py jpg_to_tiff_input/*.jpg --compression deflate
    .venv/bin/python jpg_to_tiff.py jpg_to_tiff_input/*.jpg --combine album
"""

import argparse
import glob
import os
import re
import sys
import time

from PIL import Image, ImageChops, ImageOps, TiffImagePlugin

JPG_DIR = "jpg_to_tiff_input"
TIFF_OUT_DIR = "jpg_to_tiff_output"
KEEP_MODES = ("RGB", "L")                       # saved as they are; everything else becomes RGB
COMPRESSIONS = {"lzw": ("tiff_lzw", 5, "LZW"), "deflate": ("tiff_deflate", 8, "Deflate")}


# --- copied from jpg_to_png.py, on purpose ----------------------------------
# Scripts here run their whole job at import, so they cannot be imported; the project's
# convention is to copy the small shared pieces.

def load_expected(path: str):
    """Decode a JPG into what its TIFF page must contain, plus a record of what was done."""
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
        out = ImageOps.exif_transpose(im)
        if out is None:
            out = im.copy()
        out.load()
    if info["orientation"] in (5, 6, 7, 8) and info["dpi"]:
        info["dpi"] = (info["dpi"][1], info["dpi"][0])
    if out.mode not in KEEP_MODES:
        out = out.convert("RGB")
        info["converted"] = True
        info["icc"] = None
    if info["dpi"] is not None and min(info["dpi"]) < 1:
        info["dpi"] = None
    return out, info


def size_text(n: int) -> str:
    return f"{n} B" if n < 1024 else (f"{n / 1024:.1f} KB" if n < 10240 else f"{n / 1024:.0f} KB")


def unique_path(out_dir: str, stem: str) -> str:
    path = os.path.join(out_dir, f"{stem}.tiff")
    n = 2
    while os.path.exists(path):
        path = os.path.join(out_dir, f"{stem}_{n}.tiff")
        n += 1
    return path

# ---------------------------------------------------------------------------


def safe_stem(name: str) -> str:
    stem = os.path.splitext(os.path.basename(name))[0]
    stem = re.sub(r"[^\w.\- ()]+", "_", stem).strip("._ ") or "combined"
    return stem[:80]


def page_dpi(tags):
    """The resolution a page's OWN tags state, in dots per inch, or None.

    Read from the tags, not from Pillow's .info: after counting a TIFF's pages Pillow leaves the
    last page's values in .info, so a page without a resolution appears to have one.
    """
    x, y, unit = tags.get(282), tags.get(283), tags.get(296)
    if not x or not y:
        return None
    k = 2.54 if unit == 3 else 1.0                  # 3 = per centimetre
    return float(x) * k, float(y) * k


def verify_tiff(tiff_path: str, jpg_paths: list, compression: str):
    """Reopen the TIFF, decode every JPG afresh, compare page by page.

    Returns (problems, per_page) where per_page is one list of problems per page.
    """
    problems, per_page = [], [[] for _ in jpg_paths]
    want_tag = COMPRESSIONS[compression][1]
    try:
        with open(tiff_path, "rb") as fh:
            head = fh.read(4)
        if head not in (b"II*\x00", b"MM\x00*"):
            return ["the saved file is not a TIFF (wrong file signature)"], per_page
        with Image.open(tiff_path) as tif:
            if tif.format != "TIFF":
                problems.append(f"the saved file reads back as {tif.format}, not TIFF")
            frames = getattr(tif, "n_frames", 1)
            if frames != len(jpg_paths):
                problems.append(f"the TIFF has {frames} page(s) but {len(jpg_paths)} were expected")
            for i, jpg in enumerate(jpg_paths):
                if i >= frames:
                    per_page[i].append("this page is missing from the TIFF")
                    continue
                tif.seek(i)
                tif.load()
                want, info = load_expected(jpg)
                bad = per_page[i]
                if tif.size != want.size:
                    bad.append(f"size is {tif.size[0]}x{tif.size[1]} but should be {want.size[0]}x{want.size[1]}")
                elif tif.mode != want.mode:
                    bad.append(f"mode is {tif.mode} but should be {want.mode}")
                else:
                    diff = ImageChops.difference(tif.convert(want.mode), want).getbbox()
                    if diff is not None:
                        bad.append(f"pixels differ from the decoded JPG (first difference inside {diff})")
                gd = page_dpi(tif.tag_v2)
                if info["dpi"]:
                    if not gd or abs(gd[0] - info["dpi"][0]) > 0.5 or abs(gd[1] - info["dpi"][1]) > 0.5:
                        bad.append(f"resolution is {gd} but the JPG had {info['dpi']}")
                elif gd:
                    bad.append(f"the page states a resolution ({gd[0]:.0f}x{gd[1]:.0f}) that the JPG did not have")
                got_icc = tif.tag_v2.get(34675)
                if isinstance(got_icc, str):
                    got_icc = got_icc.encode("latin-1")
                if info["icc"] and got_icc != info["icc"]:
                    bad.append("the colour profile was not carried over unchanged")
                if not info["icc"] and got_icc:
                    bad.append("the page carries a colour profile it should not have")
                tag = tif.tag_v2.get(259)
                if tag != want_tag:
                    bad.append(f"compression tag is {tag} but {COMPRESSIONS[compression][2]} ({want_tag}) was asked for")
    except Exception as exc:
        problems.append(f"could not verify: {type(exc).__name__}: {exc}")
    return problems, per_page


def convert(jpg_paths: list, out_dir: str, stem: str, compression: str):
    """Write the TIFF. Returns (tiff_path, infos, error)."""
    pil_comp = COMPRESSIONS[compression][0]
    images, infos = [], []
    for p in jpg_paths:
        img, info = load_expected(p)
        images.append(img)
        infos.append(info)
    os.makedirs(out_dir, exist_ok=True)
    path = unique_path(out_dir, stem)
    # Every page gets its OWN tags, written explicitly. Passing dpi / icc_profile as ordinary save
    # options is not enough in a multi-page file: measured on Pillow 12.3, a page with no resolution
    # inherited the previous page's 150 dpi. Tags set per page (tiffinfo) do not leak between pages.
    for img, info in zip(images, infos):
        ifd = TiffImagePlugin.ImageFileDirectory_v2()
        if info["dpi"]:
            ifd[282], ifd[283], ifd[296] = float(info["dpi"][0]), float(info["dpi"][1]), 2   # 2 = inch
        if info["icc"]:
            ifd[34675] = info["icc"]
        # Pillow also looks at the profile still attached to the decoded image and writes it if no
        # profile is given. For a converted CMYK JPEG that is the CMYK profile, wrong for RGB. So
        # it is removed from the image, and passed explicitly (None when there is none).
        img.info.pop("icc_profile", None)
        img.encoderinfo = {"compression": pil_comp, "tiffinfo": ifd, "icc_profile": info["icc"]}
    first, rest = images[0], images[1:]
    first.save(path, "TIFF", **({"save_all": True, "append_images": rest} if rest else {}),
               **first.encoderinfo)
    return path, infos


def notes_for(info):
    n = []
    if info["orientation"] not in (1, 0):
        n.append(f"rotated to upright (EXIF orientation {info['orientation']})")
    if info["converted"]:
        n.append(f"{info['source_mode']} converted to RGB")
    n.append(f"dpi {info['dpi'][0]:.0f}x{info['dpi'][1]:.0f} kept" if info["dpi"] else "no resolution stored in the JPG")
    n.append("colour profile kept" if info["icc"] else "no colour profile carried")
    return n


def run(jpg_paths: list, out_dir: str, stem: str, compression: str, combine: bool) -> bool:
    label = f"{len(jpg_paths)} images -> one multi-page TIFF" if combine else jpg_paths[0]
    print("=" * 72)
    print(label)
    print("=" * 72)
    started = time.time()

    readable, skipped = [], []
    for p in jpg_paths:
        if os.path.splitext(p)[1].lower() not in (".jpg", ".jpeg"):
            skipped.append((p, "not a .jpg / .jpeg file"))
            continue
        try:
            load_expected(p)
            readable.append(p)
        except Exception as exc:
            skipped.append((p, str(exc)))
    for p, why in skipped:
        print(f"  LEFT OUT {p}: {why}")
    if not readable:
        print("  VERDICT: FAIL -- no TIFF written")
        print()
        return False

    try:
        tiff_path, infos = convert(readable, out_dir, stem, compression)
    except Exception as exc:
        print(f"  COULD NOT WRITE: {type(exc).__name__}: {exc}")
        print("  VERDICT: FAIL -- no TIFF written")
        print()
        return False

    print(f"  -> {tiff_path}")
    total_in = sum(os.path.getsize(p) for p in readable)
    total_out = os.path.getsize(tiff_path)
    print(f"  {len(readable)} page(s), {COMPRESSIONS[compression][2]}; "
          f"{size_text(total_in)} -> {size_text(total_out)} ({total_out / total_in:.1f}x)")
    problems, per_page = verify_tiff(tiff_path, readable, compression)
    with Image.open(tiff_path) as t:
        for i, (p, info) in enumerate(zip(readable, infos)):
            t.seek(i)
            tag = "PASS" if not per_page[i] else "FAIL"
            print(f"  page {i + 1}: {os.path.basename(p)}  {t.size[0]}x{t.size[1]}px, {t.mode}  [{tag}]")
            for n in notes_for(info):
                print(f"       - {n}")
            print("       - EXIF not carried over")
            for pb in per_page[i]:
                print(f"       ! {pb}")
    print()
    print("  --- verification (the saved TIFF reopened, every JPG decoded again) ---")
    for pb in problems:
        print(f"  - {pb}")
    n_bad = sum(1 for x in per_page if x)
    if not problems and not n_bad:
        print("  signature, page count, size, mode, every pixel, resolution, profile and compression all match")
    ok = not problems and not n_bad and not skipped
    print(f"  {time.time() - started:.2f}s")
    if skipped and not problems and not n_bad:
        print(f"  VERDICT: FAIL -- {len(skipped)} input(s) were left out (see above); the TIFF holds the rest")
    else:
        print(f"  VERDICT: {'PASS -- converted and verified' if ok else 'FAIL -- see the problems above'}")
    print()
    return ok


parser = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("images", nargs="+", help="JPG / JPEG file(s), globs allowed")
parser.add_argument("--outdir", default=TIFF_OUT_DIR, help=f"where the TIFF files go (default {TIFF_OUT_DIR})")
parser.add_argument("--compression", choices=sorted(COMPRESSIONS), default="lzw",
                    help="lossless compression: lzw (default, opens everywhere) or deflate "
                         "(smaller, but a few old viewers cannot open it)")
parser.add_argument("--combine", metavar="NAME",
                    help="put every input into ONE multi-page TIFF called NAME.tiff, in the order given")
args = parser.parse_args()

paths = []
for pattern in args.images:
    paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern])

existing = []
for p in paths:
    if os.path.exists(p):
        existing.append(p)
    else:
        print(f"skipping {p}: not found")

ok = fail = 0
if args.combine:
    if existing:
        if run(existing, args.outdir, safe_stem(args.combine), args.compression, True):
            ok += 1
        else:
            fail += 1
else:
    for p in existing:
        if run([p], args.outdir, safe_stem(p), args.compression, False):
            ok += 1
        else:
            fail += 1
    if len(existing) > 1:
        print("=" * 72)
        print(f"{ok} succeeded, {fail} failed, {len(existing)} total")

sys.exit(1 if fail else 0)
