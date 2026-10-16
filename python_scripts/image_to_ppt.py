"""
image_to_ppt.py -- convert an IMAGE into PowerPoint (.pptx), read by the local 2B model.

Usage:
    .venv/Scripts/python python_scripts/image_to_ppt.py                      every image in input/image_to_ppt_input/
    .venv/Scripts/python python_scripts/image_to_ppt.py input/image_to_ppt_input/page.png
Output goes to output/image_to_ppt_output/. The model server must be running (see docs/new_conversions.md).
See image_pipeline.py for what is read, checked and written.
"""

import paths
import image_pipeline

if __name__ == "__main__":
    image_pipeline.main("pptx", __doc__, paths.inp("image_to_ppt_input"), paths.out("image_to_ppt_output"))
