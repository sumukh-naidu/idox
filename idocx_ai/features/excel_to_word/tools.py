"""Excel to Word: the tool the agent gets, and its part of the prompt."""

from . import convert

PROMPT = (
    "Excel to Word: for an Excel (.xlsx) file, call excel_to_word once (pass sheets only if the user names "
    "some). Never retype or summarise cell values yourself. Report the sheets and their sizes, any limits "
    "or hidden sheets from the result, and the new Word file."
)

TOOLS = {
    "excel_to_word": {
        "fn": convert.excel_to_word,
        "description": "Convert an uploaded Excel workbook (.xlsx) into a NEW Word document: each visible "
                       "sheet (or each sheet named) becomes a heading and a real Word table. Cell values are "
                       "copied exactly by code; merged cells and bold are kept; numbers and dates are shown "
                       "as Excel formats them. The workbook is not changed.",
        "parameters": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "ID of the .xlsx file, e.g. f_1a2b3c4d5e6f"},
            "sheets": {"type": "array", "items": {"type": "string"},
                       "description": "optional: only these sheet names, in this order"},
        }, "required": ["file_id"]},
    },
}
