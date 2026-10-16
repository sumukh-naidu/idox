"""
word_to_ppt.py -- convert a Word file (.docx) into PowerPoint (.pptx), read by the local 2B model (LibreOffice draws the pages first).

Usage:
    .venv/Scripts/python python_scripts/word_to_ppt.py                    every docx in input/word_to_ppt_input/
    .venv/Scripts/python python_scripts/word_to_ppt.py input/word_to_ppt_input/file.docx
Output goes to output/word_to_ppt_output/. The model server must be running (see docs/new_conversions.md).
See doc_pipeline.py for how it works.
"""

import paths
import doc_pipeline

if __name__ == "__main__":
    doc_pipeline.main("word", "ppt", __doc__, paths.inp("word_to_ppt_input"), paths.out("word_to_ppt_output"))
