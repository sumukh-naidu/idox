"""FR-PRD-105 Smart PDF Accessibility: tools, prompt, sidebar entry."""

from . import checks, fixes, images

FILE_ID = {"type": "string", "description": "ID of the PDF, e.g. f_1a2b3c4d5e6f"}

INFO = {
    "id": "FR-PRD-105",
    "title": "Smart PDF Accessibility",
    "status": "ready",
    "description": "Checks what a screen reader needs: tags, declared language, title, embedded fonts, "
                   "image descriptions, real text instead of scans, reading order, bookmarks, permissions. "
                   "Fixes what is safe (OCR text layer for scans, language, title) and drafts alt text for "
                   "images with the vision model. Tags, reading order and fonts are reported, not faked.",
    "samples": ["access_report.pdf", "access_scanned.pdf", "access_tagged.pdf"],
    "examples": [
        {"file": "access_report.pdf",
         "prompt": "Check this PDF for accessibility issues.",
         "expect": "Fails: tags, language, title, fonts embedded, image descriptions, reading order. "
                   "Passes: real text, assistive access. Bookmarks not required (2 pages)."},
        {"file": "access_report.pdf",
         "prompt": "Identify problems with reading order, document tags, and image descriptions.",
         "expect": "No tags, so reading order is undefined. Suggested alt text for both images: a bar "
                   "chart of orders per quarter Q1-Q4, and a flowchart Order -> Pack -> Ship."},
        {"file": "access_scanned.pdf",
         "prompt": "Help improve this PDF so that its content is more accessible.",
         "expect": "New file with an OCR text layer on pages 1-2, language 'en', title 'Site Inspection "
                   "Report (Sample)'. Still failing (out of scope): tags and reading order."},
        {"file": "access_tagged.pdf",
         "prompt": "Check this PDF for accessibility issues.",
         "expect": "Everything passes; bookmarks are not required for 1 page."},
    ],
}

PROMPT = (
    "Accessibility: to check a file, call check_accessibility. To improve, fix or make it more accessible, "
    "call fix_accessibility directly, without checking first or asking. Never claim tags, reading order or "
    "fonts were fixed; they need the document re-authored."
)

TOOLS = {
    "check_accessibility": {
        "fn": checks.check_accessibility,
        "description": "Check a PDF against the accessibility basics a screen reader needs: tags, declared "
                       "language, title, embedded fonts, image descriptions, real text instead of scans, "
                       "reading order, bookmarks, permissions. Each check is pass/fail/warn/n/a with a "
                       "reason and how it can be fixed. Read-only.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID,
            "describe_images": {"type": "boolean",
                                "description": "true if the user mentions images or image descriptions: also "
                                               "drafts alt text for undescribed images in the same call"},
        }, "required": ["file_id"]},
    },
    "fix_accessibility": {
        "fn": fixes.fix_accessibility,
        "description": "Write a NEW PDF with the safe fixes: OCR text layer on scanned pages (if any), declared "
                       "language and a title viewers show. Checks before and after by itself and reports what "
                       "now passes and what still fails. Original kept.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID,
            "title": {"type": "string", "description": "default: largest text on page 1"},
            "language": {"type": "string", "description": "e.g. 'en'; default: detected"},
            "add_text_layer": {"type": "boolean"},
        }, "required": ["file_id"]},
    },
    "describe_images": {
        "fn": images.describe_images,
        "description": "Draft alt text for the content images in a PDF using the vision model. Suggestions "
                       "for the author: nothing is written into the file. Page scans are skipped. Read-only.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID,
            "max_images": {"type": "integer", "description": "default 6"},
        }, "required": ["file_id"]},
    },
}
