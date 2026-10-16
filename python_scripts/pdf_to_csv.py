"""
pdf_to_csv.py -- convert a PDF into a CSV file (text one line per row, tables as rows and columns), read by the local 2B model.

Usage:
    .venv/Scripts/python python_scripts/pdf_to_csv.py                    every pdf in input/pdf_to_csv_input/
    .venv/Scripts/python python_scripts/pdf_to_csv.py input/pdf_to_csv_input/file.pdf
Output goes to output/pdf_to_csv_output/. The model server must be running (see docs/new_conversions.md).
See doc_pipeline.py for how it works.
"""

import paths
import doc_pipeline

if __name__ == "__main__":
    doc_pipeline.main("pdf", "csv", __doc__, paths.inp("pdf_to_csv_input"), paths.out("pdf_to_csv_output"))
