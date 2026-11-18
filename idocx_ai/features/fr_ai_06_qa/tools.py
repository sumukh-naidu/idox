"""FR-AI-06 Document Q&A: tools, prompt, sidebar entry."""

from . import qa, summary

check_answer = qa.check_answer

INFO = {
    "id": "FR-AI-06",
    "title": "Document Q&A and summary",
    "status": "ready",
    "description": "Answers a question about one thing in a PDF, using only the document's own text (scanned pages "
                   "are read by OCR) and naming the page the answer came from, or summarises the whole document "
                   "(long ones are read in parts so no page is skipped). Every answer is checked against the text. "
                   "For questions, long documents are narrowed to the "
                   "pages that best match the question.",
    "samples": ["service_report.pdf"],
    "examples": [
        {"file": "service_report.pdf", "prompt": "What is the contract number?",
         "expect": "SR-2026-0457, from page 1."},
        {"file": "service_report.pdf", "prompt": "How many support tickets were closed in October?",
         "expect": "412, from page 2."},
        {"file": "service_report.pdf", "prompt": "Summarise this document.",
         "expect": "A short overview and bullets covering all 3 pages: contract and period, the October ticket "
                   "figures, the service-level target and approval, the next review. Marked as verified."},
        {"file": "service_report.pdf", "prompt": "Who has to approve a change to the service levels?",
         "expect": "The Joint Review Board, from page 3."},
    ],
}

PROMPT = ("Questions about a document's content: ALWAYS call ask_document first, even if the answer seems obvious or "
          "you think the document does not cover it; never answer or say 'the document does not say' without "
          "reading it. (pdf_info only gives file facts such as page count and metadata, not content.) Then answer that one question "
          "from the page text it returns, in one or two sentences, and name the page (for example 'page 2'). Quote "
          "numbers, dates and names exactly as written. Do not summarise the whole document unless asked. If the "
          "returned text does not contain the answer, reply exactly 'The document does not contain this information.' "
          "(if some pages were left out, add that only the pages searched were checked). Never answer from general "
          "knowledge or guess, and never use a related fact to answer a question about something else - a question "
          "about November is not answered by October's figures. Summaries: when asked to "
          "summarise a document, call summarize_document (not ask_document) and write the summary from what it "
          "returns, covering every part in order.")

TOOLS = {
    "ask_document": {
        "fn": qa.ask_document,
        "description": "Get the text of a PDF's pages (page numbered) to answer a question about its content. "
                       "Read-only. Long files return only the pages that best match the question.",
        "parameters": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "ID of the PDF, e.g. f_1a2b3c4d5e6f"},
            "question": {"type": "string", "description": "the user's question, in their words"},
            "pages": {"type": "string", "description": 'optional, e.g. "2-4"; default all pages'},
        }, "required": ["file_id", "question"]},
    },
    "summarize_document": {
        "fn": summary.summarize_document,
        "description": "Get what the model needs to summarise a WHOLE PDF: its full text if short, otherwise notes "
                       "made from every part of it. Read-only. Use for 'summarise this document', not for summarising the "
                       "changes between two versions (compare_documents does that).",
        "parameters": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "ID of the PDF, e.g. f_1a2b3c4d5e6f"},
            "pages": {"type": "string", "description": 'optional, e.g. "1-10"; default the whole document'},
        }, "required": ["file_id"]},
    },
}
