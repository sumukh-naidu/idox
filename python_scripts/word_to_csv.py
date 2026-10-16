"""
word_to_csv.py -- convert a Word file (.docx) into a CSV file (text one line per row, tables as rows and columns), read by the local 2B model (LibreOffice draws the pages first).

Usage:
    .venv/Scripts/python python_scripts/word_to_csv.py                    every docx in input/word_to_csv_input/
    .venv/Scripts/python python_scripts/word_to_csv.py input/word_to_csv_input/file.docx
Output goes to output/word_to_csv_output/. The model server must be running (see docs/new_conversions.md).
See doc_pipeline.py for how it works.
"""

import paths
import doc_pipeline

if __name__ == "__main__":
    doc_pipeline.main("word", "csv", __doc__, paths.inp("word_to_csv_input"), paths.out("word_to_csv_output"))
