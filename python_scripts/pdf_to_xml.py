"""
pdf_to_xml.py -- convert a PDF into an XML document, read by the local 2B model.

Usage:
    .venv/Scripts/python python_scripts/pdf_to_xml.py                    every pdf in input/pdf_to_xml_input/
    .venv/Scripts/python python_scripts/pdf_to_xml.py input/pdf_to_xml_input/file.pdf
Output goes to output/pdf_to_xml_output/. The model server must be running (see docs/new_conversions.md).
See doc_pipeline.py for how it works.
"""

import paths
import doc_pipeline

if __name__ == "__main__":
    doc_pipeline.main("pdf", "xml", __doc__, paths.inp("pdf_to_xml_input"), paths.out("pdf_to_xml_output"))
