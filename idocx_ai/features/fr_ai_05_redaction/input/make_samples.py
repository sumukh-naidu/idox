"""Generate the FR-AI-05 redaction test set. Every person, number and key is FAKE.

    .venv/bin/python features/fr_ai_05_redaction/input/make_samples.py

IDs are generated to pass their real checksums (so detectors must accept them), and
decoys are generated to FAIL them (so detectors must reject them). Fake secrets are
assembled at runtime rather than written out here, so secret scanners do not flag
this source file.
"""

import base64
import hashlib
import json
import random
import re
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent
A4 = (595, 842)
rnd = random.Random(2026)

_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
      [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
      [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
      [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
      [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
      [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]
_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def verhoeff_digit(body: str) -> str:
    c = 0
    for i, d in enumerate(reversed(body)):
        c = _D[c][_P[(i + 1) % 8][int(d)]]
    return str(_INV[c])


def gstin(prefix14: str) -> str:
    chars, total = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0
    for i, ch in enumerate(prefix14):
        p = chars.index(ch) * (2 if i % 2 else 1)
        total += p // 36 + p % 36
    return prefix14 + chars[(36 - total % 36) % 36]


def group4(d: str) -> str:
    return " ".join(d[i:i + 4] for i in range(0, len(d), 4))


def token(alphabet: str, n: int) -> str:
    return "".join(rnd.choice(alphabet) for _ in range(n))


ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

aadhaar_body = "8" + "".join(rnd.choice("0123456789") for _ in range(10))
AADHAAR = group4(aadhaar_body + verhoeff_digit(aadhaar_body))
AADHAAR_DECOY = AADHAAR[:-1] + str((int(AADHAAR[-1]) + 1) % 10)            # fails Verhoeff
vid_body = "".join(rnd.choice("0123456789") for _ in range(15))
VID = group4(vid_body + verhoeff_digit(vid_body))
GSTIN = gstin("27ABCPE1234F1Z")
CARD, CARD_DECOY = "4111 1111 1111 1111", "4111 1111 1111 1112"          # test card number; decoy fails Luhn

SECRETS = {
    "AWS access key": "AKIA" + "IOSFODNN7" + "EXAMPLE",                   # AWS's documented example key
    "GitHub token": "gh" + "p_" + token(ALNUM, 36),
    "API secret key": "sk" + "-proj-" + token(ALNUM, 40),
    "Google API key": "AI" + "za" + token(ALNUM + "-_", 35),
    "Slack token": "xo" + "xb-" + token("0123456789", 12) + "-" + token(ALNUM, 24),
    "JWT / access token": ".".join(base64.urlsafe_b64encode(s.encode()).decode().rstrip("=") for s in
                                   ('{"alg":"HS256","typ":"JWT"}', '{"sub":"test-user"}', token(ALNUM, 16))),
    "bearer token": token(ALNUM, 40),
    "password or secret": "Test@Pass" + token("0123456789", 3),
    "password in a connection string": "S3cret" + token(ALNUM, 6),
}
# Key markers are split so the source itself does not look like a key to secret scanners.
KEY_BEGIN, KEY_END = "-----BEGIN RSA " + "PRIVATE KEY-----", "-----END RSA " + "PRIVATE KEY-----"
KEY_LINES = ["MII" + token(ALNUM + "+/", 61) for _ in range(4)]

FORM = [
    ("Customer onboarding form (TEST DATA - all values fake)", True),
    ("Name: Ravi Kumar Testcase", False),
    ("Address: Flat 12, Test Residency, MG Road, Bengaluru 560001", False),
    ("Date of Birth: 14/08/1991", False),
    (f"Aadhaar: {AADHAAR}", False),
    (f"VID: {VID}", False),
    ("PAN: ABCPE1234F", False),
    ("Passport No: P1234567", False),
    ("Mobile: +91 98765 43210", False),
    ("Landline: 022-2345 6789", False),
    ("Email: ravi.testcase@example.com", False),
    ("Bank account no: 001234567890   IFSC: TEST0001234", False),
    ("UPI: ravi.test@okaxis", False),
    (f"Card number: {CARD}", False),
    (f"GSTIN: {GSTIN}", False),
    ("", False),
    ("Office use only", True),
    (f"Application reference: {AADHAAR_DECOY}", False),
    (f"Order number: {CARD_DECOY}", False),
    ("Invoice no 123456789012, form version 2.3, printed on 01/10/2026", False),
    ("Reference code K7654321. Branch PIN code 560001.", False),
    ("Please choose a strong password for your account.", False),
]

IT_SHEET = [
    ("IT access sheet (TEST DATA - all keys fake)", True),
    (f"AWS access key id: {SECRETS['AWS access key']}", False),
    (f"GITHUB_TOKEN={SECRETS['GitHub token']}", False),
    (f"OPENAI_API_KEY={SECRETS['API secret key']}", False),
    (f"maps key: {SECRETS['Google API key']}", False),
    (f"slack bot: {SECRETS['Slack token']}", False),
    ("session token:", False),                       # the token on its own line: a long line runs off the page,
    (SECRETS["JWT / access token"], False),          # and text outside the page is not extractable
    (f"Authorization: Bearer {SECRETS['bearer token']}", False),
    (f"DB_PASSWORD={SECRETS['password or secret']}", False),
    (f"DATABASE_URL=postgres://app_user:{SECRETS['password in a connection string']}@db.internal.test:5432/app", False),
    (KEY_BEGIN, False),
    *[(ln, False) for ln in KEY_LINES],
    (KEY_END, False),
    ("", False),
    ("Not secrets:", True),
    ("token: required for every API call", False),
    ("commit 9fceb02d0ae598e95dc970b74767f19372d61af8", False),
    ("request id 550e8400-e29b-41d4-a716-446655440000", False),
]


def page(doc, lines, size=10.5):
    p = doc.new_page(width=A4[0], height=A4[1])
    y = 70
    for text, bold in lines:
        if text:
            p.insert_text((60, y), text, fontsize=13 if bold else size, fontname="hebo" if bold else "helv")
        y += 24 if bold else 21
    return p


def save(doc, name):
    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "Info", "null")
    doc.update_object(cat, re.sub(r"/Info\s*null", "", doc.xref_object(cat, compressed=True)))
    doc.save(OUT / name, garbage=3, deflate=True)


if __name__ == "__main__":
    doc = pymupdf.open()
    page(doc, FORM)
    page(doc, IT_SHEET, size=9)
    save(doc, "redaction_form.pdf")

    scan = pymupdf.open()                             # page 1 only, as an image with no text layer
    pix = doc[0].get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
    sp = scan.new_page(width=A4[0], height=A4[1])
    sp.insert_image(sp.rect, stream=pix.tobytes("jpeg", jpg_quality=85))
    save(scan, "redaction_scanned.pdf")

    expected_p1 = [
        ("date of birth", "14/08/1991"), ("Aadhaar number", AADHAAR), ("Aadhaar VID", VID), ("PAN", "ABCPE1234F"),
        ("passport number", "P1234567"), ("phone number", "+91 98765 43210"), ("phone number", "022-2345 6789"),
        ("email address", "ravi.testcase@example.com"), ("bank account number", "001234567890"),
        ("IFSC code", "TEST0001234"), ("UPI ID", "ravi.test@okaxis"), ("card number", CARD), ("GSTIN", GSTIN),
    ]
    # Fake secrets go into the ground truth as fingerprints, not text: GitHub push protection cannot tell
    # a fake token from a real one, and a flagged string in a commit means rewriting history.
    fingerprint = lambda v: {"sha256": hashlib.sha256(re.sub(r"[\s\-]", "", v).lower().encode()).hexdigest()}
    expected_p2 = [(k, fingerprint(v)) for k, v in SECRETS.items()] + [("private key", "PRIVATE KEY")]
    truth = {
        "_comment": "FR-AI-05 test set. Every value is FAKE: IDs pass their checksums, decoys fail them.",
        "redaction_form.pdf": {"pages": 2,
                               "expected": {"1": expected_p1, "2": expected_p2},
                               "must_not_flag": [AADHAAR_DECOY, CARD_DECOY, "123456789012", "01/10/2026",
                                                 "K7654321", "560001", "required",
                                                 "9fceb02d0ae598e95dc970b74767f19372d61af8",
                                                 "550e8400-e29b-41d4-a716-446655440000"],
                               "for_the_model": {"names": ["Ravi Kumar Testcase"],
                                                 "addresses": ["Flat 12, Test Residency, MG Road, Bengaluru 560001"]}},
        "redaction_scanned.pdf": {"pages": 1, "text_layer": False, "expected": {"1": expected_p1}},
    }
    (OUT / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    for n in ("redaction_form.pdf", "redaction_scanned.pdf"):
        print(f"{n:<24} {(OUT / n).stat().st_size // 1024:>5} KB")
