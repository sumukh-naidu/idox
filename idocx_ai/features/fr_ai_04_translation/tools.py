"""FR-AI-04 AI Document Translation with Layout Preservation: tools, prompt, sidebar entry."""

from . import translate

INFO = {
    "id": "FR-AI-04",
    "title": "Translation",
    "status": "ready",
    "description": "Translates English PDFs into Hindi or Spanish, keeping the layout: each block of text is "
                   "replaced in place, tables keep their columns, images and lines stay, and numbers, dates, "
                   "emails, IDs and abbreviations must come through unchanged. Scanned pages are read by OCR and "
                   "translated onto clean pages. Every translation also comes as a text file (searchable Hindi).",
    "samples": ["agreement.pdf", "agreement_scanned.pdf"],
    "examples": [
        {"file": "agreement.pdf",
         "prompt": "Translate this document from English to Hindi while preserving the original layout.",
         "expect": "agreement-hi.pdf with the same layout (logo, headings, the fee table with untouched amounts), "
                   "about 30 s for 2 pages, plus agreement-hi.txt. Amounts, dates, the email and the GSTIN unchanged."},
        {"file": "agreement.pdf",
         "prompt": "Translate this agreement into Spanish without changing the document structure.",
         "expect": "agreement-es.pdf with the same structure, GST still 'GST', every number unchanged, plus a .txt."},
        {"file": "agreement.pdf",
         "prompt": "Translate the text while keeping the headings, tables, formatting, and page structure as close "
                   "to the original as possible.",
         "expect": "No language named: the assistant should ASK whether you want Hindi or Spanish, not guess."},
        {"file": "agreement_scanned.pdf",
         "prompt": "Translate this scanned page into Spanish.",
         "expect": "Read by OCR; the translation is placed on a clean page where the text was (the scan image is "
                   "not kept), amounts unchanged, with a note to check names and numbers."},
    ],
}

PROMPT = ("Translation: English to Hindi or Spanish only. If the user has not named the target language, ask which "
          "one; never guess. Call translate_document once, then report both files it created and any values it "
          "asks you to check.")

TOOLS = {
    "translate_document": {
        "fn": translate.translate_document,
        "description": "Translate an English PDF into Hindi or Spanish, writing a NEW PDF with the same layout and a "
                       "text copy. Numbers, dates, emails, IDs and abbreviations are checked to come through "
                       "unchanged. Scanned pages are read by OCR and translated onto clean pages.",
        "parameters": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "ID of the PDF, e.g. f_1a2b3c4d5e6f"},
            "language": {"type": "string", "enum": ["Hindi", "Spanish"]},
            "pages": {"type": "string", "description": 'optional, e.g. "1-3"; default all pages'},
        }, "required": ["file_id", "language"]},
    },
}
