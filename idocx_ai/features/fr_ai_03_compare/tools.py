"""FR-AI-03 Document Comparison & Change Explanation: tools, prompt, sidebar entry.

Kept lean on purpose: the behaviour rules live in the tool descriptions and in
compare_documents' own result ("next_step"), where the model follows them best.
"""

from . import compare, explain

FILE_ID = {"type": "string", "description": "ID of the PDF, e.g. f_1a2b3c4d5e6f"}

INFO = {
    "id": "FR-AI-03",
    "title": "Document Comparison",
    "status": "ready",
    "description": "Compares two versions clause by clause (or paragraph by paragraph). Plain code finds "
                   "every addition, removal and modification (moved text counts as modified) with its page and "
                   "box in both versions, the old and new text and a confidence; headers, page numbers, "
                   "renumbering and punctuation are ignored unless asked, and the result says what was ignored. "
                   "Returns a summary (counts and 3-5 key changes explained by the model, each explanation "
                   "checked against the real change), warns about scans and about different languages "
                   "(compares only if you confirm), says 'No differences found' for identical files, and "
                   "writes a marked-up copy of the new version.",
    "samples": ["contract_v1.pdf", "contract_v2.pdf", "contract_v2_scanned.pdf"],
    "examples": [
        {"file": "contract_v1.pdf", "also": ["contract_v2.pdf"],
         "prompt": "Compare these two versions of the contract and show what changed.",
         "expect": "6 changes: payment 30 -> 45 days, liability 12 -> 6 months, Exclusivity removed, Data "
                   "Protection added, Governing Law moved, Termination reworded. Plus a marked-up copy "
                   "(contract_v2-changes.pdf) with highlights and notes."},
        {"file": "contract_v1.pdf", "also": ["contract_v2.pdf"],
         "prompt": "Identify the clauses that were added, removed, or modified between these documents.",
         "expect": "Added: Data Protection. Removed: Exclusivity. Modified: Payment, Liability, Termination. "
                   "Governing Law only moved. The renumbering is NOT reported as a change."},
        {"file": "contract_v1.pdf", "also": ["contract_v2.pdf"],
         "prompt": "Compare the previous and latest versions and provide a summary of the meaningful changes.",
         "expect": "Material: payment terms, liability cap, exclusivity removed, data protection added. "
                   "Minor: termination wording, governing law moved. Every explanation checked against "
                   "the change list."},
        {"file": "contract_v1.pdf", "also": ["contract_v2_scanned.pdf"],
         "prompt": "Compare these two versions and show what changed.",
         "expect": "The scanned version is read by OCR, with a warning that small wording differences may "
                   "be OCR errors. The real changes (payment, liability, the added and removed clauses) "
                   "are still found."},
    ],
}

PROMPT = ("Comparison: compare files the user uploaded, never copies a tool made. If more than two uploaded "
          "files could be meant and the user did not say which, ask which two. Call compare_documents once, "
          "with the older version as old_file_id (by name, else upload order; say which you treated as "
          "older). Show the summary text, then the key changes (change id, page, explanation, confidence) and "
          "anything marked Low confidence or in a warning. If it returns status confirm_language, ask the user "
          "whether to compare documents in different languages and call again with confirm_language_difference "
          "true only if they say yes. If the status is no_differences, say 'No differences found'. The result already "
          "holds the summary and the key changes, so never call summarize_document or check_change_explanations "
          "for a comparison unless the user asks to explain a change that is not among the key changes.")

TOOLS = {
    "compare_documents": {
        "fn": compare.compare_documents,
        "description": "Compare two versions of a document. Returns a summary (counts and 3-5 key changes with "
                       "explanations) and every change found (Added, Removed, Modified; moved text is Modified) with "
                       "an id, page and box in both versions, the old and new text, a word diff, signals such as "
                       "changed numbers and a confidence; ignores headers, page numbers, renumbering and punctuation "
                       "unless include_ignored. Different languages need the user's confirmation. Also writes a "
                       "marked-up copy of the new version. Explain only the changes it returns.",
        "parameters": {"type": "object", "properties": {
            "old_file_id": FILE_ID, "new_file_id": FILE_ID,
            "include_ignored": {"type": "boolean", "description": "also report headers, footers, page numbers and "
                                                                  "punctuation-only edits; only if the user asks"},
            "confirm_language_difference": {"type": "boolean", "description": "true only after the user confirmed "
                                                                            "comparing documents in different languages"},
        }, "required": ["old_file_id", "new_file_id"]},
    },
    "check_change_explanations": {
        "fn": explain.check_change_explanations,
        "description": "Check explanations of compared changes before presenting a summary: each needs a real "
                       "change id, a significance (material: amounts, dates, obligations, parties, whole "
                       "clauses; minor: wording with the same meaning, moves) and an explanation whose numbers "
                       "match the change. Returns verified, rejected (with reasons) and not yet explained.",
        "parameters": {"type": "object", "properties": {
            "comparison_id": {"type": "string"},
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "change_id": {"type": "string", "description": "e.g. C1"},
                "significance": {"type": "string", "enum": ["material", "minor"]},
                "explanation": {"type": "string", "description": "one or two plain sentences"},
            }, "required": ["change_id", "significance", "explanation"]}},
        }, "required": ["comparison_id", "items"]},
    },
}
