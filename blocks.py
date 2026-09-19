"""
blocks.py -- the schema, the checks, and the single model call.

ONE call to the model per page. One system prompt. The model looks at the page
image and returns everything: its reasoning about the layout, the extracted
contents, and a review of its own work.

--------------------------------------------------------------------------
HOW REASONING FITS IN A SINGLE CONSTRAINED CALL

format=<schema> forces the response to be JSON and nothing else -- the model
physically cannot write a "Thought:" line alongside it. The way to get
reasoning anyway is to make the reasoning part of the schema, and to exploit
the fact that JSON is generated FIELD BY FIELD IN SCHEMA ORDER:

    analysis   <- generated FIRST   (ReAct: think before acting)
    blocks     <- generated SECOND  (the extraction, conditioned on the above)
    review     <- generated LAST    (Reflexion: examine what was just written)

Because `analysis` is emitted before a single cell is transcribed, the model
has already worked out how many columns there are and where the awkward rows
sit by the time it starts extracting. That reasoning is in its context, in its
own words, which is what makes ReAct work.

An honest limit on the `review` field: it is generated AFTER the blocks, so it
cannot go back and fix them. Its value is diagnostic -- it tells you where the
model itself thinks it may have slipped. True self-correction needs a second
attempt, which is exactly what the automated checks below are for.

--------------------------------------------------------------------------
WHY THE EXTRACTION SCHEMA LOOKS LIKE THIS

Three failed runs shaped it, and each taught the same lesson:

  CONSTRAINED DECODING ENFORCES SHAPE, NEVER SEMANTICS.

Anything stated in words -- even inside a field description -- is advisory, and
a 4B model will drop it. If a rule matters, express it as a different schema,
not as a better sentence.

  Run 1: fields with defaults fall out of JSON Schema's "required" list, so the
         decoder let the model omit n_rows entirely. -> no defaults, ever.
  Run 2: one Block type with "use [] if not a table" in the description; the
         model copied paragraph text into cells anyway. -> split TextBlock and
         TableBlock, so there is no cells field on a paragraph to misuse.
  Run 3: "rows INCLUDING the header" was ignored and it counted data rows.
         -> split header from rows so the question cannot be asked ambiguously.

Field ORDER matters here too: n_data_rows/n_cols come before rows, so the model
must commit to a count before transcribing. If it sees 5 rows and writes 4, the
mismatch is detectable.
"""

import re
from typing import List, Literal, Union

import ollama
from pydantic import BaseModel, Field

MODEL_NAME = "qwen3-vl:4b-instruct"


# ===========================================================================
# STAGE 1 -- the ReAct "Thought" step, as schema fields
#
# Two fields, both of which the model must commit to BEFORE transcribing
# anything, and both of which the checks below can verify against what it
# actually produced. That verifiability is the test a field has to pass to
# live here: it started as four, and the two that could only be judged by
# reading them were wrong every time (see the note in the class body).
# ===========================================================================

class LayoutAnalysis(BaseModel):
    # A COUNT, not an enumeration -- and that distinction is the whole point.
    #
    # This field was originally block_kinds: List[Literal[...]], one entry per
    # block. It served its purpose (forcing the model to acknowledge every
    # paragraph before extracting, which fixed a page coming back as one table
    # and nothing else) but it carried a fatal flaw: an unbounded array under
    # greedy decoding can never decide to stop. At each position the grammar
    # allows another item or a closing bracket, and if another item stays
    # marginally more likely, the list runs forever. On a bullet-heavy page it
    # emitted "list" until it filled the entire context -- 23 minutes, and the
    # blocks field was never reached at all. Capping the length only changed
    # the failure: it stopped at exactly 40 entries and then extracted nothing.
    #
    # An integer cannot do that. It is one token, it commits the model to a
    # number before it extracts anything, and the check below compares that
    # number against reality -- which is all the enumeration was ever used for.
    n_blocks: int = Field(
        description=(
            "How many blocks are on this page, counting every paragraph of "
            "body text, every heading and every table. A bulleted list is ONE "
            "block, however many bullets it has. You must output exactly this "
            "many blocks below."
        )
    )
    table_column_counts: List[int] = Field(
        max_length=10,
        description=(
            "The column count of each REAL table, in order. A real table has a "
            "repeating grid: several rows sharing the same column positions. "
            "Text that is merely spaced out, or a single strip of label/value "
            "pairs, is NOT a table -- leave it out. Example: [6]. Use [] if "
            "there are no tables."
        )
    )
    # spanning_rows and fragile_text used to live here. Both were removed on
    # the evidence of every run in which they appeared:
    #
    #   spanning_rows  business doc: "header row only" -- wrong, it missed the
    #                  divider entirely, and the extraction handled that row
    #                  correctly anyway. Lorem page and EOD report: ":[{",
    #                  raw JSON punctuation emitted as a string value.
    #   fragile_text   business doc: listed whole paragraphs. Lorem page:
    #                  listed "1","2","3","4". EOD report, which is nothing
    #                  but code identifiers: [].
    #
    # Neither was ever right. Worse, on a page with no tables the model has
    # nothing to say in these table-shaped fields, emits garbage, and then --
    # with that garbage now in its own context -- closes the blocks array
    # empty. The EOD report extracted 0 blocks with these fields present, and
    # 14 correct blocks with the analysis section removed entirely.
    #
    # What survives are the two fields that have been right every time: a
    # count the checks can verify, and the column counts.


# ===========================================================================
# STAGE 2 -- the extraction
# ===========================================================================

class TextBlock(BaseModel):
    kind: Literal["heading", "paragraph", "caption", "list", "image"] = Field(
        description="What structural kind of non-table block this is."
    )
    text: str = Field(
        description=(
            "Full text of the block, exactly as printed. Use REAL line breaks "
            "for lines that are separate on the page, such as the lines of an "
            "address. Never write the two characters backslash-n."
        ),
    )
    # --- how the block LOOKS on the page ----------------------------------
    #
    # On a digital PDF the text layer carries font sizes and positions, so
    # layout can be measured. A scanned page has none of that: the only thing
    # that can see the letterhead is centred, or the date sits to the right,
    # is the model looking at the picture. Without these, a fax letter with a
    # large centred letterhead and a right-aligned date came out as thirteen
    # identical left-aligned paragraphs.
    #
    # These come AFTER text, deliberately. Placed before it, the model wrote
    # three appearance decisions for every block before committing any of its
    # words -- and on the first run that way it stopped after 8 blocks where it
    # had previously produced 13, losing five paragraphs of the letter. Content
    # is secured first; the decoration follows.
    #
    # All three are closed sets or a boolean, so they cost about one token each
    # and cannot run away the way an open list can.
    align: Literal["left", "center", "right"] = Field(
        description=(
            "How this block sits horizontally on the page: 'center' if it is "
            "centred across the page, 'right' if it is pushed to the right "
            "margin, otherwise 'left'."
        )
    )
    size: Literal["large", "normal", "small"] = Field(
        description=(
            "Size of this block's text compared with the page's ordinary body "
            "text. Use 'normal' for ordinary body text -- most blocks on most "
            "pages are normal. Use 'large' ONLY for titles and letterheads "
            "that are visibly bigger than the body, and 'small' ONLY for "
            "footers, footnotes and fine print that are visibly smaller."
        )
    )
    bold: bool = Field(
        description="True if this block is printed in bold or heavy type."
    )


class TableBlock(BaseModel):
    kind: Literal["table"]
    has_header: bool = Field(
        description=(
            "True if the table's first row is a header of column LABELS "
            "(names describing the columns). False if the first row is already "
            "data -- for example a plain grid of numbers with no labels. When "
            "false, leave header empty and put every row in rows."
        ),
    )
    n_data_rows: int = Field(
        description=(
            "How many entries the rows list below will contain. Count EVERY "
            "row you are about to output, including full-width divider rows "
            "and section-heading rows. Exclude only the header row."
        ),
    )
    n_cols: int = Field(
        description="Number of columns.",
    )
    header: List[str] = Field(
        description=(
            "The header row as a list of column labels. Use [] when "
            "has_header is false."
        ),
    )
    rows: List[List[str]] = Field(
        description=(
            "The data rows, each a list of cell strings. Every row must have "
            "exactly n_cols entries -- pad with empty strings \"\" where a cell "
            "is blank or merged. Copy cell text exactly as printed, including "
            "$ , % and + - signs."
        ),
    )


# ===========================================================================
# The page: analysis -> blocks, in that generation order
#
# There is deliberately NO self-review section after the blocks. A review
# generated after the extraction cannot repair it -- the JSON above it is
# already written -- so it costs a quarter of the generation time and fixes
# nothing. The automated checks below do that job properly, by comparing
# against the PDF's own text rather than the model's opinion of itself.
# ===========================================================================

class Page(BaseModel):
    analysis: LayoutAnalysis = Field(
        description="Your reasoning about the page layout, BEFORE extracting."
    )
    blocks: List[Union[TableBlock, TextBlock]] = Field(
        description="Every block on the page, in top-to-bottom reading order."
    )


# ===========================================================================
# THE SINGLE SYSTEM PROMPT
# ===========================================================================

SYSTEM_PROMPT = """You extract the contents of document page images exactly as printed.

You work in two stages, and the response format enforces their order. The
analysis is written before you transcribe anything.

STAGE 1 -- OBSERVE (the "analysis" field)
Before extracting anything, settle the decisions that are easy to get wrong:
which regions are genuinely tables, how many columns each has, which rows break
the grid, and which identifiers must be copied character by character.

Keep this stage SHORT. Lists and numbers, not sentences. It exists so that you
commit to a column count before you start transcribing -- not to explain
yourself.

STAGE 2 -- ACT (the "blocks" field)
Now extract the page, following the analysis you just wrote. Apply these rules
in order of importance:

1. EVERY piece of text on the page becomes a block. Every paragraph of body
   text, every heading, every caption -- not just the interesting parts, and
   not just the tables. A page of five paragraphs and one table is SIX blocks,
   not one. Most pages are mostly prose; extract all of it.

2. A region is a table ONLY if it has a repeating grid of aligned cells across
   several rows. A strip of spaced-out label/value pairs is a PARAGRAPH --
   emit it as a paragraph, never drop it.

3. A row spanning the full width of a table -- a section divider or group
   heading -- is its OWN row. Put its text in the first cell and fill the
   remaining cells with empty strings "". NEVER merge it into the next row:
   that shifts every value in that row into the wrong column.

4. Every data row must contain exactly as many cells as the header. Count
   before you write. Use "" for blank cells rather than omitting them.
   n_data_rows must equal the number of rows you output, divider rows
   included.

5. Copy text character by character. Dots, underscores, slashes and hyphens in
   identifiers are significant: crm.team._action_assign_leads is NOT the same
   as crm.team_action_assign_leads. Never tidy, expand, correct or complete
   anything you see.

6. Transcribe only what is printed. Never summarise, compute or infer.

7. Record how each block LOOKS, not only what it says. Set align to 'center'
   for anything centred across the page and 'right' for anything pushed to the
   right margin. Set size to 'large' for titles and letterheads, 'small' for
   footers and fine print. Set bold when the type is heavy. A letterhead is
   usually large, centred and bold; a date line is often right-aligned; a
   footer is usually small and centred.

8. Where lines are separate on the page -- the lines of an address, a
   signature block -- put REAL line breaks between them inside the block's
   text. Never write the two characters backslash-n."""


USER_PROMPT = "Analyse this page briefly, then extract every block on it."


# ===========================================================================
# CHECKS -- how we know whether the extraction is actually right
# ===========================================================================

# Typographic variants that mean the same character. A PDF's text layer and a
# model's transcription routinely disagree on these: the source here uses a
# straight apostrophe in "Forgenite's" while the model writes a curly one.
# Without folding them, the grounding check reports a hallucination that is
# really just a different quote mark -- a false alarm that hides real ones.
_PUNCT = str.maketrans({
    "‘": "'", "’": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
    "…": "...",
})


def normalize(s: str) -> str:
    """Collapse whitespace, fold typographic punctuation, lowercase.

    Compares CONTENT while ignoring layout and quote style. Currency symbols,
    commas, digits and signs are all preserved -- those are exactly what must
    not drift.
    """
    return re.sub(r"\s+", " ", s.translate(_PUNCT)).strip().lower()


def squash(s: str) -> str:
    """normalize(), with all spaces removed.

    A last-resort comparison for strings where only spacing differs, such as
    the model writing {"model", "method"} where the PDF has {"model","method"}.
    Used only to suppress false alarms, never to find new matches in prose.
    """
    return normalize(s).replace(" ", "")


def check_structure(page: Page):
    """Self-consistency: did the model contradict itself?

    Does NOT verify the values are right -- it cannot know that.

    Returns (problems, notes). Problems mean content or structure is wrong.
    Notes mean something was untidy but nothing was lost, so they are reported
    without failing the page.
    """
    problems = []
    notes = []

    # The model enumerates every block it can see BEFORE extracting any of
    # them. If it then emits fewer, it dropped content it had already
    # acknowledged -- which is exactly how a page of paragraphs plus one table
    # came back as the table alone, with every other check passing.
    # Direction matters here, so the comparison is deliberately asymmetric.
    #
    # Extracted FEWER than promised -> the model acknowledged content and then
    # dropped it. That is the failure that produced a .docx containing 0% of a
    # Lorem ipsum page, and it is a hard fail.
    #
    # Extracted MORE than promised -> the analysis undercounted, but nothing
    # was lost. Observed on the same page once fixed: it listed 2 blocks and
    # correctly extracted 6. Failing that would punish a right answer for
    # having a lazy plan, so it is only a note.
    # n_blocks is ADVISORY in both directions, and that is deliberate.
    #
    # Its real job is done before this check ever runs: committing to a number
    # up front is what stops the model extracting one table and calling a page
    # finished. But the number itself is an eyeball estimate -- measured at 10
    # against 14 real blocks on one page, and 7 against 6 on another -- so
    # failing a page on a miscount would reject correct extractions.
    #
    # Dropped content is caught by check_coverage() instead, which compares
    # against the PDF's own text rather than the model's guess. Ground truth
    # decides; the estimate only ever gets to raise an eyebrow.
    promised = page.analysis.n_blocks
    got = [b.kind for b in page.blocks]
    if len(got) != promised:
        notes.append(
            f"analysis expected {promised} blocks, {len(got)} extracted "
            f"{got} -- see check 4 for whether anything was actually lost"
        )

    for i, block in enumerate(page.blocks):
        label = f"block[{i}] kind={block.kind}"

        if isinstance(block, TableBlock):
            if not block.rows:
                problems.append(f"{label}: table with no data rows")
                continue

            if len(block.rows) != block.n_data_rows:
                problems.append(
                    f"{label}: claims n_data_rows={block.n_data_rows} but "
                    f"returned {len(block.rows)} rows"
                )

            # A headerless table (a plain grid of numbers, say) has no labels
            # to count, so the header is only checked when one is claimed.
            # Without has_header, such a table forced the model to promote its
            # first data row to a header, which then made n_data_rows
            # disagree with the rows by exactly one, every time.
            if block.has_header:
                if len(block.header) != block.n_cols:
                    problems.append(
                        f"{label}: claims n_cols={block.n_cols} but header has "
                        f"{len(block.header)} labels"
                    )
            elif block.header:
                problems.append(
                    f"{label}: has_header is false but {len(block.header)} "
                    f"header labels were returned"
                )

            ragged = [
                (r, len(row)) for r, row in enumerate(block.rows)
                if len(row) != block.n_cols
            ]
            if ragged:
                problems.append(
                    f"{label}: claims n_cols={block.n_cols} but rows "
                    f"{ragged} disagree"
                )
        else:
            if not block.text.strip():
                problems.append(f"{label}: non-table block has empty text")

    return problems, notes


def check_coverage(page: Page, source_text: str, threshold: float = 0.95):
    """How much of the PDF's text made it into the extraction?

    The mirror image of check_grounding, and the check that was missing.
    Grounding asks "is everything the model wrote really in the PDF?" -- it
    measures precision. This asks "is everything in the PDF really in what the
    model wrote?" -- it measures recall.

    Without it, a model that extracts one correct table and silently discards
    five paragraphs passes every other check: its 16 cells are all genuinely on
    the page, its counts are self-consistent, its table matches the detector.
    That happened on a Lorem ipsum page and produced a .docx containing 0% of
    the document.

    Returns (problems, coverage, missing_words, missing_lines). Only works on
    digital PDFs.
    """
    source = normalize(source_text)
    if not source:
        # NOT a failure -- there is simply nothing to measure against. It used
        # to return the message as a problem, so a scanned page reported
        # "FAIL (100% of the PDF's text was extracted)", which is both a
        # contradiction and a check that fails on every scan forever. A check
        # that cries wolf gets ignored, so coverage is None to mean "unknown"
        # and the caller reports N/A.
        return ([], None, [], [])

    parts = []
    for block in page.blocks:
        if isinstance(block, TableBlock):
            parts.extend(block.header)
            parts.extend(c for row in block.rows for c in row)
        else:
            parts.append(block.text)
    produced = normalize(" ".join(parts))
    produced_squashed = squash(" ".join(parts))

    # Words shorter than 4 characters carry little signal and match by
    # accident, so they are excluded from the measure.
    words = {w for w in source.split() if len(w) > 3}
    if not words:
        return ([], 1.0, [], [])

    missing = sorted(
        w for w in words
        if w not in produced and squash(w) not in produced_squashed
    )
    coverage = 1 - len(missing) / len(words)
    missing_lines = []
    source_seen = set()

    problems = []
    if coverage < threshold:
        shown = ", ".join(missing[:8])
        more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
        problems.append(
            f"only {coverage:.0%} of the PDF's text was extracted -- "
            f"{len(missing)} words missing: {shown}{more}"
        )

    # Word coverage measures VOCABULARY, and that is not enough on its own.
    # A dropped heading whose words appear elsewhere costs almost nothing:
    # "Employee Onboarding Summary" went missing from a real page and scored
    # 98%, because "employee" and "onboarding" both occur in the paragraph
    # below it and only "summary" was unique. One word out of 43.
    #
    # So every LINE of the source is checked for presence too. A whole line is
    # either there or it is not, regardless of how common its words are.
    # Comparing after whitespace collapsing means a paragraph the model
    # reflowed still matches -- only genuinely absent lines are reported.
    for line in source_text.splitlines():
        line_n = normalize(line)
        if len(line_n) < 8:            # page numbers, stray cell fragments
            continue
        if line_n in source_seen:
            continue
        source_seen.add(line_n)
        if line_n not in produced and squash(line) not in produced_squashed:
            missing_lines.append(line.strip())

    if missing_lines:
        shown = "; ".join(repr(m[:50]) for m in missing_lines[:4])
        more = (f" (+{len(missing_lines) - 4} more)"
                if len(missing_lines) > 4 else "")
        problems.append(
            f"{len(missing_lines)} line(s) of the PDF are missing "
            f"entirely: {shown}{more}"
        )

    return problems, coverage, missing, missing_lines


def ungrounded_text_blocks(page: Page, source_text: str) -> List[int]:
    """Indices of text blocks whose words are not in the PDF's text layer.

    Used to find text the model read out of a PICTURE rather than off the page.
    On a page whose logo is a 1352x969 raster, the model emitted a text block
    reading "FreeTestData" -- those characters exist nowhere in the document's
    text, only as pixels. Once the real image is embedded, that string is both
    a duplicate and wrong.

    This deliberately reuses the same comparison as check_grounding(), so a
    block is only ever called ungrounded on the same evidence the check
    reports. Tables are left alone: a misread cell is a data error to fix, not
    a caption to delete.
    """
    source = normalize(source_text)
    source_squashed = squash(source_text)
    if not source:
        return []

    out = []
    for i, block in enumerate(page.blocks):
        if isinstance(block, TableBlock) or not block.text.strip():
            continue
        if normalize(block.text) in source or squash(block.text) in source_squashed:
            continue
        words = [w for w in normalize(block.text).split() if len(w) > 3]
        hits = sum(1 for w in words if w in source)
        if not words or hits / len(words) < 0.9:
            out.append(i)
    return out


def ungrounded_table_blocks(page: Page, source_text: str) -> List[int]:
    """Indices of tables whose cells are ALL absent from the PDF's text layer.

    The model does not only read words out of pictures -- it reads structure.
    On a page whose only graphic was a bar chart, it read the axis labels and
    bar values and reported them as a 2x5 table:

        header: ['Jan','Feb','Mar','Apr','May']
        rows:   [['120','150','135','180','210']]

    None of those characters exist in the document's text. The page has a
    chart, not a table, and the chart is separately embedded as an image -- so
    the document ended up with the picture AND a table of the picture's
    contents.

    Two details this needs to get right:

    WHOLE WORDS, NOT SUBSTRINGS. The first version used substring matching and
    dropped nothing, because the page's prose says "in January to 47% in May"
    and "between March and April" -- so the cells Jan, Mar, Apr and May all
    "matched" as fragments of longer words. Cells are compared against the
    source's word set instead.

    A THRESHOLD, NOT ALL. Requiring every cell to be ungrounded is too strict
    for the same reason: one cell reading "May" legitimately appears in the
    prose. 80% separates the cases cleanly -- the chart table scores 90%
    ungrounded, while a real 49-cell table with one misread scores 2%.

    ungrounded_text_blocks() excludes tables for a good reason: a table with
    one bad cell is real data with a defect, and deleting it would lose 48
    correct cells to fix one wrong one. The threshold preserves that -- this
    only fires on a table that was invented wholesale.

    Only meaningful on digital pages: a scanned page has no text layer, so
    nothing can be ungrounded against it and this returns [].
    """
    source = normalize(source_text)
    if not source:
        return []
    words = set(source.split())
    squashed = squash(source_text)

    out = []
    for i, block in enumerate(page.blocks):
        if not isinstance(block, TableBlock):
            continue
        cells = [c for c in list(block.header)
                 + [c for row in block.rows for c in row] if c.strip()]
        if not cells:
            continue

        def grounded(cell: str) -> bool:
            parts = normalize(cell).split()
            if parts and all(p in words for p in parts):
                return True
            # Longer strings are distinctive enough that a substring match is
            # safe; short ones are what produced the false positives.
            n = normalize(cell)
            return len(n) > 8 and (n in source or squash(cell) in squashed)

        ungrounded = sum(1 for c in cells if not grounded(c))
        if ungrounded / len(cells) >= 0.8:
            out.append(i)
    return out


def check_grounding(page: Page, source_text: str):
    """Does every string the model produced really appear in the PDF?

    The valuable check: real ground truth, not a heuristic. Catches
    hallucination and misreads -- the "right shape, wrong value" failure that
    self-consistency can never see.

    Returns (problems, found, total). Only works on digital PDFs.
    """
    problems = []
    source = normalize(source_text)
    source_squashed = squash(source_text)
    found = 0
    total = 0

    if not source:
        # Not a failure, just unanswerable -- see the note in check_coverage().
        # total == 0 is the caller's signal that nothing could be verified.
        return ([], 0, 0)

    def present(s: str) -> bool:
        return normalize(s) in source or squash(s) in source_squashed

    for i, block in enumerate(page.blocks):
        label = f"block[{i}] kind={block.kind}"

        if isinstance(block, TableBlock):
            strings = list(block.header) + [c for row in block.rows for c in row]
            missing = []
            for s in strings:
                if not s.strip():
                    continue
                total += 1
                if present(s):
                    found += 1
                else:
                    missing.append(s)
            if missing:
                shown = ", ".join(repr(m) for m in missing[:6])
                more = f" (+{len(missing) - 6} more)" if len(missing) > 6 else ""
                problems.append(
                    f"{label}: {len(missing)} cell(s) not in PDF text: {shown}{more}"
                )
        else:
            if not block.text.strip():
                continue
            total += 1
            if present(block.text):
                found += 1
            else:
                # Word-level fallback -- a paragraph the model lightly reflowed
                # is different from one it invented.
                words = [w for w in normalize(block.text).split() if len(w) > 3]
                hits = sum(1 for w in words if w in source)
                cov = hits / len(words) if words else 0
                if cov >= 0.9:
                    found += 1
                else:
                    problems.append(
                        f"{label}: text not found in PDF "
                        f"({cov:.0%} of words matched)"
                    )

    return problems, found, total


# ===========================================================================
# THE MODEL CALL -- one per page
# ===========================================================================

def extract_page(image_path: str, model: str = MODEL_NAME,
                 num_ctx: int = 8192, num_predict: int = 4000) -> Page:
    """Send one page image to the local model; get back analysis + blocks + review.

    num_ctx is raised above Ollama's 4096 default because the reasoning fields
    plus a dense page's JSON can exceed it, and an overflowing context silently
    truncates the output rather than erroring.
    """
    response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT, "images": [image_path]},
        ],
        format=Page.model_json_schema(),   # decoder cannot emit invalid JSON
        options={
            "temperature": 0,
            "num_ctx": num_ctx,
            # Hard ceiling on generation. Not a tuning knob -- a circuit
            # breaker. An unbounded array in the schema plus greedy decoding
            # (temperature 0) can loop: at every position the grammar allows
            # either another item or closing the array, and if "another item"
            # stays marginally more likely, the array never ends. That happened
            # on a bullet-heavy page -- block_kinds emitted "list" until it
            # filled the whole context, 23 minutes, and never reached the
            # actual content. The arrays are capped now, but this stops any
            # future runaway costing half an hour before anyone notices.
            "num_predict": num_predict,
        },
    )

    if response.get("done_reason") == "length":
        raise ValueError(
            f"model hit the {num_predict}-token generation limit without "
            f"finishing -- output was truncated and cannot be parsed. This "
            f"usually means a runaway list in the analysis stage."
        )

    return Page.model_validate_json(response["message"]["content"])
