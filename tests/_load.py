"""Helpers for the tests. Run them from the idox folder:  python -m unittest discover -s tests -v

None of the tests calls the model. They need pymupdf and Pillow; some also need Tesseract or LibreOffice and
skip themselves when it is missing.
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# A digital and a scanned copy of the same made-up agreement, already in the repo.
FIXTURES = os.path.join(REPO, "idocx_ai", "features", "fr_ai_04_translation", "input")
AGREEMENT = os.path.join(FIXTURES, "agreement.pdf")
AGREEMENT_SCANNED = os.path.join(FIXTURES, "agreement_scanned.pdf")


def load_script_defs(filename):
    """Run a converter script's definitions (everything before its argparse block) and return them.

    The converter scripts do their work the moment they are imported, so they cannot be imported; this reads
    just their functions and constants. Once a script has a __main__ guard, a plain import works instead.
    """
    src = open(os.path.join(REPO, filename), encoding="utf-8").read()
    cut = src.find("\nparser = argparse")
    if cut < 0:
        cut = src.find("\nif __name__")
    ns = types.SimpleNamespace()
    exec(compile(src[:cut] if cut > 0 else src, filename + " (definitions)", "exec"), ns.__dict__)
    return ns
