"""FR-PRD-104 One-click Document Cleanup: the tools the agent gets, and its part of the prompt."""

from . import cleanup

FILE_ID = {"type": "string", "description": "ID of the PDF, e.g. f_1a2b3c4d5e6f"}

# What the test page's sidebar shows. Sample files are read from this folder's input/.
INFO = {
    "id": "FR-PRD-104",
    "title": "One-click Cleanup",
    "status": "ready",
    "description": "Finds blank pages, duplicate pages and nearly empty pages, and strips metadata. "
                   "Writes a new file and never changes the original. Borderline pages are shown to "
                   "you and only removed when you confirm.",
    "samples": ["cleanup_digital.pdf", "cleanup_scanned.pdf", "cleanup_control.pdf"],
    "examples": [
        {"file": "cleanup_digital.pdf",
         "prompt": "Clean this PDF by removing blank pages and duplicate pages.",
         "expect": "Removes pages 3, 5, 7, 8 and 10 (12 -> 7 pages). Asks about page 9 (one line) and "
                   "page 12 (header and footer only). Metadata is left alone: it was not asked for."},
        {"file": "cleanup_scanned.pdf",
         "prompt": "Clean up this scanned document and remove unnecessary metadata where supported.",
         "expect": "Removes pages 2 and 5 and strips the metadata, then asks about page 4 (a re-scan of "
                   "page 1) and page 6 (stamp only). Reply 'yes, remove 4 and 6' -> 2 pages left."},
        {"file": "cleanup_control.pdf",
         "prompt": "Automatically identify and apply the available document cleanup actions.",
         "expect": "Nothing to clean: no blank or duplicate pages, only a title. No new file is created."},
    ],
}

PROMPT = (
    "Cleanup: call clean_pdf once. For 'clean this' or 'all cleanup actions' set remove_blank, "
    "remove_duplicates and strip_metadata; otherwise set only what was asked. Report only what it returns."
)

TOOLS = {
    "find_blank_pages": {
        "fn": cleanup.find_blank_pages,
        "description": "Find blank pages: empty pages, scanned pages with nothing on them, whitespace-only "
                       "pages, 'intentionally left blank' notices, and header/footer-only pages. Also lists "
                       "nearly empty pages (a line or two, or a small stamp): those are NOT blank and may hold "
                       "a sign-off or signature. Read-only. Pages under ask_user_first must not be removed "
                       "until the user confirms: show each one with its reason and ask.",
        "parameters": {"type": "object", "properties": {"file_id": FILE_ID}, "required": ["file_id"]},
    },
    "find_duplicate_pages": {
        "fn": cleanup.find_duplicate_pages,
        "description": "Find pages that repeat an earlier page: identical pages, and re-scans of the same "
                       "page. Blank pages are not reported here. Read-only. Pages under ask_user_first are "
                       "likely but not certain duplicates: ask the user before removing them.",
        "parameters": {"type": "object", "properties": {"file_id": FILE_ID}, "required": ["file_id"]},
    },
    "clean_pdf": {
        "fn": cleanup.clean_pdf,
        "description": "One-click cleanup in ONE call and ONE new file (original kept): finds and removes blank "
                       "and/or duplicate pages itself (only the safe ones), strips metadata (title kept), and "
                       "returns the pages to ask the user about. Also removes pages the user confirms later: "
                       "remove_pages with numbering='original' on the latest file.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID,
            "remove_blank": {"type": "boolean"},
            "remove_duplicates": {"type": "boolean"},
            "strip_metadata": {"type": "boolean"},
            "remove_pages": {"type": "array", "items": {"type": "integer"}, "description": "pages the user confirmed"},
            "numbering": {"type": "string", "enum": ["this_file", "original"]},
        }, "required": ["file_id"]},
    },
}
