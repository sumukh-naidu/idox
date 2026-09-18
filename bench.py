"""
bench.py -- measure where extraction time actually goes, so changes can be
compared instead of guessed at.

Run it once before a change and once after; it appends to bench_results.json
and prints a comparison against the previous run.

    .venv/bin/python bench.py                      # default page/dpi
    .venv/bin/python bench.py --label "igpu on"    # tag the run
    .venv/bin/python bench.py --dpi 150

WHAT IT REPORTS, AND WHY EACH MATTERS

  processor       Where Ollama actually put the model. "100% CPU" means the
                  iGPU is not being used, whatever the config says. This is the
                  first thing to check after enabling OLLAMA_IGPU_ENABLE.

  prompt eval     Reading the image + system prompt + schema. Compute-bound,
                  so this is the phase a GPU should improve most. It was 53%
                  of total time on CPU.

  generation      Writing the JSON out, one token at a time. Memory-bandwidth
                  bound. An integrated GPU shares system RAM with the CPU, so
                  this phase may barely improve even when prefill does.

  blocks/rows     A speed win that loses content is not a win. If block count
                  drops, the setting made things worse, not faster.
"""

import argparse
import json
import os
import subprocess
import time

import ollama
import pymupdf

from blocks import MODEL_NAME, SYSTEM_PROMPT, USER_PROMPT, Page, TableBlock

RESULTS = "bench_results.json"

parser = argparse.ArgumentParser()
parser.add_argument("--pdf", default="real_business_doc.pdf")
parser.add_argument("--page", type=int, default=1)
parser.add_argument("--dpi", type=int, default=125)
parser.add_argument("--label", default="", help="tag for this run, e.g. 'igpu on'")
args = parser.parse_args()

img = f"/tmp/bench_p{args.page}_{args.dpi}.png"
pdf_page = pymupdf.open(args.pdf)[args.page - 1]
pix = pdf_page.get_pixmap(dpi=args.dpi)
pix.save(img)

print(f"model  : {MODEL_NAME}")
print(f"page   : {args.pdf} p{args.page} at {args.dpi} dpi "
      f"({pix.width}x{pix.height})")
print("running...", flush=True)

started = time.time()
resp = ollama.chat(
    model=MODEL_NAME,
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT, "images": [img]},
    ],
    format=Page.model_json_schema(),
    options={"temperature": 0, "num_ctx": 8192},
)
wall = time.time() - started

# Where did Ollama actually run it? Config intent is not evidence.
try:
    ps = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10)
    # The PROCESSOR column is found by locating the field containing '%'
    # rather than by a fixed index: SIZE is two fields ("4.3 GB"), and a
    # split load reads as one field ("51%/49% CPU/GPU"), so positions move.
    processor = "unknown"
    for line in ps.stdout.splitlines():
        if MODEL_NAME.split(":")[0] not in line:
            continue
        fields = line.split()
        for i, f in enumerate(fields):
            if "%" in f and i + 1 < len(fields):
                processor = f"{f} {fields[i + 1]}"
                break
        break
except Exception:
    processor = "unknown"

page = Page.model_validate_json(resp["message"]["content"])
tables = [b for b in page.blocks if isinstance(b, TableBlock)]

prompt_tokens = resp.get("prompt_eval_count") or 0
out_tokens = resp.get("eval_count") or 0
prompt_s = (resp.get("prompt_eval_duration") or 0) / 1e9
gen_s = (resp.get("eval_duration") or 0) / 1e9
load_s = (resp.get("load_duration") or 0) / 1e9

result = {
    "label": args.label or "unlabelled",
    "when": time.strftime("%Y-%m-%d %H:%M"),
    "dpi": args.dpi,
    "page": args.page,
    "processor": processor,
    "wall_s": round(wall, 1),
    "load_s": round(load_s, 1),
    "prompt_tokens": prompt_tokens,
    "prompt_s": round(prompt_s, 1),
    "prompt_tps": round(prompt_tokens / prompt_s, 1) if prompt_s else 0,
    "out_tokens": out_tokens,
    "gen_s": round(gen_s, 1),
    "gen_tps": round(out_tokens / gen_s, 1) if gen_s else 0,
    "blocks": len(page.blocks),
    "tables": len(tables),
    "table_rows": sum(len(t.rows) for t in tables),
}

print()
print("=" * 62)
print(f"  processor      {result['processor']}")
print(f"  wall clock     {result['wall_s']}s   (model load {result['load_s']}s)")
print(f"  prompt eval    {result['prompt_s']}s for {prompt_tokens} tokens "
      f"= {result['prompt_tps']} tok/s")
print(f"  generation     {result['gen_s']}s for {out_tokens} tokens "
      f"= {result['gen_tps']} tok/s")
print(f"  extracted      {result['blocks']} blocks, {result['tables']} table(s), "
      f"{result['table_rows']} rows")
print("=" * 62)

history = []
if os.path.exists(RESULTS):
    with open(RESULTS) as fh:
        history = json.load(fh)

if history:
    prev = history[-1]
    print(f"\nvs previous run ({prev['label']}, {prev['when']}, "
          f"{prev['processor']}):")

    def delta(name, now, before, unit="s", lower_better=True):
        if not before:
            return
        pct = (now - before) / before * 100
        arrow = "faster" if (pct < 0) == lower_better else "slower"
        if not lower_better:
            arrow = "more" if pct > 0 else "less"
        print(f"  {name:<14} {before}{unit} -> {now}{unit}  "
              f"({pct:+.0f}%, {arrow})")

    delta("wall clock", result["wall_s"], prev["wall_s"])
    delta("prompt eval", result["prompt_s"], prev["prompt_s"])
    delta("generation", result["gen_s"], prev["gen_s"])

    if result["blocks"] != prev["blocks"]:
        print(f"  !! blocks changed {prev['blocks']} -> {result['blocks']} "
              f"-- a speed win that loses content is not a win")

history.append(result)
with open(RESULTS, "w") as fh:
    json.dump(history, fh, indent=2)
print(f"\nsaved to {RESULTS} ({len(history)} runs recorded)")
