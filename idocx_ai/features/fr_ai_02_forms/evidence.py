"""FR-AI-02 evidence for one extracted value: what kind of value it is, where it sits on the page, how sure we are.

Everything here is plain code, not the model: the model only says what a field's value is and quotes it, and the
code decides the kind, the box and the confidence (brief 2.2: value, confidence and the box on the page).
"""

import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import pymupdf

from core.text import OCR_DPI

TESSERACT_TIMEOUT = 60      # seconds per page
BLUR_RATIO = 0.55           # a page whose edges barely change when blurred again is already blurred
LOW_PAGE_OCR = 60           # mean tesseract confidence of a page below this: poor scan

CONF_TEXT_LAYER = 97        # exact text from the PDF itself
CONF_VISION = 70            # read by the vision model from the image: check it
CONF_UNVERIFIED = 40        # the quote or the value is not in the source
CONF_FORMAT_BAD = 60        # the value does not have the format its kind needs
CONF_POOR_SCAN = 60         # blurred or low-confidence scan


# ------------------------------------------------------------------ kind of value and its format

_PAN = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
_GSTIN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_AADHAAR = re.compile(r"^\d{4}\s?\d{4}\s?\d{4}$")
_DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
                 "%d/%m/%y", "%d-%m-%y", "%d %B, %Y"]
_CURRENCY = re.compile(r"^(?:rs\.?|inr|₹|\$|usd|eur|€)\s*", re.I)
_AMOUNT_LABEL = re.compile(r"amount|total|sum|fee|price|premium|balance|tax|cost|salary|income|payable|charges?", re.I)
_ID_LABEL = re.compile(r"\b(?:number|no\.?|id|code|ref(?:erence)?|policy|account|invoice|voucher)\b", re.I)
_NAME_LABEL = re.compile(r"name|holder|nominee|applicant|beneficiary|signatory|employer|vendor", re.I)
_DATE_LABEL = re.compile(r"date|dob|born|birth|expiry|valid (?:from|till|upto)", re.I)


def parse_date(value: str) -> datetime | None:
    v = re.sub(r"\s+", " ", str(value)).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def parse_amount(value: str) -> float | None:
    v = _CURRENCY.sub("", str(value).strip())
    v = re.sub(r"[\s,]", "", v).rstrip("/-").rstrip("/")
    return float(v) if re.fullmatch(r"-?\d+(?:\.\d+)?", v) else None


def value_kind(label: str, value) -> tuple[str, bool | None, str | None]:
    """(kind, format_valid, note): kind is date, amount, name, id or text, so iDocx can check the format."""
    v = str(value).strip()
    low = label.lower()
    pattern_ids = [("PAN", _PAN, "pan"), ("GSTIN", _GSTIN, "gstin"), ("IFSC", _IFSC, "ifsc"), ("Aadhaar", _AADHAAR, "aadhaar")]
    for name, rx, word in pattern_ids:
        if rx.match(v):
            return "id", True, f"looks like {"an" if name[0] in "AEIOU" else "a"} {name}"
        if word in low:
            return "id", False, f"does not look like {"an" if name[0] in "AEIOU" else "a"} {name}"
    if _DATE_LABEL.search(label) or parse_date(v):
        ok = parse_date(v) is not None
        return "date", ok, None if ok else "not a date in a known format"
    if _AMOUNT_LABEL.search(label) or _CURRENCY.match(v):
        ok = parse_amount(v) is not None
        return "amount", ok, None if ok else "not a number"
    if _NAME_LABEL.search(label) and not _ID_LABEL.search(label):
        ok = bool(re.fullmatch(r"[A-Za-z][A-Za-z .'\-&]+", v)) and len(v) >= 2
        return "name", ok, None if ok else "a name has letters only"
    if _ID_LABEL.search(label) and not re.search(r"mobile|phone", low):
        ok = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/\-_.]{2,39}", v)) and any(c.isdigit() for c in v)
        return "id", ok, None if ok else "an ID has letters, digits and - / only"
    return "text", None, None


# ------------------------------------------------------------------ scans: words, confidence, blur

def ocr_words(page) -> list[dict]:
    """Words on a scanned page: text, tesseract confidence 0-100 and box in PDF points (top-left origin)."""
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "page.png"
        page.get_pixmap(dpi=OCR_DPI).save(png)
        done = subprocess.run(["tesseract", str(png), "stdout", "-l", "eng", "tsv"], capture_output=True, text=True,
                              timeout=TESSERACT_TIMEOUT, check=False,
                              env={**os.environ, "TESSDATA_PREFIX": pymupdf.get_tessdata()})
    scale, words = 72 / OCR_DPI, []
    for row in done.stdout.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or c[0] != "5" or not c[11].strip() or float(c[10]) < 0:
            continue
        x, y, w, h = (int(v) * scale for v in c[6:10])
        words.append({"text": c[11].strip(), "conf": float(c[10]), "box": (x, y, x + w, y + h)})
    return words


def sharpness_ratio(page) -> float:
    """How much of a page's edge detail survives a blur: near 1 means it was already blurred, a sharp scan is lower."""
    from PIL import Image, ImageFilter, ImageStat
    pix = page.get_pixmap(dpi=100, colorspace=pymupdf.csGRAY)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    edges = ImageStat.Stat(img.filter(ImageFilter.FIND_EDGES)).var[0]
    blurred = ImageStat.Stat(img.filter(ImageFilter.GaussianBlur(2)).filter(ImageFilter.FIND_EDGES)).var[0]
    return blurred / edges if edges else 1.0


def scan_quality(words: list[dict], page) -> dict:
    """Mean OCR confidence and, if the scan is poor, why (the brief's example reason is 'page is blurred')."""
    mean = sum(w["conf"] for w in words) / len(words) if words else 0.0
    reason = None
    if sharpness_ratio(page) > BLUR_RATIO:
        reason = "page is blurred"
    elif mean < LOW_PAGE_OCR:
        reason = "OCR confidence is low on this page"
    return {"mean_confidence": round(mean), "reason": reason}


# ------------------------------------------------------------------ where a value sits

def _norm_word(s: str) -> str:
    return re.sub(r"[^\w]", "", s).lower()


def box_of(rect, page, page_no: int) -> dict:
    return {"page": page_no, "x0": round(rect[0], 1), "y0": round(rect[1], 1), "x1": round(rect[2], 1),
            "y1": round(rect[3], 1), "unit": "pt", "origin": "top-left",
            "page_width": round(page.rect.width, 1), "page_height": round(page.rect.height, 1)}


def locate_in_text_layer(page, page_no: int, value: str, quote: str | None) -> dict | None:
    hits = page.search_for(str(value))
    if not hits:
        return None
    if quote and len(hits) > 1:        # the same text can appear twice: take the one inside the quoted line
        inside = [h for q in page.search_for(quote[:80]) for h in hits if q.contains(h)]
        hits = inside or hits
    return box_of(hits[0], page, page_no)


def locate_in_scan(words: list[dict], page, page_no: int, value: str) -> tuple[dict | None, float | None]:
    """(box, mean confidence of the value's own words) from the OCR words, or (None, None) if they are not there."""
    want = [_norm_word(t) for t in str(value).split() if _norm_word(t)]
    have = [_norm_word(w["text"]) for w in words]
    for i in range(len(have) - len(want) + 1):
        if want and have[i:i + len(want)] == want:
            span = words[i:i + len(want)]
            rect = (min(w["box"][0] for w in span), min(w["box"][1] for w in span),
                    max(w["box"][2] for w in span), max(w["box"][3] for w in span))
            return box_of(rect, page, page_no), sum(w["conf"] for w in span) / len(span)
    return None, None
