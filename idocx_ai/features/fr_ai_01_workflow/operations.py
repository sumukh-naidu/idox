"""FR-AI-01 document operations: merge, split, extract, rotate, reorder, compress, rename.

Each one writes a NEW file, keeps track of which original page every page came
from, and checks its own result before reporting success.
"""

import io

import pymupdf
from PIL import Image

from core import store
from core.pdfutil import (ToolError, derived_name, open_pdf, origin_of, page_ranges, parse_page_spec,
                          save_new)

# level -> (downsample images above this dpi, to this dpi, JPEG quality)
COMPRESSION = {"standard": (200, 150, 75), "strong": (120, 96, 60)}
MIN_SAVING = 0.02   # below a 2% saving, keep the original rather than write a near-identical copy


def _pdf_name(name) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


def _copy_pages(doc, pages: list[int]):
    sub = pymupdf.open()
    for p in pages:
        sub.insert_pdf(doc, from_page=p - 1, to_page=p - 1)
    return sub


def merge_pdfs(file_ids: list[str], name: str | None = None) -> dict:
    if not isinstance(file_ids, list) or len(file_ids) < 2:
        raise ToolError("give at least two file IDs, in the order they should be merged")
    out, origin, parts = pymupdf.open(), [], []
    for fid in file_ids:
        meta, doc = open_pdf(fid)
        with doc:
            out.insert_pdf(doc)
        origin += origin_of(meta)
        parts.append({"file_id": fid, "name": meta["name"], "pages": meta["pages"]})
    with out:
        new = save_new(out, _pdf_name(name) or derived_name(parts[0]["name"], "merged"), file_ids,
                       "merge_pdfs", page_origin=origin)

    expected = sum(p["pages"] for p in parts)
    if new["pages"] != expected:
        raise ToolError(f"verification failed: merged file has {new['pages']} pages, expected {expected}")
    start = 1
    for p in parts:
        p["pages_in_merged_file"] = f"{start}-{start + p['pages'] - 1}"
        start += p["pages"]
    return {"new_file_id": new["id"], "name": new["name"], "pages": new["pages"], "order": parts,
            "files_created": [new]}


def split_pdf(file_id: str, parts: list[dict]) -> dict:
    meta, doc = open_pdf(file_id)
    with doc:
        n = doc.page_count
        if not isinstance(parts, list) or not parts:
            raise ToolError('parts must be a list like [{"pages": "1-3", "name": "Definitions"}, ...]')
        plans = []
        for part in parts:                       # validate every part before writing any file
            if not isinstance(part, dict) or "pages" not in part:
                raise ToolError('each part needs "pages", e.g. {"pages": "1-3", "name": "Definitions"}')
            plans.append((parse_page_spec(part["pages"], n), _pdf_name(part.get("name"))))

        origin, created, summary, covered = origin_of(meta), [], [], set()
        for i, (pages, name) in enumerate(plans, 1):
            with _copy_pages(doc, pages) as sub:
                f = save_new(sub, name or derived_name(meta["name"], f"part{i}"), [file_id], "split_pdf",
                             page_origin=[origin[p - 1] for p in pages])
            if f["pages"] != len(pages):
                raise ToolError(f"verification failed: part {i} has {f['pages']} pages, expected {len(pages)}")
            created.append(f)
            covered |= set(pages)
            summary.append({"new_file_id": f["id"], "name": f["name"], "pages": page_ranges(pages),
                            "page_count": f["pages"]})

    missing = [p for p in range(1, n + 1) if p not in covered]
    return {"parts": summary, "pages_not_in_any_part": page_ranges(missing) if missing else "none",
            "files_created": created}


def extract_pages(file_id: str, pages: str, name: str | None = None) -> dict:
    meta, doc = open_pdf(file_id)
    with doc:
        wanted = parse_page_spec(pages, doc.page_count)
        origin = origin_of(meta)
        with _copy_pages(doc, wanted) as sub:
            new = save_new(sub, _pdf_name(name) or derived_name(meta["name"], "extract"), [file_id],
                           "extract_pages", page_origin=[origin[p - 1] for p in wanted])
    if new["pages"] != len(wanted):
        raise ToolError(f"verification failed: expected {len(wanted)} pages, got {new['pages']}")
    return {"new_file_id": new["id"], "name": new["name"], "pages_extracted": ", ".join(map(str, wanted)),
            "page_count": new["pages"], "files_created": [new]}


def rotate_pages(file_id: str, pages: str, degrees: int) -> dict:
    if degrees not in (90, 180, 270, -90, -180, -270):
        raise ToolError("degrees must be 90, 180 or 270 (clockwise), or negative for anticlockwise")
    meta, doc = open_pdf(file_id)
    with doc:
        n = doc.page_count
        chosen = sorted(set(range(1, n + 1) if str(pages).strip().lower() == "all"
                            else parse_page_spec(pages, n)))
        for p in chosen:
            doc[p - 1].set_rotation((doc[p - 1].rotation + degrees) % 360)
        want = {p: doc[p - 1].rotation for p in chosen}
        new = save_new(doc, derived_name(meta["name"], "rotated"), [file_id], "rotate_pages",
                       page_origin=origin_of(meta))

    _, path = store.get_file(new["id"])
    with pymupdf.open(path) as check:
        wrong = [p for p, r in want.items() if check[p - 1].rotation != r]
    if wrong:
        raise ToolError(f"verification failed: pages {wrong} did not take the new rotation")
    return {"new_file_id": new["id"], "rotated_pages": page_ranges(chosen),
            "degrees_clockwise": degrees % 360, "files_created": [new]}


def reorder_pages(file_id: str, order: str) -> dict:
    meta, doc = open_pdf(file_id)
    with doc:
        n = doc.page_count
        seq = parse_page_spec(order, n)
        if sorted(seq) != list(range(1, n + 1)):
            seen, dup = set(), set()
            for p in seq:
                (dup if p in seen else seen).add(p)
            missing = [p for p in range(1, n + 1) if p not in seen]
            raise ToolError(f"order must list every page exactly once; missing {page_ranges(missing)}, "
                            f"repeated {page_ranges(sorted(dup)) if dup else 'none'}")
        origin = origin_of(meta)
        doc.select([p - 1 for p in seq])
        new = save_new(doc, derived_name(meta["name"], "reordered"), [file_id], "reorder_pages",
                       page_origin=[origin[p - 1] for p in seq])
    if new["pages"] != n:
        raise ToolError(f"verification failed: expected {n} pages, got {new['pages']}")
    return {"new_file_id": new["id"], "new_order": ", ".join(map(str, seq)), "files_created": [new]}


def _downsample_images(doc, threshold: int, target: int, quality: int) -> int:
    """Re-encode images drawn above `threshold` dpi down to `target` dpi.

    Resolution is measured from how large each image is drawn on the page.
    MuPDF's own rewrite_images judges by embedded resolution metadata instead,
    which left a 300 dpi test image untouched because its file claimed 96 dpi.
    """
    done, changed = set(), 0
    for page in doc:
        for xref, smask, w, h, bpc, cs, *_ in page.get_images(full=True):
            if xref in done:
                continue
            done.add(xref)
            # Transparency, 1-bit scans and CMYK risk damage when re-encoded as JPEG.
            if smask or bpc == 1 or "CMYK" in cs:
                continue
            rects = page.get_image_rects(xref)
            if not rects:
                continue
            shown = max(rects, key=lambda r: r.width * r.height)
            dpi = min(w / (shown.width / 72), h / (shown.height / 72))
            if dpi <= threshold:
                continue
            raw = doc.extract_image(xref)
            if not raw:
                continue
            im = Image.open(io.BytesIO(raw["image"]))
            im = im.convert("L" if im.mode in ("L", "LA", "1") else "RGB")
            k = target / dpi
            im = im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality)
            if buf.tell() < len(raw["image"]):
                page.replace_image(xref, stream=buf.getvalue())
                changed += 1
    return changed


def compress_pdf(file_id: str, level: str = "standard") -> dict:
    if level not in COMPRESSION:
        raise ToolError(f"level must be one of {list(COMPRESSION)}")
    threshold, target, quality = COMPRESSION[level]
    meta, doc = open_pdf(file_id)
    with doc:
        text_before = [p.get_text() for p in doc]
        images = _downsample_images(doc, threshold, target, quality)
        try:
            doc.subset_fonts()
        except Exception:
            pass                                  # a font that cannot be subset only costs size
        data = doc.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, use_objstms=1)

    before, after = meta["size"], len(data)
    if after > before * (1 - MIN_SAVING):
        return {"nothing_to_do": True, "size_kb": round(before / 1024),
                "note": "already compact: compressing would save under 2%, so no new file was written"}
    new = store.save_file(data, derived_name(meta["name"], "compressed"), meta["pages"], "compress_pdf",
                          [file_id], page_origin=origin_of(meta))
    _, path = store.get_file(new["id"])
    with pymupdf.open(path) as check:
        if check.page_count != meta["pages"] or [p.get_text() for p in check] != text_before:
            raise ToolError("verification failed: the compressed file lost pages or text")
    return {"new_file_id": new["id"], "size_kb_before": round(before / 1024),
            "size_kb_after": round(after / 1024), "saved_percent": round(100 * (1 - after / before)),
            "images_downsampled": images, "level": level,
            "verified": "same pages and same text as before", "files_created": [new]}


def rename_file(file_id: str, new_name: str) -> dict:
    name = _pdf_name(new_name)
    if not name:
        raise ToolError("new_name must not be empty")
    try:
        meta, path = store.get_file(file_id)
    except store.NotFound as e:
        raise ToolError(str(e))
    new = store.save_file(path.read_bytes(), name, meta["pages"], "rename_file", [file_id],
                          page_origin=origin_of(meta))
    return {"new_file_id": new["id"], "old_name": meta["name"], "new_name": new["name"],
            "note": "a renamed copy; the file under the old name is unchanged", "files_created": [new]}
