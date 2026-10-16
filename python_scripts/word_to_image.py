"""
word_to_image.py -- convert a Word file (.docx) into one PNG per page.

No model: this is a rendering job. LibreOffice draws the pages (the same engine word_to_pdf.py uses), then every
page is saved as a PNG. There is nothing for a model to read.

Usage:
    .venv/Scripts/python python_scripts/word_to_image.py                    every docx in input/word_to_image_input/
    .venv/Scripts/python python_scripts/word_to_image.py input/word_to_image_input/file.docx
Output goes to output/word_to_image_output/ (<name>_page1.png, ... plus the intermediate <name>.pdf).
Needs LibreOffice. See doc_pipeline.py for how it works.
"""

import paths
import doc_pipeline

if __name__ == "__main__":
    doc_pipeline.main("word", "image", __doc__, paths.inp("word_to_image_input"), paths.out("word_to_image_output"))
