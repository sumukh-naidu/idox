"""Generate the FR-AI-06 Q&A test set: a made-up three-page service report. All content is FAKE.

    .venv/bin/python samples/fr_ai_06_qa/make_samples.py
"""

import json
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent

PAGES = [
    ("Service Report - October 2026",
     ["Contract number: SR-2026-0457",
      "Supplier: ACME Test Ltd. Customer: Example Buyer Pvt Ltd.",
      "This report covers support activity from 1 October 2026 to 31 October 2026.",
      "The service desk is staffed from 9 am to 6 pm, Monday to Friday."]),
    ("Support Activity",
     ["During October the service desk closed 412 support tickets and opened 436.",
      "The average first response time was 22 minutes.",
      "Customer satisfaction was 94% across 188 survey replies.",
      "The busiest day was 14 October with 61 new tickets."]),
    ("Service Levels and Changes",
     ["The target for first response is 30 minutes for priority 1 tickets.",
      "Any change to the service levels must be approved by the Joint Review Board.",
      "The next review meeting is on 12 December 2026.",
      "Questions about this report go to reports@example.com."]),
]

doc = pymupdf.open()
for title, lines in PAGES:
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 80), title, fontsize=18, fontname="hebo")
    y = 130
    for line in lines:
        page.insert_text((60, y), line, fontsize=11, fontname="helv")
        y += 24
doc.set_metadata({"title": "Service Report - October 2026"})
doc.save(OUT / "service_report.pdf")

truth = {"service_report.pdf": {
    "facts": [
        {"question": "What is the contract number?", "answer": "SR-2026-0457", "page": 1},
        {"question": "How many support tickets were closed in October?", "answer": "412", "page": 2},
        {"question": "What was the customer satisfaction score?", "answer": "94%", "page": 2},
        {"question": "Who must approve a change to the service levels?", "answer": "Joint Review Board", "page": 3},
        {"question": "When is the next review meeting?", "answer": "12 December 2026", "page": 3},
    ],
    "not_in_document": [
        "What is the penalty for missing the response target?",
        "Who is the CEO of ACME Test Ltd?",
        "How many tickets were closed in November?",
        "What is the support phone number?",
        "What is the monthly fee for the service?",
    ],
}}
(OUT / "ground_truth.json").write_text(json.dumps(truth, indent=2) + "\n")
print("wrote", OUT / "service_report.pdf")
