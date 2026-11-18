"""FR-PRD-105: WCAG colour contrast between text and its page background.

The background is approximated as the page's single most common pixel colour (fine for
the plain-background business documents this service sees; a photo or gradient background
would need a per-region sample, which this does not attempt).
"""

from collections import Counter

import pymupdf

NORMAL_MIN, LARGE_MIN = 4.5, 3.0   # WCAG AA ratios
LARGE_PT, LARGE_BOLD_PT = 18, 14
BOLD_FLAG = 16
SAMPLE_PIXELS = 4000


def _background_rgb(page) -> tuple[int, int, int] | None:
    pix = page.get_pixmap(dpi=36, colorspace=pymupdf.csRGB)
    data, n = pix.samples, pix.width * pix.height
    if not n:
        return None
    step = max(1, (n * 3) // (SAMPLE_PIXELS * 3))
    pixels = Counter(bytes(data[i:i + 3]) for i in range(0, len(data) - 2, step * 3))
    return tuple(pixels.most_common(1)[0][0])


def _luminance(rgb: tuple[int, int, int]) -> float:
    def lin(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(rgb1: tuple[int, int, int], rgb2: tuple[int, int, int]) -> float:
    lighter, darker = sorted((_luminance(rgb1), _luminance(rgb2)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _span_issue(page_no: int, span: dict, bg: tuple[int, int, int]) -> dict | None:
    color = span["color"]
    rgb = ((color >> 16) & 255, (color >> 8) & 255, color & 255)
    large = span["size"] >= LARGE_PT or (span["size"] >= LARGE_BOLD_PT and bool(span["flags"] & BOLD_FLAG))
    required = LARGE_MIN if large else NORMAL_MIN
    ratio = contrast_ratio(rgb, bg)
    if ratio >= required:
        return None
    return {"page": page_no, "text": span["text"].strip()[:40], "ratio": round(ratio, 2), "required": required}


def _page_spans(page):
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            yield from line["spans"]


def check_contrast(doc, max_issues: int = 10) -> tuple[int, list[dict]]:
    """(text spans checked, the ones below the WCAG AA ratio for their size)."""
    checked, issues, seen = 0, [], set()
    for page in doc:
        bg = _background_rgb(page)
        if bg is None:
            continue
        for span in _page_spans(page):
            if not span["text"].strip():
                continue
            checked += 1
            issue = _span_issue(page.number + 1, span, bg)
            if issue and (issue["page"], issue["text"]) not in seen:
                seen.add((issue["page"], issue["text"]))
                issues.append(issue)
    issues.sort(key=lambda i: i["ratio"])
    return checked, issues[:max_issues]
