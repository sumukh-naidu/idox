"""The tool registry the agent sees: shared tools here, plus each feature's own.

Each tool returns a JSON-able dict. A tool that writes a file returns it under
"files_created" so the agent loop can announce it and attach it to the session.
To add a feature: give it a tools.py with TOOLS and PROMPT, and list it in FEATURES.
"""

from core import pages
from core.pdfutil import STANDARD_INFO_KEYS, ToolError, info_keys, open_pdf, page_ranges
from features.fr_prd_104_cleanup import tools as fr_prd_104

FEATURES = [fr_prd_104]

FILE_ID = {"type": "string", "description": "ID of the PDF, e.g. f_1a2b3c4d5e6f"}


def pdf_info(file_id: str) -> dict:
    meta, doc = open_pdf(file_id)
    with doc:
        no_text = [i + 1 for i, page in enumerate(doc) if not page.get_text("text").strip()]
        first = doc[0].rect if doc.page_count else None
        md = doc.metadata or {}
        return {
            "file_id": file_id,
            "name": meta["name"],
            "pages": doc.page_count,
            "size_kb": round(meta["size"] / 1024),
            "pdf_version": md.get("format"),
            "page_size_pt": [round(first.width), round(first.height)] if first else None,
            "metadata": {k: v for k, v in md.items() if v and k != "format"},
            "custom_metadata_fields": [k for k in info_keys(doc) if k not in STANDARD_INFO_KEYS],
            "has_xmp_metadata": bool(doc.get_xml_metadata()),
            "pages_without_text_layer": page_ranges(no_text),
        }


CORE_TOOLS = {
    "pdf_info": {
        "fn": pdf_info,
        "description": "Facts about one PDF: page count, size, metadata (including custom fields and "
                       "XMP), and which pages have no text layer. Read-only.",
        "parameters": {"type": "object", "properties": {"file_id": FILE_ID}, "required": ["file_id"]},
    },
    "remove_pages": {
        "fn": pages.remove_pages,
        "description": "Write a NEW PDF without the given pages; the original is kept. Remove all pages "
                       "you can in a SINGLE call. After a removal, page numbers in the new file shift; to "
                       "remove more pages from a file that already had pages removed, pass the page numbers "
                       "of the uploaded file with numbering='original' and the tool translates them.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID,
            "pages": {"type": "array", "items": {"type": "integer"}, "description": "pages to remove, e.g. [3, 7]"},
            "numbering": {"type": "string", "enum": ["this_file", "original"],
                          "description": "'this_file' (default): page numbers of file_id. 'original': page "
                                         "numbers of the uploaded file the user has been talking about."},
        }, "required": ["file_id", "pages"]},
    },
}

TOOLS = dict(CORE_TOOLS)
for feature in FEATURES:
    clash = set(TOOLS) & set(feature.TOOLS)
    if clash:
        raise RuntimeError(f"tool name clash in {feature.__name__}: {clash}")
    TOOLS.update(feature.TOOLS)

PROMPTS = [feature.PROMPT for feature in FEATURES]


def schemas() -> list[dict]:
    return [{"type": "function",
             "function": {"name": n, "description": t["description"], "parameters": t["parameters"]}}
            for n, t in TOOLS.items()]


def run(name: str, args) -> dict:
    if name not in TOOLS:
        return {"ok": False, "error": f"unknown tool '{name}'. Available: {', '.join(TOOLS)}"}
    if not isinstance(args, dict):
        return {"ok": False, "error": "arguments must be a JSON object"}
    try:
        return {"ok": True, "result": TOOLS[name]["fn"](**args)}
    except ToolError as e:
        return {"ok": False, "error": str(e)}
    except TypeError as e:
        return {"ok": False, "error": f"bad arguments: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
