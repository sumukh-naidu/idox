"""FR-AI-01 AI Document Workflow & Action Automation: tools, prompt, sidebar entry."""

from . import operations, reading

FILE_ID = {"type": "string", "description": "ID of the PDF, e.g. f_1a2b3c4d5e6f"}
PAGES = {"type": "string", "description": 'pages as ranges, e.g. "1-3, 7, 9-end"'}

INFO = {
    "id": "FR-AI-01",
    "title": "Workflow Automation",
    "status": "ready",
    "description": "Several document operations in one request, each step working on the previous "
                   "step's output: merge, split by page ranges, extract, rotate and reorder pages, "
                   "compress, rename. Also summaries and tasks/deadlines read from the document; every "
                   "task must quote the page it comes from or it is dropped.",
    "samples": ["merge_a.pdf", "merge_b.pdf", "contract_12p.pdf", "project_memo.pdf", "scanned_notice.pdf"],
    "examples": [
        {"file": "merge_a.pdf", "also": ["merge_b.pdf"],
         "prompt": "Merge these PDF files, remove blank pages, and compress the final document.",
         "expect": "Adds both files. Merges A then B (6 pages), removes the blank page (5 pages), then "
                   "compresses: the file gets much smaller because B has a 300 dpi image."},
        {"file": "contract_12p.pdf",
         "prompt": "Split this document into separate files for pages 1-3, 4-6 and 7-12, and name them "
                   "Definitions, Payment Terms and Schedules.",
         "expect": "Three new files named Definitions.pdf (3 pages), Payment Terms.pdf (3 pages) and "
                   "Schedules.pdf (6 pages)."},
        {"file": "project_memo.pdf",
         "prompt": "Identify the tasks and deadlines in this document.",
         "expect": "Four deadlines: 15 October 2026, 20/10/2026, 31 Oct 2026, 5 November 2026. Must NOT "
                   "list 1 March 2019 (founding date) or 12 June 2025 (past audit). Listing the "
                   "1 December 2026 meeting is acceptable."},
        {"file": "project_memo.pdf",
         "prompt": "Create a short summary of this document.",
         "expect": "A few sentences on the Project Orion kick-off memo and its action items, using only "
                   "what the memo says."},
        {"file": "scanned_notice.pdf",
         "prompt": "What deadlines are mentioned in this notice?",
         "expect": "Read by OCR (it is a scan). Two deadlines: 18 October 2026 and 25 October 2026. "
                   "2 October 2026 is the issue date, not a deadline."},
        {"file": "contract_12p.pdf",
         "prompt": "Extract pages 4 to 6 into a new file, then rotate its first page by 90 degrees.",
         "expect": "A 3-page file holding the Payment Terms pages, then a copy with its first page "
                   "turned clockwise."},
    ],
}

PROMPT = (
    "Workflows: do every requested step, in order. Merge in the order the user listed, else upload order; "
    "compress last. For anything about a document's content (summary, tasks, deadlines), call read_document "
    "first and follow its next_step."
)

TOOLS = {
    "merge_pdfs": {
        "fn": operations.merge_pdfs,
        "description": "Write a NEW PDF joining several PDFs in the order given. Reports which page range "
                       "of the merged file came from which input.",
        "parameters": {"type": "object", "properties": {
            "file_ids": {"type": "array", "items": {"type": "string"}, "description": "IDs in merge order"},
            "name": {"type": "string", "description": "optional name for the merged file"},
        }, "required": ["file_ids"]},
    },
    "split_pdf": {
        "fn": operations.split_pdf,
        "description": "Write one NEW PDF per part, each holding the given page ranges, optionally named. "
                       "Reports any pages left out of every part.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID,
            "parts": {"type": "array", "description": 'e.g. [{"pages": "1-3", "name": "Definitions"}]',
                      "items": {"type": "object", "properties": {
                          "pages": PAGES, "name": {"type": "string", "description": "optional file name"}},
                          "required": ["pages"]}},
        }, "required": ["file_id", "parts"]},
    },
    "extract_pages": {
        "fn": operations.extract_pages,
        "description": "Write a NEW PDF holding only the given pages, in the order given.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID, "pages": PAGES,
            "name": {"type": "string", "description": "optional name for the new file"},
        }, "required": ["file_id", "pages"]},
    },
    "rotate_pages": {
        "fn": operations.rotate_pages,
        "description": "Write a NEW PDF with the given pages rotated. Positive degrees turn clockwise.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID,
            "pages": {"type": "string", "description": 'pages as ranges, e.g. "2, 4", or "all"'},
            "degrees": {"type": "integer", "enum": [90, 180, 270, -90]},
        }, "required": ["file_id", "pages", "degrees"]},
    },
    "reorder_pages": {
        "fn": operations.reorder_pages,
        "description": "Write a NEW PDF with the pages in a new order. The order must list every page "
                       'exactly once, e.g. "3, 1-2, 4-end" moves page 3 to the front.',
        "parameters": {"type": "object", "properties": {"file_id": FILE_ID, "order": PAGES},
                       "required": ["file_id", "order"]},
    },
    "compress_pdf": {
        "fn": operations.compress_pdf,
        "description": "Write a NEW, smaller PDF by downsampling large images and compacting the file. "
                       "Text and pages are kept and checked. If it would save under 2%, no file is "
                       "written. level 'strong' shrinks images further at some loss of quality.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID, "level": {"type": "string", "enum": ["standard", "strong"]},
        }, "required": ["file_id"]},
    },
    "rename_file": {
        "fn": operations.rename_file,
        "description": "Make a copy of a file under a new name, for download.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID, "new_name": {"type": "string"},
        }, "required": ["file_id", "new_name"]},
    },
    "read_document": {
        "fn": reading.read_document,
        "description": "Return a PDF's text page by page: from the text layer, or by OCR for scanned "
                       "pages. Capped per call; if 'more' is present, call again for the rest. Read-only.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID,
            "pages": {"type": "string", "description": 'optional, e.g. "1-5"; default all pages'},
        }, "required": ["file_id"]},
    },
    "verify_tasks": {
        "fn": reading.verify_tasks,
        "description": "Check tasks and deadlines against the document before listing them. Returns which "
                       "items are verified and which were rejected, and why. Read-only.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID,
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "task": {"type": "string"},
                "deadline": {"type": ["string", "null"], "description": "as written in the document"},
                "quote": {"type": "string", "description": "the exact sentence it comes from"},
                "page": {"type": "integer"},
            }, "required": ["task", "quote"]}},
        }, "required": ["file_id", "items"]},
    },
}
