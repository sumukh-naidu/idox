"""Page operations shared by several features. Every one writes a new file and checks its own result."""

from core.pdfutil import ToolError, check_pages, derived_name, open_pdf, origin_of, page_ranges, save_new


def _from_original(meta: dict, pages) -> list[int]:
    """Translate page numbers of the uploaded file into this file's page numbers."""
    origin = origin_of(meta)
    roots = {r for r, _ in origin}
    if len(roots) > 1:
        raise ToolError("this file combines several uploads, so 'original' page numbers are "
                        "ambiguous; use this file's own page numbers")
    where = {p: i + 1 for i, (_, p) in enumerate(origin)}
    wanted = check_pages(pages, 10**6)       # integers only; presence is checked below
    gone = [p for p in wanted if p not in where]
    if gone:
        raise ToolError(f"original page(s) {page_ranges(gone)} are no longer in this file "
                        f"(already removed). This file holds original pages "
                        f"{page_ranges(sorted(where))}.")
    return [where[p] for p in wanted]


def remove_pages(file_id: str, pages: list[int], numbering: str = "this_file") -> dict:
    meta, doc = open_pdf(file_id)
    if numbering not in ("this_file", "original"):
        raise ToolError("numbering must be 'this_file' or 'original'")
    with doc:
        before = doc.page_count
        drop = _from_original(meta, pages) if numbering == "original" else check_pages(pages, before)
        if len(drop) == before:
            raise ToolError("that would remove every page; nothing would be left")
        keep = [i for i in range(before) if i + 1 not in drop]
        origin = origin_of(meta)
        doc.select(keep)
        out = save_new(doc, derived_name(meta["name"], "pages-removed"), [file_id], "remove_pages",
                       page_origin=[origin[i] for i in keep])

    if out["pages"] != before - len(drop):
        raise ToolError(f"verification failed: expected {before - len(drop)} pages, got {out['pages']}")
    return {
        "new_file_id": out["id"],
        "removed_pages": page_ranges(drop) + (" (this file's numbering)" if numbering == "original" else ""),
        "removed_original_pages": page_ranges([origin[i - 1][1] for i in drop]),
        "pages_before": before,
        "pages_after": out["pages"],
        "new_file_holds_original_pages": page_ranges([o[1] for o in out["page_origin"]]),
        "files_created": [out],
    }
