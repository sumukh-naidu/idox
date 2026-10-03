"""FR-AI-05 Smart Redaction: tools, prompt, sidebar entry.

There is deliberately no tool that applies a redaction: only the user's approval in the
review card does, through the API. The model can find and propose, never finalise.
"""

from core import proposals

from . import apply, find

proposals.register_applier("redaction", apply.apply_redaction)

FILE_ID = {"type": "string", "description": "ID of the PDF, e.g. f_1a2b3c4d5e6f"}

INFO = {
    "id": "FR-AI-05",
    "title": "Smart Redaction",
    "status": "ready",
    "description": "Finds sensitive information: government IDs (checksum-validated), bank and card details, "
                   "phone numbers and emails, dates of birth, API keys, tokens and passwords (rules), plus names "
                   "and addresses (model, checked as exact text). Proposes them with a highlighted preview; YOU "
                   "review and apply them in the review card. The redaction removes the text (blacks out the image "
                   "on scans) and is verified.",
    "samples": ["redaction_form.pdf", "redaction_scanned.pdf"],
    "examples": [
        {"file": "redaction_form.pdf",
         "prompt": "Find sensitive information in this document and highlight what should be redacted.",
         "expect": "About 25 items over 2 pages: Aadhaar, VID, PAN, passport, GSTIN, bank account, IFSC, UPI, "
                   "card, phone numbers, email, date of birth, the name and address, and 10 secrets (keys, tokens, "
                   "passwords, private key). Values shown masked. Plus a highlighted preview PDF. The decoys in "
                   "'Office use only' and 'Not secrets' are NOT flagged. A review card appears: click Apply -> a "
                   "verified redacted file."},
        {"file": "redaction_form.pdf",
         "prompt": "Identify phone numbers, email addresses, and government identification numbers that may need "
                   "redaction.",
         "expect": "Lists the two phone numbers, the email and the government IDs (Aadhaar, VID, PAN, passport, "
                   "GSTIN), masked."},
        {"file": "redaction_scanned.pdf",
         "prompt": "Find sensitive information and allow me to review it before applying the redaction.",
         "expect": "A scan: read by OCR with a warning to check numbers carefully. Same 13 items as page 1 of the "
                   "form plus the name and address. Untick a few items, click Apply: only the ticked ones are blacked "
                   "out, burned into the image."},
    ],
}

PROMPT = ("Redaction: call find_sensitive_info and follow its next_step. Never repeat a full ID, number or secret; "
          "use the masked values. You cannot apply a redaction: only the user can, from the review card.")

TOOLS = {
    "find_sensitive_info": {
        "fn": find.find_sensitive_info,
        "description": "Find sensitive information in a PDF (IDs, bank and card details, contact details, dates of "
                       "birth, keys, tokens, passwords, names, addresses) and prepare a redaction proposal for the "
                       "user to review, with a highlighted preview file. Does not redact anything. Values come back "
                       "masked.",
        "parameters": {"type": "object", "properties": {"file_id": FILE_ID}, "required": ["file_id"]},
    },
}
