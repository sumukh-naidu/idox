"""
ocr.py -- read text off a page that has none, so scanned pages can be checked.

ROLE: VERIFIER, NOT EXTRACTOR.

The model still does the extraction. OCR exists here to give the checks
something to compare against, because on a scanned page they currently have
nothing and report N/A:

    2. text-layer grounding:   N/A    (no text layer -- nothing to verify against)
    4. content coverage:       N/A    (no text layer -- dropped content cannot be detected)

That blindness is not theoretical. On one run of a scanned letter the model
returned 8 blocks where it had previously returned 13 -- five paragraphs of the
letter silently gone -- and every check passed or reported N/A. On the digital
version of the same document, coverage caught a single dropped heading and the
repair logic restored it exactly.

--------------------------------------------------------------------------
WHY OCR IS EVIDENCE AND THE MODEL ALONE IS NOT

Running the model twice proves nothing: temperature is 0, so identical input
gives byte-identical output. Even varying the input only gets you two readings
from the same network, which share the same blind spots -- if it misreads a
font, both passes agree on the wrong answer.

Tesseract is a genuinely different method: per-character classification rather
than a transformer reading a downsampled image. Its mistakes are uncorrelated
with the model's, and that independence is the whole value. Agreement between
them is real evidence; disagreement points at the exact value to check.

--------------------------------------------------------------------------
WHAT THIS IS NOT ALLOWED TO DO

OCR text is a READING of the page, not the page itself. A digital PDF's text
layer is the author's actual characters, which is why dropped lines can be
restored from it byte-for-byte. Nothing may be restored from OCR -- a
disagreement gets reported, never silently applied. The caller enforces this by
tracking where the comparison text came from.

Only used where text exists solely as pixels: the whole page on a scan, and
individual images on an otherwise digital page.
"""

import os
import shutil
import tempfile

# 300 dpi is the long-standing default for OCR accuracy on printed text --
# meaningfully better than the 125 dpi the model reads at, and cheap because
# Tesseract takes about a second per page rather than minutes.
OCR_DPI = 300


def have_tesseract() -> bool:
    """Is the OCR engine actually installed?

    Checked separately from the Python wrapper: pip installs pytesseract
    happily without the tesseract binary it drives, and the resulting error
    appears much later and less clearly.
    """
    if shutil.which("tesseract") is None:
        return False
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return True


def install_hint() -> str:
    if shutil.which("tesseract") is None:
        return ("tesseract not installed -- "
                "sudo apt-get install -y tesseract-ocr")
    return "pytesseract not installed -- pip install pytesseract"


def ocr_page(pdf_page, dpi: int = OCR_DPI, lang: str = "eng") -> str:
    """Read a page's text with Tesseract. Returns "" if OCR is unavailable.

    Returning "" rather than raising keeps the caller's logic simple: no OCR
    behaves exactly like no text layer, and the checks report N/A as they did
    before.
    """
    if not have_tesseract():
        return ""

    import pytesseract

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        pdf_page.get_pixmap(dpi=dpi).save(tmp.name)
        return pytesseract.image_to_string(tmp.name, lang=lang)
    except Exception:
        # A failed OCR read is not worth aborting a conversion for. Behaving
        # as though there were no text layer is the honest fallback.
        return ""
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def ocr_image(path: str, lang: str = "eng") -> str:
    """Read text out of one extracted image file.

    For the other case where text exists only as pixels: a digital page that
    embeds a screenshot or a pasted scan. The page's text layer is exact but
    says nothing about what is inside its pictures -- a logo reading
    "FreeTestData" appeared nowhere in it.
    """
    if not have_tesseract():
        return ""
    import pytesseract
    try:
        return pytesseract.image_to_string(path, lang=lang)
    except Exception:
        return ""
