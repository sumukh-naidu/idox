"""Check the FR-AI-02 tools against samples/fr_ai_02_forms/ground_truth.json, without the model.

    .venv/bin/python tests/checks/fr_ai_02_forms.py

Files the tools write, and report.txt, go to tests/output/fr_ai_02_forms/ (cleared each run).
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from core import store  # noqa: E402
from core.pdfutil import ToolError  # noqa: E402
from features.fr_ai_02_forms import extract, forms  # noqa: E402

INPUT = ROOT / "samples" / "fr_ai_02_forms"
OUT = ROOT / "tests" / "output" / "fr_ai_02_forms"
shutil.rmtree(OUT, ignore_errors=True)
store.set_root(OUT / "files")

truth = json.loads((INPUT / "ground_truth.json").read_text())
failures, report = 0, []


def log(line=""):
    print(line)
    report.append(line)


def check(label, ok, detail=""):
    global failures
    failures += not ok
    log(f"  {'PASS' if ok else 'FAIL'}  {label}{'  -- ' + detail if detail else ''}")


def upload(name):
    return store.save_file((INPUT / name).read_bytes(), name, truth["pages"][name], source="test")["id"]


log("reading form fields")
for name, gt in truth["forms"].items():
    r = forms.read_form_fields(upload(name))
    names = [f["name"] for f in r["fields"]]
    check(f"{name}: every field found, in order", names == gt["fields"], f"{len(names)} fields")
    check(f"{name}: only signatures/declarations are user-only", r["user_only"] == gt["never_fill"], str(r["user_only"]))
    choice = [f for f in r["fields"] if f["type"] == "choice"]
    check(f"{name}: dropdown options read", all(f.get("options") for f in choice),
          "; ".join(f"{f['name']}: {f['options']}" for f in choice))

try:
    forms.read_form_fields(upload("invoice.pdf"))
    check("a flat PDF is refused", False)
except ToolError as e:
    check("a flat PDF is refused", "no fillable fields" in str(e), str(e)[:90])

doc = pymupdf.open()                                   # a field with no tooltip: label from printed text
pg = doc.new_page()
pg.insert_text((60, 114), "Father's name", fontsize=10)
w = pymupdf.Widget()
w.field_name, w.field_type, w.rect = "f1", pymupdf.PDF_WIDGET_TYPE_TEXT, pymupdf.Rect(200, 100, 400, 120)
pg.add_widget(w)
check("a field without a tooltip takes the label printed beside it",
      forms.form_fields(doc)[0]["label"] == "Father's name", forms.form_fields(doc)[0]["label"])

log("extraction: checking values against the source (no model)")
from features.fr_ai_02_forms import extract  # noqa: E402

inv = upload("invoice.pdf")
pages = extract.document_text(inv)
T, C, X = {"type": "text"}, {"type": "choice"}, {"type": "checkbox"}
cases = [
    ("exact value in its quote", T, "INV-2026-0187", "Invoice No: INV-2026-0187", "found"),
    ("number written differently (19470 vs 19,470.00)", T, "19470.00", "Total amount payable: Rs. 19,470.00", "found"),
    ("quote not in the document", T, "INV-9999", "Invoice No: INV-9999", "unverified"),
    ("value not inside its quote (subtotal quoted for the total)", T, "19,470.00", "Subtotal: 16,500.00", "unverified"),
    ("dropdown option stated in the quote", C, "NEFT", "Payment by NEFT to:", "found"),
    ("checkbox with evidence", X, True, f"GSTIN: {truth['fills'][1]['expected']['payee_gstin']}", "found"),
    ("nothing stated: null stays empty", T, None, None, "not_found"),
]
for label, f, value, quote, want in cases:
    got = extract.ground(f, value, quote, pages)["status"]
    check(f"grounding: {label}", got == want, f"-> {got}")

check("OCR failure is spotted (too few words / mostly symbols)",
      extract.ocr_failed("~~ |# ;; ]]") and extract.ocr_failed("Total 12") and not extract.ocr_failed(pages[0]["text"]))
scan_pages = extract.document_text(upload("invoice_scanned.pdf"))
check("the scanned invoice reads fine by OCR, so no vision fallback", scan_pages[0]["source"] == "OCR",
      f"{len(scan_pages[0]['text'].split())} OCR words")
t = extract.tables(inv, pages)
check("digital table read exactly", t and [t[0]["header"], *t[0]["rows"]] == truth["extraction"]["table"],
      f"{len(t[0]['rows'])} rows" if t else "no table")

log("form filling: proposal, edits, fill, read back (values given, no model)")
from core import proposals  # noqa: E402
import features.fr_ai_02_forms.tools  # noqa: E402,F401  (registers the form_fill applier)
from features.fr_ai_02_forms import fill  # noqa: E402


def read_back(fid):
    _, path = store.get_file(fid)
    with pymupdf.open(path) as d:
        return {w.field_name: w.field_value for pg in d for w in pg.widgets()}


def refused(fn, expect):
    try:
        fn()
        return False, "not refused"
    except (ValueError, ToolError) as e:
        return expect in str(e), str(e)[:100]


for spec, edits in [(truth["fills"][0], {"sum_insured": "5,00,000"}), (truth["fills"][1], {})]:
    form = upload(spec["form"])
    _, d = __import__("core.pdfutil", fromlist=["open_pdf"]).open_pdf(form)
    with d:
        fields = forms.form_fields(d)
    given = dict(spec["expected"], sum_insured="Rs. 5,00,000") if "sum_insured" in spec["expected"] else spec["expected"]
    values = {k: {"value": v, "status": "found", "page": 1} for k, v in given.items()}
    p = fill.build_proposal(form, upload(spec["source"]), fields, values)
    status = {i["id"]: i["status"] for i in p["items"]}
    check(f"{spec['form']}: user-only fields cannot be approved, empty ones are marked empty",
          all(status[f] == "user only" for f in spec["never_fill"])
          and all(status[f] == "not_found" for f in spec["must_stay_empty"]))
    ok, why = refused(lambda: proposals.approve(p["id"], spec["never_fill"]), "not in this proposal")
    check(f"{spec['form']}: approving a signature/declaration field is refused", ok, why)
    ids = [i for i in given if status.get(i) == "found"]
    res = proposals.approve(p["id"], ids, edits)
    got = read_back(res["file"]["id"])
    want = {**given, **edits}
    wrong = {k: (got[k], v) for k, v in want.items()
             if not ((got[k] not in (False, "Off", "", None)) if v is True else str(got[k]) == str(v))}
    check(f"{spec['form']}: every field holds the approved value (edits applied), read back",
          not wrong, f"{res['filled']} filled" + (f"; wrong {wrong}" if wrong else ""))
    untouched = [f for f in spec["must_stay_empty"] + spec["never_fill"] if got[f] not in (False, "Off", "", None)]
    check(f"{spec['form']}: empty and user-only fields left blank", not untouched, f"filled: {untouched}" if untouched else "")
    _, orig = store.get_file(form)
    with pymupdf.open(orig) as d:
        check(f"{spec['form']}: the blank form is untouched",
              all(w.field_value in (False, "Off", "", None) for pg in d for w in pg.widgets()))

p = fill.build_proposal(form, inv, fields, {"payment_mode": {"value": "NEFT", "status": "found", "page": 1}})
ok, why = refused(lambda: proposals.approve(p["id"], ["payment_mode"], {"payment_mode": "Bitcoin"}), "not one of the options")
check("a dropdown value that is not an option is refused", ok, why)
res = proposals.approve(p["id"], ["payment_mode"])
ok, why = refused(lambda: proposals.approve(p["id"], ["payment_mode"]), "already applied")
check("approving twice is refused", ok, why)
ok, why = refused(lambda: fill.prepare_form_fill(upload("application_form.pdf"), upload("policy_schedule.pdf")),
                  "swapped")
check("source and form passed the wrong way round: told they look swapped", ok, why)

log("extraction: confidence, kind, box, Not found, Conflict, templates (fake model)")
from core import llm, rules  # noqa: E402
from features.fr_ai_02_forms import evidence, templates  # noqa: E402

SCRIPT, CALLS, FAIL_ON_CALL = {}, [], set()


def fake_chat(messages, timeout=600, **options):
    schema = options["response_format"]["json_schema"]["schema"]
    CALLS.append(messages[0]["content"])
    if len(CALLS) in FAIL_ON_CALL:
        raise ConnectionResetError("connection reset by peer")
    answer = {n: {"candidates": SCRIPT(n, messages[0]["content"]) if callable(SCRIPT) else SCRIPT.get(n, [])}
              for n in schema["properties"]}
    resp = {"choices": [{"message": {"content": json.dumps(answer)}}], "usage": {"prompt_tokens": 40, "completion_tokens": 8}}
    llm.record_usage(resp)       # what the real llm.chat does after every call
    return resp


llm.chat = fake_chat
inv_text = "\n".join(p["text"] for p in extract.document_text(inv))
line = lambda needle: next(x for x in inv_text.splitlines() if needle in x)
C = lambda value, quote: {"value": value, "quote": quote}

SCRIPT, CALLS[:] = {
    "invoice_number": [C("INV-2026-0187", line("INV-2026-0187"))],
    "invoice_date": [C("05/10/2026", line("05/10/2026"))],
    "total_amount": [C("19,470.00", line("19,470.00"))],
    "due_date": [C("Due date", line("Due date"))],
    "buyer_pan": [], "supplier_name": [C("Imaginary Traders Ltd", "Supplier: Imaginary Traders Ltd")],
}, []
r = extract.extract_information(inv, ["invoice number", "invoice date", "total amount", "due date", "buyer pan", "supplier name"])
v = {x["field"]: x for x in r["values"]}
n = v["invoice number"]
check("a value on a text page: found, confidence 97, kind id, valid, with a box on page 1",
      n["status"] == "found" and n["confidence"] == 97 and not n["low_confidence"] and n["value_type"] == "id"
      and n["format_valid"] and n["box"]["page"] == 1 and n["box"]["x1"] > n["box"]["x0"], str(n["box"]))
check("the box really holds the value on the page",
      pymupdf.open(store.get_file(inv)[1])[0].get_textbox(pymupdf.Rect(n["box"]["x0"] - 1, n["box"]["y0"] - 1,
                                                                        n["box"]["x1"] + 1, n["box"]["y1"] + 1)).strip().endswith("INV-2026-0187"))
check("a date and an amount get their kind and a valid format",
      (v["invoice date"]["value_type"], v["invoice date"]["format_valid"], v["total amount"]["value_type"],
       v["total amount"]["format_valid"]) == ("date", True, "amount", True))
check("a missing field is 'Not found' with confidence 0 and Low confidence",
      v["buyer pan"]["value"] == "Not found" and v["buyer pan"]["confidence"] == 0 and v["buyer pan"]["status"] == "not_found"
      and v["buyer pan"]["low_confidence"])
check("a value whose quote is not in the document is unverified, 40 or less, Low confidence",
      v["supplier name"]["status"] == "unverified" and v["supplier name"]["confidence"] <= 40 and v["supplier name"]["low_confidence"])
check("a value with the wrong format for its kind is capped at 60 with the reason",
      v["due date"]["format_valid"] is False and v["due date"]["confidence"] <= 60 and "date" in v["due date"]["reason"])

SCRIPT = {"invoice_number": [C("INV-2026-0187", line("INV-2026-0187")), C("27ABCPE1234F1ZB", line("GSTIN: 27ABCPE1234F1ZB"))]}
r = extract.extract_information(inv, ["invoice number"])
c = r["values"][0]
check("two different values for one field are a Conflict, both returned, best first",
      c["status"] == "conflict" and len(c["values"]) == 2 and c["values"][0]["confidence"] >= c["values"][1]["confidence"]
      and "also found" in c["why"], str(c.get("why")))

print()
log("kinds and formats")
for label, value, expect in [("PAN", "ABCPE1234F", ("id", True)), ("PAN", "ABCPE12345", ("id", False)),
                             ("Date of birth", "14 June 1990", ("date", True)), ("Date of birth", "31/02/1990", ("date", False)),
                             ("Loan amount", "Rs. 5,00,000", ("amount", True)), ("Total amount", "five lakh", ("amount", False)),
                             ("Applicant name", "Arjun Testcase", ("name", True)), ("Applicant name", "A1jun", ("name", False)),
                             ("IFSC code", "TEST0001234", ("id", True)), ("Policy number", "POL/2026/004512", ("id", True)),
                             ("Mobile number", "9876543210", ("text", None))]:
    k = evidence.value_kind(label, value)
    check(f"{label}: {value} -> {expect[0]}, valid {expect[1]}", k[:2] == expect, str(k))

log("every page is read, one failed group keeps the rest")
filler = lambda i: f"Section {i} ordinary text about routine matters. " * 140
big = pymupdf.open()
for i in range(1, 7):
    pg = big.new_page()
    pg.insert_textbox(pymupdf.Rect(40, 40, 555, 800), filler(i) + (" Vendor code: V-889 is assigned." if i == 6 else ""), fontsize=6)
bid = store.save_file(big.tobytes(), "big.pdf", 6, source="test")["id"]
SCRIPT = lambda name, text: [C("V-889", "Vendor code: V-889 is assigned")] if "Vendor code: V-889" in text else []
CALLS[:] = []
r = extract.extract_information(bid, ["vendor code"])
check("a document longer than one model call is read in groups and the last page's value is found",
      r["values"][0]["status"] == "found" and r["values"][0]["page"] == 6 and len(CALLS) > 1 and not r["partial"],
      f"{len(CALLS)} groups, {r['pages_read']} pages")
check("progress lists every group of pages as done", all(x["status"] == "done" for x in r["progress"]) and r["progress"][-1]["pages"].endswith("6"))
CALLS[:], FAIL_ON_CALL = [], {2}
r = extract.extract_information(bid, ["vendor code"])
check("a group that fails is reported and the result is partial, not lost",
      r["partial"] and any(x["status"] == "failed" for x in r["progress"]) and "partial" in " ".join(r["warnings"]))
FAIL_ON_CALL = set(range(1, 20))
try:
    extract.extract_information(bid, ["vendor code"])
    check("all groups failing is an error", False)
except ToolError as e:
    check("all groups failing is an error", "could not read" in str(e))
FAIL_ON_CALL = set()

log("templates")
SCRIPT = lambda name, text: []
r = extract.extract_information(inv, template="KYC Form")
check("the KYC template asks for its fields and each is 'Not found' on an invoice",
      len(r["values"]) == len(templates.fields_of("KYC Form")) and all(x["value"] == "Not found" for x in r["values"]))
check("a wrong template is warned: 'This does not look like a KYC document.'", "This does not look like a KYC document." in r["warnings"])
r = extract.extract_information(inv, template="Vendor Voucher")
check("a fitting template gives no wrong-template warning", not any("does not look like" in w for w in r["warnings"]))
for bad in ({"template": "Passport"}, {}):
    try:
        extract.extract_information(inv, **bad)
        check(f"refused: {bad or 'no fields and no template'}", False)
    except ToolError:
        check(f"refused: {bad or 'no fields and no template'}", True)

log("scans: OCR confidence, box, blur")
scan_id = upload("invoice_scanned.pdf")
sp = pymupdf.open(store.get_file(scan_id)[1])[0]
words = evidence.ocr_words(sp)
q = evidence.scan_quality(words, sp)
check("a clear scan: OCR confidence is high and there is no quality problem", q["mean_confidence"] >= 85 and q["reason"] is None, str(q))
box, conf = evidence.locate_in_scan(words, sp, 1, "INV-2026-0187")
check("a value on a scan gets a box and its own OCR confidence", box and conf and box["x1"] > box["x0"] and conf > 50, f"{box} {conf}")
from PIL import Image, ImageFilter  # noqa: E402
pix = sp.get_pixmap(dpi=200)
blur = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).filter(ImageFilter.GaussianBlur(3))
blur.save(OUT / "blur.png")
bd = pymupdf.open()
bp = bd.new_page(width=sp.rect.width, height=sp.rect.height)
bp.insert_image(bp.rect, filename=str(OUT / "blur.png"))
bq = evidence.scan_quality(evidence.ocr_words(bp), bp)
check("a blurred scan is reported with the reason 'page is blurred'", bq["reason"] == "page is blurred", str(bq))

log("usage counts the tool's own model calls")
from core import agent  # noqa: E402
rules.set_run_log(OUT / "runs.jsonl")
SCRIPT = lambda name, text: [C("INV-2026-0187", line("INV-2026-0187"))] if name == "invoice_number" else []
queue = [{"choices": [{"message": {"content": "", "tool_calls": [{"id": "c1", "function": {"name": "extract_information",
          "arguments": json.dumps({"file_id": inv, "fields": ["invoice number"]})}}]}}], "usage": {"prompt_tokens": 100, "completion_tokens": 5}},
         {"choices": [{"message": {"content": "Invoice number INV-2026-0187."}}], "usage": {"prompt_tokens": 120, "completion_tokens": 9}}]
agent._call_model = lambda messages: queue.pop(0)
sess = store.get_session(store.new_session("tester")); store.attach(sess, inv)
done = list(agent.run_turn(sess, "What is the invoice number?"))[-1]
check("2 chat calls + 1 extraction call = 3 model calls, tokens include all three",
      done["usage"]["model_calls"] == 3 and done["usage"]["tokens"] == 100 + 5 + 120 + 9 + 48, str(done["usage"]))

log(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
sys.exit(1 if failures else 0)
