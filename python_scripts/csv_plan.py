"""
csv_plan.py -- ask the model how to FORMAT a CSV into a typed table, then apply the plan.

The model is given the file's first and last rows and answers with a JSON plan:
  has_header, title, columns[{name, kind}]
Nothing here invents or changes a value. The plan only decides how the table is
presented; every cell is copied verbatim.

Usage:
    .venv/Scripts/python python_scripts/csv_plan.py input/csv_plan_input/sample_report.csv
    .venv/Scripts/python python_scripts/csv_plan.py input/csv_plan_input/price_list.csv --apply

--apply writes a formatted copy next to the input (sample_report.formatted.csv).
Without it, the plan is printed so it can be reviewed. Prose above the table (a title,
intro, bullets) is preserved; the plan only re-formats the tabular region.
"""
import csv
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from blocks import LOCAL_BASE_URL  # noqa: E402

SYSTEM_PROMPT = """You plan how ONE CSV file is turned into a formatted table. A program copies every
value itself; you never write or change values. You read the file's first and last rows and
answer with a JSON plan.

RULES
1. has_header: true if the first row holds column names (words like Name, Date, Amount),
   false if it is already data. Decide by whether the row's cells read as labels, not by
   whether they look like data -- a row of region names IS a header when the column is
   called Region.
2. title: a short title (at most 8 words) built from the file name and the column names.
   It is the MAIN HEADING of the document, and the main heading is always placed in the
   centre, across the full width of the table.
3. columns: one entry per column, in order. "name" is that column's own label -- the
   text in the header row for that column (column 1's name is the first cell of the
   header row). Never take a name from a data cell. "kind" is:
   - number    measurements, counts, quantities
   - currency  money amounts (with or without a currency sign)
   - percent   values written with %
   - date      dates or times
   - id        codes, phone numbers, postal codes, account numbers, anything with leading
               zeros or that is never added up -- these must stay exactly as written
   - text      everything else
   Decide kind from the column's VALUES, not from its name: a column called "Region" is
   text, a column called "Units" is number, a column called "Revenue" is currency.
4. Never invent columns: exactly as many entries as the file has columns.

Reply with ONLY a JSON object, no prose:
{"has_header": true, "title": "...", "columns": [{"name": "...", "kind": "..."}]}"""

# A sample of rows is enough: the model needs the first row (header or not) and the
# last row (to see whether values look like data). Middle rows would add cost for no
# extra information about column kinds.
MAX_SAMPLE_ROWS = 10


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.reader(fh))


def find_table(rows):
    """Locate the tabular region of a CSV that may carry prose before its table.

    The widest row is the table's header (these files put a title, an intro and
    bullet lines above the actual grid). Returns (table_start, header, data_rows).
    """
    if not rows:
        return 0, [], []
    width = max(len(r) for r in rows)
    wide = [i for i, r in enumerate(rows) if len(r) == width]
    if not wide:
        return 0, rows[0], rows[1:]
    start = wide[0]
    return start, rows[start], rows[start + 1:]


def build_prompt(rows, filename):
    start, header, data = find_table(rows)
    n_cols = len(header)
    # Enough rows to see whether a column's values repeat a pattern (a region name,
    # a currency sign, a leading zero). Too few and a column looks like one-off text.
    # The header row is always included first: the column names come from it.
    n_show = min(MAX_SAMPLE_ROWS, max(1, len(data)))
    sample = [header] + data[:n_show]
    body = "\n".join(",".join(r) for r in sample)
    return (
        f"File: {filename}  (table starts at row {start + 1}, "
        f"{len(data)} data rows x {n_cols} columns)\n\n"
        f"{body}\n\n"
        f"Reply with the JSON plan only."
    )


def ask_model(text):
    import requests
    resp = requests.post(
        LOCAL_BASE_URL + "/v1/chat/completions",
        json={
            "model": "manual",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0,
            "max_tokens": 512,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def parse_plan(raw, n_cols):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    plan = json.loads(raw)
    cols = plan.get("columns") or []
    if len(cols) != n_cols:
        raise ValueError(f"plan has {len(cols)} columns, file has {n_cols}")
    for c in cols:
        if c.get("kind") not in ("number", "currency", "percent", "date", "id", "text"):
            raise ValueError(f"unknown column kind {c.get('kind')!r}")
    return plan


def apply_plan(header, data, plan, with_title=True):
    """Return formatted rows: title row, blank, header (if any), data rows.

    The title sits on its own with a blank line either side, so it is spaced out
    from the prose above it and from the table below. Every cell is copied as it
    was: when the plan says the first row is NOT a header, that row is data and
    stays in the output. The column kinds in the plan describe the columns (they
    are printed for review); they never change a value.
    """
    out = []
    title = (plan.get("title") or "").strip() if with_title else ""
    if title:
        out.append([])
        out.append([title])
        out.append([])
    if plan.get("has_header"):
        out.append(header)
        out.extend(data)
    else:
        out.extend([header] + list(data))      # the first row is data: never drop it
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="CSV file to plan")
    ap.add_argument("--apply", action="store_true", help="write a formatted copy")
    ap.add_argument("--no-title", action="store_true", help="do not add the model's title row")
    ap.add_argument("--out", help="output path (default: <name>.formatted.csv)")
    args = ap.parse_args()

    rows = read_csv(args.csv)
    if not rows:
        print("empty file")
        sys.exit(1)

    start, header, data = find_table(rows)
    prompt = build_prompt(rows, os.path.basename(args.csv))
    raw = ask_model(prompt)
    plan = parse_plan(raw, len(header))
    print(json.dumps(plan, indent=2))

    if args.apply:
        out_path = args.out or str(Path(args.csv).with_suffix(".formatted.csv"))
        formatted = list(rows[:start]) + apply_plan(header, data, plan, with_title=not args.no_title)
        with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerows(formatted)
        print(f"\nwrote {out_path}  ({len(formatted)} rows)")


if __name__ == "__main__":
    main()