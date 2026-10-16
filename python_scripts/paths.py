"""
paths.py -- where the project's folders live, found from this file, never from the current directory.

    <project>/
        python_scripts/   every Python script (this file is here)
        input/            every <name>_input folder: the files to convert
        output/           every <name>_output folder: what the conversions write
        tests/  api/  docs/  models_manual/

A script can be started from any folder (`python python_scripts/pdf_to_png.py ...`) and still reads from
input/<name>_input/ and writes to output/<name>_output/.
"""
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
INPUT = ROOT / "input"
OUTPUT = ROOT / "output"
MODELS = ROOT / "models_manual"


def inp(name: str) -> str:
    """The path of an input folder, e.g. inp("pdf_to_png_input") -> <project>/input/pdf_to_png_input."""
    return str(INPUT / name)


def out(name: str) -> str:
    """The path of an output folder or file, e.g. out("pdf_to_png_output") -> <project>/output/pdf_to_png_output."""
    return str(OUTPUT / name)
