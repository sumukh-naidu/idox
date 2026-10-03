"""ocr_layout.py -- OCR-geometry layout for the IMAGE -> Word route.

The model only guesses align/size from a picture. Tesseract gives the real position of every line, so alignment
is MEASURED: line centre vs the text area's centre -> center, right edge flush -> right, else left. Each model block
is matched to the OCR line that holds its first words (from the model's side, so table rows can never be taken for
headings). Text is never changed: only align, the size label, and all-empty table rows. See image_to_word.py.
"""
import difflib, re, statistics
import pytesseract
from PIL import Image
from pytesseract import Output
from blocks import Page, TableBlock

CENTRE_TOL = 0.03         # line centre within 3% of the text-area width from the area centre
EDGE_TOL = 0.03           # right edge within 3% of the area's right edge
MIN_GAP = 0.06            # a centred line must leave >=6% free on BOTH sides (full-width lines are left)
RIGHT_MIN_LEFT_GAP = 0.20
MATCH_MIN = 0.60
DESCENDERS = set('gjpqyQ,;()[]{}|')

def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())

def ocr_lines(image_path):
    img = Image.open(image_path).convert("RGB")
    d = pytesseract.image_to_data(img, output_type=Output.DICT, config="--psm 3")
    groups = {}
    for i, txt in enumerate(d["text"]):
        if not txt.strip():
            continue
        try:
            if float(d["conf"][i]) < 30:
                continue
        except ValueError:
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        groups.setdefault(key, []).append((d["left"][i], d["top"][i], d["width"][i], d["height"][i], txt))
    lines = []
    for ws in groups.values():
        ws.sort(key=lambda w: w[0])
        # letters hanging below the baseline stretch a word's box downwards ("Key points" read 1.7x the body
        # when it is the same size as its sibling heading), so those words are discounted
        hs = sorted(w[3] * (0.80 if any(c in DESCENDERS for c in w[4]) else 1.0) for w in ws)
        lines.append({"x0": min(w[0] for w in ws), "x1": max(w[0] + w[2] for w in ws),
                      "top": min(w[1] for w in ws), "h": hs[int(0.75 * (len(hs) - 1))],
                      "text": " ".join(w[4] for w in ws)})
    lines.sort(key=lambda l: (l["top"], l["x0"]))
    return img.size, lines

def text_area(lines):
    # TRUE extent (leftmost start, rightmost end). A percentile cut drops the one widest line on pages with few
    # lines and shifted the centre by 10 px, which made a centred title look right-aligned.
    return min(l["x0"] for l in lines), max(l["x1"] for l in lines)

def classify(line, L, R):
    W = R - L
    cx = (line["x0"] + line["x1"]) / 2
    lgap, rgap = line["x0"] - L, R - line["x1"]
    if abs(cx - (L + R) / 2) <= CENTRE_TOL * W and lgap > MIN_GAP * W and rgap > MIN_GAP * W:
        return "center"
    if rgap <= EDGE_TOL * W and lgap > RIGHT_MIN_LEFT_GAP * W:
        return "right"
    if lgap <= MIN_GAP * W and rgap <= MIN_GAP * W:
        return "full"      # fills the column: centred and left look identical, geometry cannot decide
    return "left"

def match_block(block, lines, used):
    key = norm(block.text)[:25]
    if len(key) < 4:
        return None, 0.0
    best, best_s = None, 0.0
    for i, ln in enumerate(lines):
        if i in used:
            continue
        t = norm(ln["text"])
        if len(t) < 3:
            continue
        m = difflib.SequenceMatcher(None, key, t, autojunk=False).find_longest_match(0, len(key), 0, len(t))
        s = m.size / min(len(key), len(t))
        if s > best_s:
            best, best_s = i, s
    return (best, best_s) if best_s >= MATCH_MIN else (None, best_s)

def apply_ocr_layout(page, image_path):
    """Returns (new_page, report_rows, ratios, info). ratios maps block index -> line height / body height.
    Text is never changed: only align, size label and all-empty table rows."""
    (iw, ih), lines = ocr_lines(image_path)
    if not lines:
        return page, [], {}, {}
    L, R = text_area(lines)
    info = {"L": L, "R": R, "area_centre": (L + R) / 2, "n_lines": len(lines)}

    used, matched = set(), {}
    for bi, b in enumerate(page.blocks):
        if isinstance(b, TableBlock):
            continue            # matching goes from the model's side, so table rows can never look like headings
        li, score = match_block(b, lines, used)
        if li is not None:
            used.add(li)
            matched[bi] = (li, score)

    # Body size = whatever carries the most TEXT (same rule as the writer): a character-weighted median.
    def wmedian(pairs):
        pairs = sorted(pairs)
        total, acc = sum(w for _, w in pairs), 0
        for v, w in pairs:
            acc += w
            if acc >= total / 2:
                return v
    pool = [(lines[li]["h"], len(page.blocks[bi].text)) for bi, (li, _) in matched.items()
            if page.blocks[bi].kind in ("paragraph", "list")] \
        or [(lines[li]["h"], len(page.blocks[bi].text)) for bi, (li, _) in matched.items()]
    body = wmedian(pool) or 1
    info["body_h"] = body

    new_blocks, rows, ratios = [], [], {}
    for bi, b in enumerate(page.blocks):
        if isinstance(b, TableBlock):
            kept = [r for r in b.rows if any(str(c).strip() for c in r)]
            if len(kept) != len(b.rows):
                rows.append((bi, "table", f"dropped {len(b.rows) - len(kept)} all-empty row(s)", "", "", ""))
                b = b.model_copy(update={"rows": kept, "n_data_rows": len(kept)})
            new_blocks.append(b)
            continue
        if bi not in matched:
            rows.append((bi, b.kind, b.text[:40], b.align, "(no OCR match)", ""))
            new_blocks.append(b)    # unmatched blocks keep the model's values
            continue
        ln = lines[matched[bi][0]]
        al = classify(ln, L, R)
        if al == "full":            # ambiguous: keep the model's value for a heading, plain paragraphs are left
            al = b.align if b.kind == "heading" else "left"
        ratios[bi] = ln["h"] / body
        rows.append((bi, b.kind, b.text[:40], b.align, al, f"{ratios[bi]:.2f}"))
        new_blocks.append(b.model_copy(update={"align": al, "size": "normal"}))
    return Page(analysis=page.analysis, blocks=new_blocks), rows, ratios, info

def apply_sizes(docx_path, page, ratios, body_pt=11.0, max_chars=90):
    """Runs on the finished .docx (the writer only knows three size labels). Resizes HEADINGS to the measured size.
      - only blocks the model called 'heading', or SHORT single-line blocks (<= max_chars) measuring >= 1.2x the body,
        are resized; a long body paragraph is NEVER touched (one was wrongly enlarged to 14 pt before this rule);
      - measurements within 0.12 of each other are one heading level and share their average."""
    from docx import Document
    from docx.shared import Pt
    doc = Document(docx_path)
    paras = [p for p in doc.paragraphs if p.text.strip()]
    eligible = {}
    for bi, r in ratios.items():
        b = page.blocks[bi]
        short = len(b.text) <= max_chars and "\n" not in b.text.strip()
        if b.kind == "heading" or (short and r >= 1.2):
            eligible[bi] = r
    levels, cur = {}, []
    for bi in sorted(eligible, key=lambda k: eligible[k]):
        if cur and eligible[bi] - eligible[cur[-1]] > 0.12:
            for k in cur: levels[k] = sum(eligible[x] for x in cur) / len(cur)
            cur = []
        cur.append(bi)
    for k in cur: levels[k] = sum(eligible[x] for x in cur) / len(cur)
    used = set()
    for bi, r in levels.items():
        b = page.blocks[bi]
        for pi, p in enumerate(paras):
            if pi in used or p.text.strip() != b.text.strip():
                continue
            used.add(pi)
            pt = round(max(body_pt, min(body_pt * r, body_pt * 2.2)) * 2) / 2
            for run in p.runs:
                run.font.size = Pt(pt)
            break
    doc.save(docx_path)
