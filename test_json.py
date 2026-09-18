"""
test_json.py -- the single-image smoke test.

test.py proved the model can READ a page (correct values, in prose). Prose is
useless to openpyxl. This proves it can hold that accuracy while constrained to
a strict, parseable structure.

The schema itself now lives in blocks.py, shared with test_pdf.py so the two
cannot drift apart. Read the comment block at the top of blocks.py for why the
schema is shaped the way it is -- it is the most useful thing learned so far.

Run:  .venv/bin/python test_json.py
"""

import time

from blocks import TableBlock, check_structure, extract_page

IMAGE_PATH = "test_page.png"


started = time.time()
page = extract_page(IMAGE_PATH)
elapsed = time.time() - started

print("=" * 70)
print(f"PARSED OK -- {len(page.blocks)} blocks in {elapsed:.1f}s")
print("=" * 70)

for i, block in enumerate(page.blocks):
    if isinstance(block, TableBlock):
        print(
            f"[{i}] {block.kind}  {block.n_data_rows} data rows "
            f"x {block.n_cols} cols"
        )
        print("      H | " + " | ".join(block.header))
        for row in block.rows:
            print("        | " + " | ".join(row))
    else:
        preview = " ".join(block.text.split())
        if len(preview) > 90:
            preview = preview[:90] + "..."
        print(f"[{i}] {block.kind}  {preview}")

print()
print("=" * 70)
print("STRUCTURAL CONFORMANCE")
print("=" * 70)

problems, notes = check_structure(page)
for n in notes:
    print(f"  ~ {n}")
if problems:
    print("ESCALATE to Tier 2 -- structural check failed:")
    for p in problems:
        print(f"  - {p}")
else:
    print("PASS -- shape is internally consistent, Tier 1 output accepted.")
