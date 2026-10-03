"""Check the FR-AI-02 tools against input/ground_truth.json, without the model.

    .venv/bin/python features/fr_ai_02_forms/check_tools.py

Files the tools write, and report.txt, go to output/check_tools/ (cleared each run).
"""

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

import pymupdf  # noqa: E402

from core import store  # noqa: E402
from core.pdfutil import ToolError  # noqa: E402
from features.fr_ai_02_forms import forms  # noqa: E402

INPUT = HERE / "input"
OUT = HERE / "output" / "check_tools"
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
    ("nothing stated: null stays empty", T, None, None, "empty"),
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
          and all(status[f] == "empty" for f in spec["must_stay_empty"]))
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

log(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
sys.exit(1 if failures else 0)
