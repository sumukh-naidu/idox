"""
word_to_xml.py -- convert a Word file (.docx) into an XML document, read by the local 2B model (LibreOffice draws the pages first).

Usage:
    .venv/Scripts/python python_scripts/word_to_xml.py                    every docx in input/word_to_xml_input/
    .venv/Scripts/python python_scripts/word_to_xml.py input/word_to_xml_input/file.docx
Output goes to output/word_to_xml_output/. The model server must be running (see docs/new_conversions.md).
See doc_pipeline.py for how it works.
"""

import paths
import doc_pipeline

if __name__ == "__main__":
    doc_pipeline.main("word", "xml", __doc__, paths.inp("word_to_xml_input"), paths.out("word_to_xml_output"))
