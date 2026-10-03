"""FR-PRD-104 One-click Document Cleanup: the tools the agent gets, and its part of the prompt."""

from . import cleanup

FILE_ID = {"type": "string", "description": "ID of the PDF, e.g. f_1a2b3c4d5e6f"}

PROMPT = (
    "Cleanup covers three things: blank pages, duplicate pages and metadata. When asked to clean a file "
    "or apply all cleanup actions, do all three; when asked for only some, do only those. Find blank and "
    "duplicate pages first, then remove every page that is safe to remove in ONE remove_pages call. Do not "
    "remove pages listed under ask_user_first; tell the user what they are and ask. Do not hold back "
    "anything else that was requested while you wait: strip metadata now if it was asked for."
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
    "strip_metadata": {
        "fn": cleanup.strip_metadata,
        "description": "Write a NEW PDF with document metadata removed: author, creator tool, producer, "
                       "dates, subject, keywords, custom fields and XMP. Keeps the title unless keep_title "
                       "is false. The original is kept.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID,
            "keep_title": {"type": "boolean", "description": "keep the document title (default true)"},
        }, "required": ["file_id"]},
    },
}
