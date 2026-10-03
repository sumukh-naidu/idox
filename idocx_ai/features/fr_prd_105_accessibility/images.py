"""FR-PRD-105 image descriptions: the vision model drafts alt text for each content image.

An untagged PDF has nowhere proper to hold alt text, so these are suggestions for the
author to add when the document is re-authored with tags; nothing is written into the file.
"""

from core import llm
from core.pdfutil import ToolError, open_pdf

from .checks import content_images, scanned_pages

MIN_SIDE_PT = 36        # under half an inch: icons and rules, not content
LONG_SIDE_PX = 768      # enough detail to read chart labels, small enough to stay quick

PROMPT = ("This image is from a document. Write alt text for a blind reader: one or two plain sentences "
          "saying what it shows, including any text, labels or numbers that matter. Do not start with "
          "'Image of' or 'Picture of'. If it is purely decorative, reply only: DECORATIVE")


def describe_images(file_id: str, max_images: int = 6) -> dict:
    meta, doc = open_pdf(file_id)
    out, skipped = [], 0
    with doc:
        candidates = [(p, x, r) for p, x, r in content_images(doc, scanned_pages(doc))
                      if min(r.width, r.height) >= MIN_SIDE_PT]
        for page_no, xref, rect in candidates[:max_images]:
            dpi = int(min(150, LONG_SIDE_PX / (max(rect.width, rect.height) / 72)))
            png = doc[page_no - 1].get_pixmap(clip=rect, dpi=dpi).tobytes("png")
            try:
                text = llm.ask_about_image(PROMPT, png, max_tokens=150)
            except Exception as e:
                raise ToolError(f"the model could not be reached to describe images: {e}")
            out.append({"page": page_no, "size_inches": f"{rect.width / 72:.1f} x {rect.height / 72:.1f}",
                        "suggested_alt_text": None if text.strip().upper() == "DECORATIVE" else text,
                        "decorative": text.strip().upper() == "DECORATIVE"})
        skipped = max(0, len(candidates) - max_images)
    result = {"file_id": file_id, "images": out,
              "note": "suggested alt text for the author to add; it is not written into the PDF"}
    if skipped:
        result["more"] = f"{skipped} more image(s); call again with a higher max_images"
    if not candidates:
        result["note"] = "no content images (page scans are not described; they need a text layer instead)"
    return result
