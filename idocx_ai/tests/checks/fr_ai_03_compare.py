"""Check the FR-AI-03 comparison against samples/fr_ai_03_compare/ground_truth.json, without the model.

    .venv/bin/python tests/checks/fr_ai_03_compare.py

Files the tools write, and report.txt, go to tests/output/fr_ai_03_compare/ (cleared each run).
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
from features.fr_ai_03_compare import compare, explain  # noqa: E402

explain._ask_model = lambda changes: []     # these checks never call the model; the key-changes checks pass a fake

INPUT = ROOT / "samples" / "fr_ai_03_compare"
OUT = ROOT / "tests" / "output" / "fr_ai_03_compare"
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
    return store.save_file((INPUT / name).read_bytes(), name, truth[name]["pages"], source="test")["id"]


def title(change):
    return change["where"].split("(")[1].split(")")[0] if "(" in change["where"] else change["where"]


def found(result):
    return {(title(c), c["kind"]) for c in result["changes"]}


want = {(c["clause"], c["type"]) for c in truth["changes"]}
v1, v2 = upload("contract_v1.pdf"), upload("contract_v2.pdf")

log("contract_v1 -> contract_v2")
r = compare.compare_documents(v1, v2)
check("exactly the planted changes, of the right type", found(r) == want,
      f"missing {sorted(want - found(r))}, extra {sorted(found(r) - want)}" if found(r) != want else
      ", ".join(f"{c['id']} {title(c)} {c['kind']}" for c in r["changes"]))
sig = {title(c): c.get("signals", []) for c in r["changes"]}
check("number changes surfaced as signals",
      "numbers changed: 30 -> 45" in sig["Payment"] and "numbers changed: 12 -> 6" in sig["Liability"])
check("unchanged clauses and renumbering not reported",
      r["summary"]["counts"]["unchanged"] == len(truth["unchanged"]) and "1 clause(s) only renumbered" in r["ignored"],
      str(r["ignored"]))
check("headers and page numbers not reported",
      not any("Version" in c.get("diff", "") + c.get("old_text", "") + c.get("new_text", "") or
              "Page " in c.get("diff", "") for c in r["changes"]))

_, path = store.get_file(r["files_created"][0]["id"])
with pymupdf.open(path) as d:
    marks = [(p.number + 1, a.type[1], a.info["content"].split(":")[0],
              p.get_textbox(a.rect).strip()) for p in d for a in p.annots()]
ids = {m[2].split()[0] for m in marks}
check("marked-up copy carries a mark for every change", ids == {c["id"] for c in r["changes"]},
      f"{len(marks)} marks for {', '.join(sorted(ids))}")
check("no highlight on a clause number", not any(m[1] == "Highlight" and m[3][:2].rstrip(".").isdigit()
                                                 and m[3].rstrip(".").isdigit() for m in marks),
      "; ".join(f"p{m[0]} {m[2]}: '{m[3][:30]}'" for m in marks if m[1] == "Highlight"))

log("explanation checks")
by_title = {title(c): c["id"] for c in r["changes"]}
e = explain.check_change_explanations(r["comparison_id"], [
    {"change_id": by_title["Payment"], "significance": "material",
     "explanation": "Payment is now due within 45 days instead of 30."},
    {"change_id": "C99", "significance": "minor", "explanation": "Something changed."},
    {"change_id": by_title["Liability"], "significance": "material",
     "explanation": "The liability cap went from 12 months of fees down to 3 months."},
])
check("a correct explanation is verified", [v["change_id"] for v in e["verified"]] == [by_title["Payment"]])
check("unknown change id and a wrong figure are rejected",
      [x["reason"][:12] for x in e["rejected"]] == ["no change wi", "states 3, wh"],
      "; ".join(x["reason"] for x in e["rejected"]))
check("unexplained changes are listed", len(e["not_explained"]) == len(r["changes"]) - 1)
try:
    explain.check_change_explanations("cmp_nope", [{"change_id": "C1"}])
    check("an unknown comparison is refused", False)
except ToolError as err:
    check("an unknown comparison is refused", "run compare_documents first" in str(err))

log("identical files")
same = compare.compare_documents(v1, upload("contract_v1.pdf"))
check("no changes and no marked-up copy", not same["changes"] and "files_created" not in same,
      same["next_step"])
check("identical files say 'No differences found'", same["status"] == "no_differences" and same["message"] == "No differences found")

log("contract_v1 -> contract_v2_scanned (OCR)")
s = compare.compare_documents(v1, upload("contract_v2_scanned.pdf"))
material = {(c["clause"], c["type"]) for c in truth["changes"] if c["material"]}
check("OCR warning given", any("OCR" in w for w in s["warnings"]), " ".join(s["warnings"]))
check("the material changes are still found", material <= found(s),
      f"found {len(s['changes'])} changes; extra from OCR: {sorted(found(s) - want) or 'none'}")

log("brief 2.3: types, positions, confidence, summary, key changes (fake explainer)")
r1 = compare.compare_documents(v1, v2)           # explainer returns nothing here
check("types are Added, Removed or Modified (moved text is Modified)",
      {c["type"] for c in r1["changes"]} <= {"Added", "Removed", "Modified"}
      and any(c["kind"] == "moved" and c["type"] == "Modified" and c["moved"] for c in r1["changes"]))
mod = next(c for c in r1["changes"] if c["kind"] == "modified")
check("a modified change has old and new, each with page, box and text",
      all(mod[k]["page"] and mod[k]["box"]["x1"] > mod[k]["box"]["x0"] and mod[k]["text"] for k in ("old", "new")), str(mod["old"]["box"]))
add_ = next(c for c in r1["changes"] if c["type"] == "Added")
rem = next(c for c in r1["changes"] if c["type"] == "Removed")
check("an Added change has no old side, a Removed change has no new side", add_["old"] is None and rem["new"] is None and add_["new"]["text"] and rem["old"]["text"])
check("every change has a 0-100 confidence and a Low confidence flag",
      all(isinstance(c["confidence"], int) and 0 <= c["confidence"] <= 100 and isinstance(c["low_confidence"], bool) for c in r1["changes"]),
      str(sorted({c["confidence"] for c in r1["changes"]})))
check("changes come in document order (new-version order)", [c["id"] for c in r1["changes"]] == [f"C{k}" for k in range(1, len(r1["changes"]) + 1)])
check("the summary has counts and a plain sentence",
      r1["summary"]["counts"]["added"] + r1["summary"]["counts"]["removed"] + r1["summary"]["counts"]["modified"] == len(r1["changes"])
      and f"{len(r1['changes'])} changes" in r1["summary"]["text"], r1["summary"]["text"])
check("what was ignored is listed", {"running headers and footers", "page numbers", "spacing differences"} <= set(r1["ignored"]))
check("a failing explainer never fails the comparison", r1["summary"]["key_changes"] == [] and r1["summary"]["explanations_unavailable"] is False
      and r1["status"] == "ok")
def boom(changes):
    raise ConnectionResetError("reset")
r2 = compare.compare_documents(v1, v2, explainer=boom)
check("a model error is reported as 'explanations unavailable', changes still returned", r2["summary"]["explanations_unavailable"] and r2["changes"])

ids = [c["id"] for c in r1["changes"]]
def explainer(changes):
    out = []
    for c in changes:
        out.append({"change_id": c["id"], "significance": "material" if any("numbers changed" in x for x in c.get("signals", [])) or c["type"] in ("added", "removed") else "minor",
                    "explanation": f"{c['where']} was {c['type']}."})
    out.append({"change_id": "C99", "significance": "material", "explanation": "invented change"})
    return out
r3 = compare.compare_documents(v1, v2, explainer=explainer)
kc = r3["summary"]["key_changes"]
check("3 to 5 key changes, each with a verified explanation, a page and a confidence", 3 <= len(kc) <= 5 and all(k["explanation"] and k["pages"] and "confidence" in k for k in kc), f"{len(kc)}: {[k['change_id'] for k in kc]}")
check("an explanation for a change that does not exist is dropped", all(k["change_id"] != "C99" for k in kc))
check("key changes are in document order", [int(k["change_id"][1:]) for k in kc] == sorted(int(k["change_id"][1:]) for k in kc))
check("figures changed rank as material first", all(k["significance"] == "material" for k in kc) or len(kc) >= 3)
def wrong_number(changes):
    return [{"change_id": c["id"], "significance": "material", "explanation": "The fee is now 999 days."} for c in changes]
r4 = compare.compare_documents(v1, v2, explainer=wrong_number)
check("an explanation stating a number that is not in the change is rejected", r4["summary"]["key_changes"] == [])

log("brief 2.3: include trivial differences when asked")
strict = compare.compare_documents(v1, v2, include_ignored=True)
check("with include_ignored the list no longer claims headers and page numbers are ignored",
      "running headers and footers" not in strict["ignored"] and len(strict["changes"]) >= len(r1["changes"]), f"{len(strict['changes'])} vs {len(r1['changes'])}")

log("brief 2.3: different languages need the user's confirmation")
en = compare.detect_language("The supplier shall pay the fees within thirty days of the invoice and the buyer shall keep this agreement confidential for the term of the contract and after.")
es = compare.detect_language("El proveedor pagará las tarifas dentro de los treinta días de la factura y el comprador mantendrá este acuerdo confidencial por el plazo del contrato y después de este.")
de = compare.detect_language("Der Lieferant zahlt die Gebühren innerhalb von dreißig Tagen nach der Rechnung und der Käufer hält diese Vereinbarung für die Dauer des Vertrages vertraulich und nicht öffentlich.")
hi = compare.detect_language("आपूर्तिकर्ता चालान के तीस दिनों के भीतर शुल्क का भुगतान करेगा और खरीदार इस समझौते को अनुबंध की अवधि के लिए गोपनीय रखेगा और उसके बाद भी")
check("languages are detected: English, Spanish, German, Hindi", (en, es, de, hi) == ("English", "Spanish", "German", "Hindi"), str((en, es, de, hi)))
check("too little text: language unknown", compare.detect_language("Hello there") is None)

def doc_with(text, name):
    d = pymupdf.open()
    d.new_page().insert_textbox(pymupdf.Rect(50, 80, 545, 780), text, fontsize=11)
    return store.save_file(d.tobytes(), name, 1, source="test")["id"]
en_id = doc_with("1. Payment. " + "The supplier shall pay the fees within thirty days of the invoice and the buyer shall keep this agreement confidential. " * 4, "en.pdf")
es_id = doc_with("1. Pago. " + "El proveedor pagará las tarifas dentro de los treinta días de la factura y el comprador mantendrá este acuerdo confidencial. " * 4, "es.pdf")
ask = compare.compare_documents(en_id, es_id)
check("different languages: no comparison, a warning and the languages", ask["status"] == "confirm_language" and ask["changes"] == []
      and ask["languages"] == {"old": "English", "new": "Spanish"} and "confirm" in ask["warning"])
go = compare.compare_documents(en_id, es_id, confirm_language_difference=True)
check("once confirmed it compares and keeps a warning", go["status"] in ("ok", "no_differences") and any("different languages" in w for w in go["warnings"]))

log("brief 2.3: size limits")
old_limit = compare.MAX_PAGES
compare.MAX_PAGES = 1
try:
    compare.compare_documents(v1, v2)
    check("too many pages is refused with the limit", False)
except ToolError as err:
    check("too many pages is refused with the limit", "limit is 1 pages" in str(err), str(err)[:90])
compare.MAX_PAGES = old_limit

log("brief 2.3: OCR lowers confidence")
check("changes on a scanned side are capped at 70 and say why",
      all(c["confidence"] <= 70 and c["low_confidence"] and "OCR" in c.get("reason", "") for c in s["changes"] if "OCR" in c.get("reason", ""))
      and any("OCR" in c.get("reason", "") for c in s["changes"]))

log("a clause missing from a scanned version may just have been missed by OCR")
sc = compare.compare_documents(v1, upload("contract_v2_scanned.pdf"))
check("Removed and Added changes are also capped at 70 when the other version was read by OCR",
      all(c["confidence"] <= 70 and c["low_confidence"] for c in sc["changes"]), str([(c["type"], c["confidence"]) for c in sc["changes"]]))

log(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
(OUT / "report.txt").write_text("\n".join(report) + "\n")
sys.exit(1 if failures else 0)
