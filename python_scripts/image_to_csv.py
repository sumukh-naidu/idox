"""
image_to_csv.py -- convert an IMAGE into CSV (.csv): text one line per row, tables as rows and columns, read by the local 2B model.

Usage:
    .venv/Scripts/python python_scripts/image_to_csv.py                      every image in input/image_to_csv_input/
    .venv/Scripts/python python_scripts/image_to_csv.py input/image_to_csv_input/page.png
Output goes to output/image_to_csv_output/. The model server must be running (see docs/new_conversions.md).
See image_pipeline.py for what is read, checked and written.
"""

import paths
import image_pipeline

if __name__ == "__main__":
    image_pipeline.main("csv", __doc__, paths.inp("image_to_csv_input"), paths.out("image_to_csv_output"))
