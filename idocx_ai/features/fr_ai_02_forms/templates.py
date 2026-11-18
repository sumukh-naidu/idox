"""FR-AI-02 form templates (brief 2.2): the fields each template asks for, and how to tell a wrong document.

The brief names KYC Form, Vendor Voucher and Loan Application but does not list their fields. These lists are a
PROPOSAL for the product owner to confirm or change; each template is just data.
"""

import re

TEMPLATES: dict[str, dict] = {
    "KYC Form": {
        "document_word": "KYC document",
        "markers": r"kyc|know your customer|aadhaar|\bpan\b|date of birth",
        "fields": ["Full name", "Father's name", "Date of birth", "PAN", "Aadhaar number", "Residential address",
                   "Mobile number", "Email address", "Occupation"],
    },
    "Vendor Voucher": {
        "document_word": "vendor voucher",
        "markers": r"voucher|vendor|invoice|payable",
        "fields": ["Vendor name", "Vendor code", "Voucher number", "Voucher date", "Invoice number", "Invoice date",
                   "Description", "Amount", "GST number", "Bank account number", "IFSC code", "Payment due date"],
    },
    "Loan Application": {
        "document_word": "loan application",
        "markers": r"loan|applicant|tenure|\bemi\b|interest",
        "fields": ["Applicant name", "Date of birth", "PAN", "Loan type", "Loan amount", "Tenure", "Purpose of loan",
                   "Monthly income", "Employer name", "Mobile number", "Email address"],
    },
}
NAMES = list(TEMPLATES)
WRONG_BELOW = 0.25    # a document with none of the template's marker words and fewer than this share of its fields found


def fields_of(name: str) -> list[str]:
    return list(TEMPLATES[name]["fields"])


def wrong_template_warning(name: str, text: str, found: int, total: int) -> str | None:
    """'This does not look like a KYC document' when the template's own words are absent and few fields were found."""
    t = TEMPLATES[name]
    if re.search(t["markers"], text, re.I):
        return None
    if total and found / total >= WRONG_BELOW:
        return None
    return f"This does not look like a {t['document_word']}."
