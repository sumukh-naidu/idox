"""ocr_layout.py -- OCR-geometry layout for the IMAGE -> Word route.

The model only guesses align/size from a picture. Tesseract gives the real position of every line, so alignment
is MEASURED: line centre vs the text area's centre -> center, right edge flush -> right, else left. Each model block
is matched to the OCR line that holds its first words (from the model's side, so table rows can never be taken for
headings). Text is never changed: only align, the size label, and all-empty table rows. See image_to_word.py.
"""
import difflib, hashlib, os, re, shutil, statistics, subprocess
from collections import Counter
import pytesseract
from PIL import Image
from pytesseract import Output
from blocks import Page, TableBlock

CENTRE_TOL = 0.03         # line centre within 3% of the text-area width from the area centre
EDGE_TOL = 0.03           # right edge within 3% of the area's right edge
MIN_GAP = 0.06            # a centred line must leave >=6% free on BOTH sides (full-width lines are left)
RIGHT_MIN_LEFT_GAP = 0.20
MATCH_MIN = 0.60
PAGE_CENTRE_TOL = 0.05   # a full-width line centred on the PAGE must be within 5% of the page width of its middle
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
                      "top": min(w[1] for w in ws), "bottom": max(w[1] + w[3] for w in ws),
                      "h": hs[int(0.75 * (len(hs) - 1))],
                      "text": " ".join(w[4] for w in ws)})
    lines.sort(key=lambda l: (l["top"], l["x0"]))
    return img.size, lines

def text_area(lines):
    # TRUE extent (leftmost start, rightmost end). A percentile cut drops the one widest line on pages with few
    # lines and shifted the centre by 10 px, which made a centred title look right-aligned.
    return min(l["x0"] for l in lines), max(l["x1"] for l in lines)

def classify(line, L, R, page_w=None):
    W = R - L
    cx = (line["x0"] + line["x1"]) / 2
    lgap, rgap = line["x0"] - L, R - line["x1"]
    if abs(cx - (L + R) / 2) <= CENTRE_TOL * W and lgap > MIN_GAP * W and rgap > MIN_GAP * W:
        return "center"
    if rgap <= EDGE_TOL * W and lgap > RIGHT_MIN_LEFT_GAP * W:
        return "right"
    if lgap <= MIN_GAP * W and rgap <= MIN_GAP * W:
        # Fills the whole text area, so centred and left look the same INSIDE it (and a widest line defines the area
        # itself). The PAGE decides: a centred line sits on the page's middle; one that starts at the text margin but
        # is off-centre on the page is left-aligned. Only a line that is also centred on the page stays ambiguous.
        if page_w and abs(cx - page_w / 2) > PAGE_CENTRE_TOL * page_w:
            return "left"
        return "full"
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
    info = {"L": L, "R": R, "area_centre": (L + R) / 2, "page_centre": iw / 2, "n_lines": len(lines)}

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
        al = classify(ln, L, R, iw)
        if al == "full":            # ambiguous: keep the model's value for a heading, plain paragraphs are left
            al = b.align if b.kind == "heading" else "left"
        ratios[bi] = ln["h"] / body
        rows.append((bi, b.kind, b.text[:40], b.align, al, f"{ratios[bi]:.2f}"))
        new_blocks.append(b.model_copy(update={"align": al, "size": "normal"}))
    return Page(analysis=page.analysis, blocks=new_blocks), rows, ratios, info

def _heading_levels(page, ratios, max_chars=90):
    """block index -> measured size ratio for the blocks that may be resized.
    Only blocks the model called 'heading', or SHORT single-line blocks (<= max_chars) measuring >= 1.2x the body
    (a long body paragraph is NEVER touched: one was wrongly enlarged to 14 pt before this rule). Measurements within
    0.12 of each other are one heading level and share their average."""
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
    return levels


def apply_sizes_pages(docx_path, pages_ratios, body_pt=11.0, max_chars=90):
    """Runs on the finished .docx (the writer only knows three size labels): gives HEADINGS the size measured from
    the image. pages_ratios is [(page, ratios), ...] in document order, one entry per measured page; paragraphs are
    matched to blocks by text, walking forward through the document so repeated text on another page is not confused."""
    from docx import Document
    from docx.shared import Pt
    doc = Document(docx_path)
    paras = [p for p in doc.paragraphs if p.text.strip()]
    ptr = 0
    for page, ratios in pages_ratios:
        levels = _heading_levels(page, ratios, max_chars)
        for bi in sorted(levels):
            text = page.blocks[bi].text.strip()
            for pi in range(ptr, len(paras)):
                if paras[pi].text.strip() == text:
                    pt = round(max(body_pt, min(body_pt * levels[bi], body_pt * 2.2)) * 2) / 2
                    for run in paras[pi].runs:
                        run.font.size = Pt(pt)
                    ptr = pi + 1
                    break
    doc.save(docx_path)


def apply_sizes(docx_path, page, ratios, body_pt=11.0, max_chars=90):
    """Single-page form of apply_sizes_pages (used by image_to_word.py)."""
    apply_sizes_pages(docx_path, [(page, ratios)], body_pt, max_chars)


# ======================================================================================================================
# Scanned DOCUMENTS (PDF route): margins, paragraph spacing and inserted pictures measured from the scan
#
# The model returns text only, so a scanned page came out with 1-inch margins, paragraphs packed together, and any
# picture on it missing. Tesseract's line positions give the real left/top/bottom edge, the real line pitch, and the gap
# before every block; the PDF's own image objects give the pictures. Measured on two scans, this took the mean position
# error of a block from 0.3-0.55 in to 0.02-0.06 in. Text is never changed.
# ======================================================================================================================
PAGE_AREA_FRACTION = 0.85   # an image covering more of the page than this IS the scanned page, not a picture on it
PICTURES_MAX_SHARE = 0.5    # several images that together cover more than this are tiles of the scan, not pictures
BOTTOM_SLACK_PT = 14.0      # a line box is taller than its ink; without slack the last line spills onto a new page
MAX_BOTTOM_PT = 72.0        # the empty space under a SHORT page is not a margin: never reserve more than an inch
TOP_ADJ_FACTOR = 0.3        # the first line's ink starts about this fraction of the body size below the top margin
BODY_PT = 11.0
REFINE_ROUNDS = 2           # --refine-layout: render, measure, correct this many times


def _match_all(page, lines):
    """block index -> index of the OCR line holding its first words (text blocks only)."""
    used, idx = set(), {}
    for bi, b in enumerate(page.blocks):
        if isinstance(b, TableBlock):
            continue
        li, _score = match_block(b, lines, used)
        if li is not None:
            used.add(li)
            idx[bi] = li
    return idx


def _table_cells(page):
    return {norm(str(c)) for b in page.blocks if isinstance(b, TableBlock)
            for c in list(b.header) + [x for r in b.rows for x in r]}


def _is_table_line(line, cells):
    n = norm(line["text"])
    return sum(1 for c in cells if c and c in n) >= 2


def scan_pictures(pdf_doc, pno, out_dir, scale=1.0):
    """Pictures printed on a scanned page: image objects clearly smaller than the page, saved exactly as stored.

    A scan's page-sized image is the page itself and is never a picture. Several images that together cover half
    the page are tiles of the scan, so those are ignored too. `scale` converts the PDF's declared points to real
    ones (some scanners declare the page in pixels)."""
    page = pdf_doc[pno]
    area = page.rect.width * page.rect.height
    infos = page.get_image_info(xrefs=True)
    if not any((i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1]) >= PAGE_AREA_FRACTION * area
               for i in infos):
        return []
    pics = []
    for k, im in enumerate(infos):
        x0, y0, x1, y1 = im["bbox"]
        if ((x1 - x0) * (y1 - y0) >= PAGE_AREA_FRACTION * area or not im.get("xref")
                or im["width"] < 40 or im["height"] < 40 or (x1 - x0) < 28 or (y1 - y0) < 28):
            continue
        try:
            data = pdf_doc.extract_image(im["xref"])
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"p{pno + 1:03d}_scanpic{k:02d}.{data.get('ext', 'png')}")
            with open(path, "wb") as fh:
                fh.write(data["image"])
        except Exception:
            continue
        pics.append({"path": path, "ext": data.get("ext", "png"), "bytes": len(data["image"]),
                     "sha1": hashlib.sha1(data["image"]).hexdigest(), "px": (data["width"], data["height"]),
                     "bbox_pt": (x0 * scale, y0 * scale, x1 * scale, y1 * scale)})
    if sum((p["bbox_pt"][2] - p["bbox_pt"][0]) * (p["bbox_pt"][3] - p["bbox_pt"][1]) for p in pics) \
            > PICTURES_MAX_SHARE * area * scale * scale:
        return []
    return sorted(pics, key=lambda p: (p["bbox_pt"][1], p["bbox_pt"][0]))


def _plan(pages, img_paths, page_w_pt, page_h_pt, dpi, scale):
    """Measure the scan: edges, line pitch, and the gap before each block (all in points)."""
    sc = 72.0 / dpi * scale          # points per pixel of the rendered scan
    plan = {"sc": sc, "w": page_w_pt, "h": page_h_pt, "pages": []}
    lefts, tops, rights, bottoms, deltas = [], [], [], [], []
    for page, png in zip(pages, img_paths):
        _size, lines = ocr_lines(png)
        if not lines:
            raise ValueError("OCR found no text on a scanned page")
        idx = _match_all(page, lines)
        cells = _table_cells(page)
        order = sorted(idx.items())
        for k, (bi, li) in enumerate(order):
            nxt = order[k + 1][1] if k + 1 < len(order) else len(lines)
            for j in range(li, nxt - 1):
                if not _is_table_line(lines[j], cells) and not _is_table_line(lines[j + 1], cells):
                    d = lines[j + 1]["top"] - lines[j]["top"]
                    if d > 0:
                        deltas.append(d)
        body = [l for l in lines if not _is_table_line(l, cells)] or lines
        lefts.append(min(l["x0"] for l in lines))
        tops.append(lines[order[0][1]]["top"] if order else lines[0]["top"])
        rights.append(max(l["x1"] for l in body))
        bottoms.append(max(l["bottom"] for l in lines))
        plan["pages"].append({"lines": lines, "idx": idx, "order": order, "sb": {}, "png": png})
    # The line pitch is the TIGHTEST repeated gap between lines of one block. Gaps far beyond the text height are not
    # line gaps (OCR splits some table rows into fragments, which then look like a 250 px gap), so they are ignored
    # first, and the lower quartile is taken instead of the median.
    heights = [l["bottom"] - l["top"] for info in plan["pages"] for l in info["lines"]]
    hmed = statistics.median(heights)
    deltas = sorted([d for d in deltas if 0.9 * hmed <= d <= 3.2 * hmed] or deltas)
    pitch_px = deltas[int(0.25 * (len(deltas) - 1))] if deltas else 1.5 * hmed
    for info in plan["pages"]:
        lines = info["lines"]
        for k, (bi, li) in enumerate(info["order"]):
            info["sb"][bi] = 0.0 if k == 0 else max(0.0, lines[li]["top"] - lines[li - 1]["top"] - pitch_px) * sc
    plan.update(pitch=pitch_px * sc, left=min(lefts) * sc, top=min(tops) * sc, right_edge=max(rights) * sc,
                bottom_edge=max(bottoms) * sc)
    return plan


def _single_column(lines, page_w_px):
    """True when no two lines share a row while sitting apart horizontally (that would be columns or a table)."""
    for i, a in enumerate(lines):
        for b in lines[i + 1:]:
            if b["top"] - a["top"] > 0.5 * (a["bottom"] - a["top"]):
                break
            if min(a["x1"], b["x1"]) < max(a["x0"], b["x0"]) - 0.05 * page_w_px:
                return False
    return True


def _reading_order(pages, ratios_list, img_paths):
    """The model sometimes returns a block out of order (a header at the top right of the scan AFTER the body).
    Where the page is one column of plain text and every block was found on the scan, sort the blocks top to bottom."""
    out_pages, out_ratios = [], []
    for page, ratios, png in zip(pages, ratios_list, img_paths):
        n = len(page.blocks)
        if n > 1 and not any(isinstance(b, TableBlock) for b in page.blocks):
            size, lines = ocr_lines(png)
            idx = _match_all(page, lines) if lines else {}
            if len(idx) == n and _single_column(lines, size[0]):
                order = sorted(range(n), key=lambda bi: idx[bi])
                if order != list(range(n)):
                    rm = {old: new for new, old in enumerate(order)}
                    page = Page(analysis=page.analysis, blocks=[page.blocks[i] for i in order])
                    ratios = {rm[k]: v for k, v in ratios.items()}
        out_pages.append(page)
        out_ratios.append(ratios)
    return out_pages, out_ratios


def _drop_picture_text(pages, ratios_list, plan, pics_by_page):
    """Text the model read OUT OF a picture is a duplicate of that picture: remove those blocks, re-index the rest."""
    sc = plan["sc"]
    new_pages, new_ratios = [], []
    for page, ratios, info, pics in zip(pages, ratios_list, plan["pages"], pics_by_page):
        boxes = [tuple(v / sc for v in p["bbox_pt"]) for p in pics]
        keep = []
        for bi in range(len(page.blocks)):
            li = info["idx"].get(bi)
            inside = False
            if li is not None:
                ln = info["lines"][li]
                cx, cy = (ln["x0"] + ln["x1"]) / 2, (ln["top"] + ln["bottom"]) / 2
                inside = any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)
            if not inside:
                keep.append(bi)
        if len(keep) != len(page.blocks):
            rm = {o: n for n, o in enumerate(keep)}
            page = Page(analysis=page.analysis, blocks=[page.blocks[i] for i in keep])
            ratios = {rm[k]: v for k, v in ratios.items() if k in rm}
            info["idx"] = {rm[k]: v for k, v in info["idx"].items() if k in rm}
            info["sb"] = {rm[k]: v for k, v in info["sb"].items() if k in rm}
            info["order"] = [(rm[bi], li) for bi, li in info["order"] if bi in rm]
            if info["sb"] and info["order"]:
                info["sb"][info["order"][0][0]] = 0.0
        new_pages.append(page)
        new_ratios.append(ratios)
    return new_pages, new_ratios


def _picture_plan(pages, plan, pics_by_page):
    """Where each picture goes: the block it follows, the gap above it and its left offset, all from the scan."""
    sc = plan["sc"]
    image_sets, flat = [], []
    for page, info, pics in zip(pages, plan["pages"], pics_by_page):
        n = len(page.blocks)
        entries = []
        for p in pics:
            x0, y0, x1, y1 = p["bbox_pt"]
            top_px = y0 / sc
            after = n
            for bi, li in info["order"]:
                if info["lines"][li]["top"] >= top_px:
                    after = bi
                    break
            above = [l["bottom"] * sc for l in info["lines"] if l["bottom"] * sc <= y0]
            p["gap_pt"] = max(0.0, y0 - max(above)) if above else 0.0
            p["after_block"] = after
            entries.append({"path": p["path"], "sha1": p["sha1"], "bytes": p["bytes"], "ext": p["ext"],
                            "bbox": (y0, y1), "width_in": (x1 - x0) / 72.0, "height_in": (y1 - y0) / 72.0,
                            "px": p["px"], "frac_above": after / n if n else 0.0,
                            "text_before": None, "text_after": None})
            flat.append(p)
        image_sets.append(entries)
    return image_sets, flat


def _layout(plan, top_adj):
    w, h = plan["w"], plan["h"]
    return {"width_pt": w, "height_pt": h, "left_pt": plan["left"],
            "right_pt": max(18.0, w - plan["right_edge"]),
            "top_pt": max(18.0, plan["top"] - top_adj),
            "bottom_pt": min(MAX_BOTTOM_PT, max(10.0, h - plan["bottom_edge"] - BOTTOM_SLACK_PT)),
            "body_pt": BODY_PT}


def _apply_spacing(docx_path, pages, plan, sb_override, pics_flat, pic_adj, layout):
    from docx import Document
    from docx.enum.text import WD_LINE_SPACING
    from docx.shared import Pt
    doc = Document(docx_path)
    paras = [p for p in doc.paragraphs if p.text.strip()]
    ptr = 0
    for pi, (page, info) in enumerate(zip(pages, plan["pages"])):
        for bi, b in enumerate(page.blocks):
            if isinstance(b, TableBlock) or bi not in info["sb"] or b.kind == "list":
                continue
            text = b.text.strip()
            for q in range(ptr, len(paras)):
                if paras[q].text.strip() == text:
                    pf = paras[q].paragraph_format
                    pf.space_before = Pt(max(0.0, sb_override.get((pi, bi), info["sb"][bi])))
                    # a paragraph directly above a table keeps the writer's gap: tables carry no space of their own
                    pf.space_after = Pt(6 if bi + 1 < len(page.blocks) and isinstance(page.blocks[bi + 1], TableBlock) else 0)
                    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
                    pf.line_spacing = Pt(plan["pitch"])
                    ptr = q + 1
                    break
    drawings = [p for p in doc.paragraphs if p._p.xpath(".//w:drawing")]
    for para, pic in zip(drawings, pics_flat):
        adj = pic_adj.get(pic["path"], (0.0, 0.0))
        para.paragraph_format.left_indent = Pt(max(0.0, pic["bbox_pt"][0] - layout["left_pt"] + adj[0]))
        para.paragraph_format.space_before = Pt(max(0.0, pic["gap_pt"] + adj[1]))
    doc.save(docx_path)


def _build(docx_path, pages, ratios_list, plan, image_sets, pics_flat, sb_override, top_adj, pic_adj):
    from to_docx import build_docx
    layout = _layout(plan, top_adj)
    problems = build_docx(pages, docx_path, layout=layout, image_sets=image_sets)
    apply_sizes_pages(docx_path, list(zip(pages, ratios_list)), body_pt=BODY_PT)
    _apply_spacing(docx_path, pages, plan, sb_override, pics_flat, pic_adj, layout)
    return problems


def _render(docx_path, dpi):
    """Word -> PDF (LibreOffice) -> one PNG per page, for measuring."""
    import pymupdf
    out = os.path.dirname(os.path.abspath(docx_path))
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", out, docx_path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    pdf_path = os.path.splitext(os.path.abspath(docx_path))[0] + ".pdf"
    pngs = []
    try:
        d = pymupdf.open(pdf_path)
        for i, pg in enumerate(d):
            png = f"{os.path.splitext(pdf_path)[0]}_refine_p{i + 1}.png"
            pg.get_pixmap(dpi=dpi).save(png)
            pngs.append(png)
        d.close()
    finally:
        for f in (pdf_path,):
            if os.path.exists(f):
                os.remove(f)
    return pngs


def _dominant_colour(png, box_px):
    im = Image.open(png).convert("RGB").crop(tuple(int(v) for v in box_px))
    cnt = Counter(c for c in im.getdata() if min(c) <= 215)
    if not cnt:
        return None
    col, n = cnt.most_common(1)[0]
    return col if n >= 0.3 * (im.width * im.height) else None     # a photo has no single colour: do not chase it


def _find_box(png, col):
    im = Image.open(png).convert("RGB")
    sm = im.resize((max(1, im.width // 3), max(1, im.height // 3)))
    px = sm.load()
    xs, ys = [], []
    for y in range(sm.height):
        for x in range(sm.width):
            r, g, b = px[x, y]
            if abs(r - col[0]) < 14 and abs(g - col[1]) < 14 and abs(b - col[2]) < 14:
                xs.append(x); ys.append(y)
    return (min(xs) * 3, min(ys) * 3) if xs else None


def build_scanned_docx(pages, ratios_list, img_paths, pdf_doc, pnos, docx_path, dpi, page_size_pt, scale=1.0,
                       image_dir=None, refine=False, log=print):
    """Write ONE .docx for a fully scanned PDF, laid out like the scan. Returns the writer's problems list.

    pages / ratios_list come from apply_ocr_layout() (alignment already corrected), img_paths are the rendered scan
    pages the model read, pnos their page numbers in pdf_doc. With refine=True the file is also rendered with
    LibreOffice, every block is compared with the scan, and the gaps are corrected (REFINE_ROUNDS times)."""
    pages, ratios_list = _reading_order(pages, ratios_list, img_paths)
    plan = _plan(pages, img_paths, page_size_pt[0], page_size_pt[1], dpi, scale)
    pics_by_page = [scan_pictures(pdf_doc, pno, image_dir, scale) if image_dir else [] for pno in pnos]
    n_pics = sum(len(p) for p in pics_by_page)
    if n_pics:
        pages, ratios_list = _drop_picture_text(pages, ratios_list, plan, pics_by_page)
        for info, pics in zip(plan["pages"], pics_by_page):
            if pics:
                plan["top"] = min(plan["top"], min(p["bbox_pt"][1] for p in pics))
                plan["bottom_edge"] = max(plan["bottom_edge"], max(p["bbox_pt"][3] for p in pics))
    image_sets, pics_flat = _picture_plan(pages, plan, pics_by_page)
    log(f"  scan layout: pitch {plan['pitch']:.1f}pt, left {plan['left'] / 72:.2f}in, top {plan['top'] / 72:.2f}in, "
        f"{n_pics} picture(s) kept from the scan")

    sb_override, top_adj, pic_adj = {}, TOP_ADJ_FACTOR * BODY_PT, {}
    cols = {}
    if refine and shutil.which("soffice"):
        for p in pics_flat:
            pi = next(i for i, s in enumerate(image_sets) if any(e["path"] == p["path"] for e in s))
            cols[p["path"]] = _dominant_colour(plan["pages"][pi]["png"], tuple(v / plan["sc"] for v in p["bbox_pt"]))
        rounds = REFINE_ROUNDS
    else:
        if refine:
            log("  ! --refine-layout needs LibreOffice (soffice); skipping the refinement")
        rounds = 0
    problems = []
    for rnd in range(rounds + 1):
        problems = _build(docx_path, pages, ratios_list, plan, image_sets, pics_flat, sb_override, top_adj, pic_adj)
        if rnd == rounds:
            break
        pngs = _render(docx_path, dpi)
        worst = 0.0
        for pi, (page, info) in enumerate(zip(pages, plan["pages"])):
            if pi >= len(pngs):
                break
            _s, wl = ocr_lines(pngs[pi])
            widx = _match_all(page, wl) if wl else {}
            errs = {bi: wl[widx[bi]]["top"] * 72.0 / dpi - info["lines"][li]["top"] * plan["sc"]
                    for bi, li in info["idx"].items() if bi in widx}
            last = None
            for bi in sorted(errs):
                e = errs[bi]
                worst = max(worst, abs(e))
                if last is None and pi == 0:
                    top_adj += e
                if last is not None:
                    sb_override[(pi, bi)] = max(0.0, sb_override.get((pi, bi), info["sb"].get(bi, 0.0)) - (e - last))
                last = e
        for pic in pics_flat:
            col = cols.get(pic["path"])
            pi = next(i for i, s in enumerate(image_sets) if any(e["path"] == pic["path"] for e in s))
            if col and pi < len(pngs):
                bb = _find_box(pngs[pi], col)
                if bb:
                    adj = pic_adj.get(pic["path"], (0.0, 0.0))
                    pic_adj[pic["path"]] = (adj[0] - (bb[0] * 72.0 / dpi - pic["bbox_pt"][0]),
                                            adj[1] - (bb[1] * 72.0 / dpi - pic["bbox_pt"][1]))
        log(f"  refine round {rnd + 1}: largest block offset {worst / 72:.2f}in")
        for f in pngs:
            if os.path.exists(f):
                os.remove(f)
    return problems
