"""FR-AI-02 AI Document Extraction & Form Filling: tools, prompt, sidebar entry."""

from core import proposals

from . import extract, fill, forms

proposals.register_applier("form_fill", fill.apply_form_fill)

FILE_ID = {"type": "string", "description": "ID of the PDF, e.g. f_1a2b3c4d5e6f"}

INFO = {
    "id": "FR-AI-02",
    "title": "Extraction & Form Filling",
    "status": "ready",
    "description": "Extracts fields (names, dates, amounts, IDs) and tables from a document, each value checked "
                   "against the source text; text layer, OCR for scans, the vision model when OCR fails. Fills "
                   "fillable PDF forms after you review and edit the values; signatures and declarations are left "
                   "to you, and every filled field is read back to verify.",
    "samples": ["policy_schedule.pdf", "invoice.pdf", "invoice_scanned.pdf", "application_form.pdf",
                "payment_form.pdf"],
    "examples": [
        {"file": "policy_schedule.pdf", "also": ["application_form.pdf"],
         "prompt": "Extract the customer name, address, date of birth and policy number from the policy schedule and "
                   "fill the application form.",
         "expect": "Review card: Meera Testcase, 22/03/1985, the Pune address, POL/2026/004512, Family Floater, "
                   "Rs. 5,00,000, nominee Arjun Testcase - all found on p1. Mobile and email EMPTY (not in the "
                   "schedule). The declaration is yours to tick. Edit if you like, click Fill form -> verified copy."},
        {"file": "invoice.pdf", "also": ["payment_form.pdf"],
         "prompt": "Read this invoice and populate the corresponding fields in the payment form.",
         "expect": "Payee ACME Test Supplies Pvt Ltd and its GSTIN (not the buyer's), GST registered ticked, "
                   "INV-2026-0187, dates, 19,470.00 (the total, not the subtotal), account, IFSC, NEFT. The "
                   "approver signature is left to you."},
        {"file": "invoice_scanned.pdf", "also": ["payment_form.pdf"],
         "prompt": "Extract the required information from the uploaded document and use it to complete the form.",
         "expect": "The same values read from the scan by OCR, with a warning to check them."},
        {"file": "invoice.pdf",
         "prompt": "Extract the invoice details and create a summary.",
         "expect": "Invoice INV-2026-0187 dated 05/10/2026, due 04/11/2026, from ACME Test Supplies Pvt Ltd to "
                   "Example Buyer Pvt Ltd; total Rs. 19,470.00 (subtotal 16,500.00 plus CGST and SGST); the "
                   "3-row item table. Each value marked as found on page 1."},
        {"file": "invoice_scanned.pdf",
         "prompt": "Extract the invoice number, invoice date, total amount and the line items.",
         "expect": "Values read by OCR, with a warning. The item table is read by the vision model from the page "
                   "image (OCR garbles ruled tables), all 3 rows; cells OCR could not confirm are marked to check."},
    ],
}

PROMPT = ("Extraction: call extract_information with the fields the user wants (include_tables for line items). "
          "Form filling: call prepare_form_fill directly, once, with the source document and the fillable form; only "
          "the user fills it, from the review card. Follow each tool's next_step; never supply a value a tool left empty.")

TOOLS = {
    "read_form_fields": {
        "fn": forms.read_form_fields,
        "description": "List a fillable PDF form's fields: name, label, type, dropdown options, and which are for the "
                       "user only (signatures, declarations). Read-only.",
        "parameters": {"type": "object", "properties": {"file_id": FILE_ID}, "required": ["file_id"]},
    },
    "prepare_form_fill": {
        "fn": fill.prepare_form_fill,
        "description": "Prepare filling a fillable PDF form from a source document, in ONE call: it reads the "
                       "form's fields and extracts and checks every value from the source by itself, so call it "
                       "directly, without read_document, read_form_fields or extract_information first. Puts a "
                       "proposal in a review card where the user edits and approves; fills nothing itself. "
                       "Signatures and declarations are always left to the user.",
        "parameters": {"type": "object", "properties": {
            "source_file_id": {"type": "string", "description": "the document to take values from"},
            "form_file_id": {"type": "string", "description": "the fillable form"},
        }, "required": ["source_file_id", "form_file_id"]},
    },
    "extract_information": {
        "fn": extract.extract_information,
        "description": "Extract named fields (and optionally tables) from a document. Each value comes with its "
                       "status: found (with page and quote), unverified (and why), or empty. Read-only.",
        "parameters": {"type": "object", "properties": {
            "file_id": FILE_ID,
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": 'what to extract, e.g. ["invoice number", "invoice date", "total amount"]'},
            "include_tables": {"type": "boolean"},
        }, "required": ["file_id", "fields"]},
    },
}
