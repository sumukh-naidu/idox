"""FR-AI-05 rule-based detectors for sensitive information.

Each detector is a pattern, plus a checksum where the format has one (Aadhaar and VID
use Verhoeff, cards use Luhn, GSTIN has its own mod-36 check digit), plus required
context words where the bare pattern would catch too much (a bank account number is
just digits; a passport number looks like many reference numbers). A checksum lets a
detector reject digit strings that merely look like an ID.

Values never leave this module unmasked: results carry a masked form, so the model
and the chat never repeat a full ID or secret.
"""

import re
from dataclasses import dataclass, field

CONTEXT_CHARS = 40     # how far before a match to look for context words
# A digit run must not be part of a longer grouped number: a 12-digit Aadhaar pattern would
# otherwise match the first 12 digits of a 16-digit card number "4111 1111 1111 1111".
NB, NA = r"(?<!\d)(?<!\d )(?<!\d-)", r"(?![ -]?\d)"

# ------------------------------------------------------------------ checksums

_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
      [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
      [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
      [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
      [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
      [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_ok(digits: str) -> bool:
    c = 0
    for i, d in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][int(d)]]
    return c == 0


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


_GST = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_ok(g: str) -> bool:
    total = 0
    for i, ch in enumerate(g[:14]):
        p = _GST.index(ch) * (2 if i % 2 else 1)
        total += p // 36 + p % 36
    return _GST[(36 - total % 36) % 36] == g[14]


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


# ------------------------------------------------------------------ detectors

@dataclass
class Detector:
    kind: str                       # what it is, shown to the user
    category: str                   # government_id / financial / contact / personal / secret
    pattern: re.Pattern
    group: int = 0                  # which regex group is the sensitive value
    check: object = None            # value -> bool; None means no checksum exists
    context: tuple = ()             # words that must appear just before the match
    confidence: str = "high"
    exclude_context: tuple = field(default_factory=tuple)


def _p(rx, flags=0):
    return re.compile(rx, flags)


# Order matters: earlier (more specific) detectors win overlapping text.
DETECTORS = [
    # --- secrets
    Detector("private key", "secret", _p(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    Detector("AWS access key", "secret", _p(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Detector("GitHub token", "secret", _p(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    Detector("Slack token", "secret", _p(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    Detector("Google API key", "secret", _p(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    Detector("API secret key", "secret", _p(r"\b(?:sk|rk)[-_](?:live_|test_|proj-|ant-)?[A-Za-z0-9_\-]{20,}")),
    Detector("JWT / access token", "secret", _p(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    Detector("password in a connection string", "secret", _p(r"\b[a-z][a-z0-9+.\-]*://[^\s:@/]+:([^\s@/]+)@"), group=1),
    Detector("bearer token", "secret", _p(r"\bBearer\s+([A-Za-z0-9._\-~+/]{16,}=*)"), group=1),
    # "PIN" is left out on purpose: in India "PIN code" is the postal code.
    Detector("password or secret", "secret",
             _p(r"\b[\w.\-]*?(?:password|passwd|pwd|passcode|secret|api[_ -]?key|access[_ -]?key|"
                r"secret[_ -]?key|auth[_ -]?token|token)\b\s*[:=]\s*[\"']?([^\s\"',;]{4,})", re.I), group=1,
             check=lambda v: bool(re.search(r"[\d\W_]", v)) or len(v) >= 16),
    # --- government IDs
    Detector("GSTIN", "government_id", _p(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b"), check=gstin_ok),
    Detector("PAN", "government_id", _p(r"\b[A-Z]{3}[ABCFGHLJPTK][A-Z]\d{4}[A-Z]\b")),
    Detector("Aadhaar VID", "government_id", _p(NB + r"\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}" + NA),
             check=lambda v: verhoeff_ok(_digits(v)), context=("vid", "virtual id")),
    Detector("Aadhaar number", "government_id", _p(NB + r"[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}" + NA),
             check=lambda v: verhoeff_ok(_digits(v))),
    Detector("passport number", "government_id", _p(r"\b[A-PR-WYZ][1-9]\d{6}\b"), context=("passport",)),
    Detector("voter ID", "government_id", _p(r"\b[A-Z]{3}\d{7}\b"), context=("voter", "epic")),
    # --- financial
    Detector("card number", "financial", _p(NB + r"(?:4\d{3}|5[1-5]\d{2}|2[2-7]\d{2}|3[47]\d{2}|6\d{3})"
                                             r"(?:[ -]?\d{4}){2}[ -]?\d{1,7}" + NA),
             check=lambda v: 13 <= len(_digits(v)) <= 19 and luhn_ok(_digits(v))),
    Detector("IFSC code", "financial", _p(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")),
    Detector("bank account number", "financial", _p(NB + r"\d{9,18}" + NA),
             context=("account", "a/c", "acct", "acc no", "account no"), confidence="medium"),
    Detector("UPI ID", "financial", _p(r"\b[\w.\-]{2,}@[a-zA-Z]{2,}\b(?![.\w])")),
    # --- contact
    Detector("email address", "contact", _p(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)*\.[a-zA-Z]{2,}\b")),
    Detector("phone number", "contact", _p(r"(?<![\w+])(?:\+91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?!\d)")),
    Detector("phone number", "contact", _p(r"(?<![\w+])0\d{2,4}[ -]\d{3,4}[ -]?\d{4}(?!\d)"), confidence="medium"),
    # --- personal
    Detector("date of birth", "personal",
             _p(r"\b(?:\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                r"[a-z]*\.?\s+\d{4})\b", re.I),
             context=("dob", "date of birth", "birth date", "born on", "d.o.b")),
]


def mask(kind: str, value: str) -> str:
    """Enough to recognise the item in a review list, never enough to reuse it."""
    v = value.strip()
    if kind == "private key":
        return "-----BEGIN … PRIVATE KEY----- (whole block)"
    if kind == "person name":
        return " ".join(w[0] + "." for w in v.split() if w[:1].isalpha())
    if kind == "postal address":
        return f"{v.split(',')[0][:12]}… ({len(v)} characters)"
    digits = _digits(v)
    if len(digits) >= 8 and len(digits) >= len(v) * 0.6:
        return "X" * (len(digits) - 4) + digits[-4:]
    if kind in ("email address", "UPI ID"):           # only these: a password with "@" in it
        user, _, domain = v.partition("@")              # once showed as "T…@Pass610"
        return f"{user[:1]}…@{domain}"
    if kind in ("password or secret", "password in a connection string"):
        return "•" * 8                                  # nothing of a password is safe to show
    if len(v) <= 6:
        return "•" * len(v)
    return f"{v[:3]}…{v[-4:]}"


def find_in_text(text: str) -> list[dict]:
    """Every detection in one page's text, as (start, end) spans of the sensitive value."""
    found, taken = [], []
    for det in DETECTORS:
        for m in det.pattern.finditer(text):
            start, end = m.span(det.group)
            value = m.group(det.group)
            if any(s < end and start < e for s, e in taken):
                continue
            before = text[max(0, start - CONTEXT_CHARS):start].lower()
            if det.context and not any(w in before for w in det.context):
                continue
            if det.check and not det.check(value):
                continue
            taken.append((start, end))
            found.append({"kind": det.kind, "category": det.category, "value": value,
                          "start": start, "end": end,
                          "basis": ("checksum valid" if det.check else
                                    "pattern and context" if det.context else "pattern"),
                          "confidence": det.confidence})
    return sorted(found, key=lambda f: f["start"])
