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

import os
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


class TextBlockNoLook(BaseModel):
    """TextBlock without align/size/bold -- for pages where measuring beats asking.

    On a DIGITAL page (see measure_block_look() in test_pdf.py) the text layer
    gives the real font size and x-position of every line, so asking the model
    to guess align/size/bold there is pure waste: real generation time (these
    three fields measured ~17 tokens/block, field names plus values, tokenized
    directly against the running server) spent on values that get overwritten
    unconditionally, before the page is ever written out. This schema is used
    ONLY when the caller already knows -- before the model is even called --
    that those three fields will be measured afterward, never read from here.

    Same field ORDER logic as TextBlock: not applicable, since there is nothing
    left after text to reorder.
    """
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


class PageNoLook(BaseModel):
    """Page, with TextBlockNoLook instead of TextBlock -- see that class."""
    analysis: LayoutAnalysis = Field(
        description="Your reasoning about the page layout, BEFORE extracting."
    )
    blocks: List[Union[TableBlock, TextBlockNoLook]] = Field(
        description="Every block on the page, in top-to-bottom reading order."
    )


def _add_look_placeholders(page: "PageNoLook") -> "Page":
    """Expand a PageNoLook into a canonical Page.

    align/size/bold are filled with placeholders, never read as real values --
    the only caller of the *NoLook schema (test_pdf.py, digital pages) always
    runs measure_block_look() immediately afterward, which overwrites all
    three unconditionally before the page is used for anything else.
    """
    blocks = [
        b if isinstance(b, TableBlock) else
        TextBlock(kind=b.kind, text=b.text, align="left", size="normal",
                  bold=False)
        for b in page.blocks
    ]
    return Page(analysis=page.analysis, blocks=blocks)


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
# Bullet characters get the same treatment, for a real, confirmed reason: a
# PDF's text layer stores a list item's bullet as whatever glyph the author
# used ("•"), while the model's own list rendering always uses a plain "-".
# Without folding these too, check_coverage()'s line-match sees "• Generated
# programmatically..." and "- Generated programmatically..." as unrelated
# strings -- the words are identical, only the bullet differs -- calls the
# line "missing entirely", and repair_missing_lines() then RE-INSERTS it as a
# new block. The model's extraction was correct; the repair step was the one
# creating a visible duplicate, by "fixing" content that was never missing.
_PUNCT = str.maketrans({
    "‘": "'", "’": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
    "…": "...",
    "•": "-", "◦": "-", "▪": "-", "▫": "-",
    "‣": "-", "·": "-",
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


_BULLET_CHARS = "-*•●‣ \t"


def drop_duplicate_blocks(page: Page, source_text: str = None):
    """Remove TextBlocks the model repeated -- a real, confirmed bug, two shapes.

    Under greedy decoding (temperature 0) with no repeat_penalty, the model
    was observed writing a page's real content correctly and then, instead of
    closing the blocks array, continuing to generate content already written.
    repeat_penalty (see extract_page()) fixes this at the source, confirmed by
    re-running the same page. This is the deterministic backstop for whatever
    repeat_penalty misses -- confirmed necessary in practice: it is NOT 100%
    reliable even with repeat_penalty on, seen on a real document where the
    same page extracted cleanly on some runs and not others.

    Two distinct shapes of the same bug, both handled here:

      1. WHOLE-BLOCK repeat -- an earlier block re-emitted verbatim as a later
         block. On a real 4-block page, blocks 1 and 3 were each repeated
         twice more before the array finally closed.

      2. LIST-ITEM repeat -- a "list" block (all bullets correctly grouped as
         ONE block, per the schema) followed by those SAME items generated
         AGAIN individually, one per extra block. Caught on a real document:
         a 3-item list, then all 3 items repeated as 3 separate standalone
         blocks right after it. A whole-block check alone misses this, since
         no single later block matches the list block's full text -- each one
         only matches ONE of its items. Normalizing away a leading bullet
         marker (-, *, bullet characters) is necessary here because the
         model formats the repeat differently each time ("- item" inside the
         list, "* item" or "item" alone outside it).

    Text over 12 normalized characters counts -- a short repeated label
    ("Total", "N/A") can legitimately appear twice on a page and is not this
    bug. 12 is deliberate: caught on a real document at exactly 20 characters
    ("Monthly Active Users", emitted once as a heading and once as a
    separate paragraph) -- a higher cutoff would have missed that real
    duplicate. The first occurrence is always kept; later repeats, of any
    shape, are dropped.

      3. TABLE repeat, whole or fragment -- confirmed necessary on a real
         document: a small standalone table appeared whose rows were exact
         repeats of rows already inside an earlier, larger table on the same
         page. Left unchecked, this doesn't just look redundant -- it can
         actively confuse merge_nested_tables() into pairing a genuinely
         nested table with this stray duplicate instead of the real one,
         producing a wrong merge. Checked two ways: an exact whole-table
         repeat (same header and rows as one already kept), and a table
         whose EVERY row already appeared as a row in some earlier table --
         a full subset, not a coincidental one-row overlap (which two
         genuinely different tables can share by chance, e.g. both having a
         "Total" row).

      4. GENUINE REPEATS (only when source_text is given). A document can
         really contain the same paragraph twice -- boilerplate, or a test page
         built from repeated placeholder text -- and dropping the second copy
         loses real content. On a digital page the PDF's own text says how many
         times a passage occurs, so a text block is then allowed to appear that
         many times (never fewer than once) before further copies are dropped.
         Without source_text (scans, raw images) nothing changes.

    Returns (new_page, dropped_descriptions).
    """
    source_norm = normalize(source_text) if source_text else None
    text_counts = {}
    seen = set()
    seen_list_items = set()
    seen_tables = set()
    seen_table_rows = set()
    kept, dropped = [], []

    for block in page.blocks:
        if isinstance(block, TableBlock):
            row_keys = [
                normalize(" ".join(str(c) for c in row)) for row in block.rows
            ]
            table_key = normalize(
                " ".join(block.header) + " ".join(k for k in row_keys)
            )
            substantive = [k for k in row_keys if len(k) > 12]

            if table_key in seen_tables and block.rows:
                dropped.append(f"table (header={block.header})")
                continue
            if substantive and all(k in seen_table_rows for k in substantive):
                dropped.append(f"table (header={block.header}, "
                               f"all rows already seen)")
                continue

            seen_tables.add(table_key)
            seen_table_rows.update(row_keys)
            kept.append(block)
            continue

        if not block.text.strip():
            kept.append(block)
            continue

        key = normalize(block.text)
        bare = key.strip(_BULLET_CHARS)

        if len(key) > 12 and key in seen:
            allowed = 1
            if source_norm is not None:
                # Counted from the start and the end of the passage, not the
                # whole of it: the model's copy often carries a misread word in
                # the middle, so the whole passage is not found in the PDF at
                # all (measured: 0 occurrences of a paragraph the PDF has twice).
                anchors = [key[:40], key[-40:]] if len(key) >= 40 else [key]
                allowed = max(1, *(source_norm.count(a) for a in anchors))
            if text_counts.get(key, 1) >= allowed:
                dropped.append(block.text[:60])
                continue
            text_counts[key] = text_counts.get(key, 1) + 1
            kept.append(block)
            continue
        if (block.kind != "list" and len(bare) > 12
                and bare in seen_list_items):
            dropped.append(block.text[:60])
            continue

        seen.add(key)
        if block.kind == "list":
            for line in block.text.splitlines():
                item = normalize(line).strip(_BULLET_CHARS)
                if item:
                    seen_list_items.add(item)
        kept.append(block)

    if not dropped:
        return page, []
    return Page(analysis=page.analysis, blocks=kept), dropped


def fix_trailing_heading_after_table(page: Page):
    """Move a heading that trails a table back in front of it.

    A real, confirmed model ordering mistake -- NOT a one-off: reproduced
    consistently across repeated runs of the same document, unlike the
    content-dropping issue elsewhere in this project, which varies run to
    run. The model commits to a table's shape in its own analysis stage
    before writing any blocks, and appears to only assign the table's label
    once the table itself is already fully written -- describing it in
    hindsight ("Sample table") as the very last block, instead of
    introducing it first the way every other heading on the page does.

    Deterministic, not a guess, and narrow on purpose: a heading almost
    always introduces what follows it, so one with NOTHING after it, sitting
    immediately after a table, doesn't fit that pattern -- a strong, safe
    signal it belongs in front of the table instead. A table followed by a
    heading that goes on to introduce more content is a completely normal
    document structure and is left untouched; this only fires for the one
    specific, detectable shape: table, then a lone trailing heading, then
    nothing else.

    Returns (new_page, fixed_count).
    """
    blocks = list(page.blocks)
    if len(blocks) < 2:
        return page, 0

    last, second_last = blocks[-1], blocks[-2]
    trailing_heading = (
        not isinstance(last, TableBlock) and last.kind == "heading"
    )
    if trailing_heading and isinstance(second_last, TableBlock):
        blocks[-2], blocks[-1] = blocks[-1], blocks[-2]
        return Page(analysis=page.analysis, blocks=blocks), 1

    return page, 0


def merge_nested_tables(page: Page):
    """Merge a table that's actually nested inside another table's cell.

    Our schema has no way to represent "this cell contains another whole
    table" -- TableBlock is a flat 2D grid only. Faced with genuine nesting
    (confirmed on a real document, up to 3 levels deep: a table inside a
    table inside a table cell), the model does the only thing the schema
    allows: emits each level as its own separate, independent TableBlock,
    one right after the other.

    The real, detectable signal that the next table is nested rather than
    a new independent one: the table immediately before it has a genuinely
    BLANK trailing cell in its last row -- the "couldn't fit the real
    content here" placeholder left behind. Column-width comparison alone is
    NOT reliable -- confirmed on the same real document: a doubly-nested
    table can be the exact same width as its own immediate parent (both 2
    columns), so "narrower than the previous table" misses it entirely.

    Always merges the DEEPEST nesting first (the last such blank-cell
    trigger found in the block list, not the first), because a document's
    most deeply nested table always appears later in the flattened
    sequence than its ancestors -- repeating until no trigger remains
    correctly unwinds any depth of nesting, not just one level.

    Returns (new_page, merge_descriptions).
    """
    blocks = list(page.blocks)
    merges = []

    while True:
        trigger_i = None
        for i in range(len(blocks) - 1):
            a, b = blocks[i], blocks[i + 1]
            if (isinstance(a, TableBlock) and isinstance(b, TableBlock)
                    and a.rows and a.rows[-1] and a.rows[-1][-1] == ""):
                trigger_i = i  # keep overwriting -- the LAST match wins,
                                # so the deepest nesting resolves first
        if trigger_i is None:
            break

        parent, child = blocks[trigger_i], blocks[trigger_i + 1]
        parent_rows = list(parent.rows)
        last_row = parent_rows.pop()
        parent_prefix = last_row[:-1]                # drop the blank placeholder cell
        parent_header_prefix = (
            parent.header[:-1] if parent.has_header else []
        )

        child_width = (
            len(child.header) if child.has_header
            else (len(child.rows[0]) if child.rows else 0)
        )
        child_header = (
            child.header if child.has_header else [""] * child_width
        )
        new_header = parent_header_prefix + child_header
        width = len(new_header)

        # Rows the parent already had that were NEVER nested get padded to
        # the new, wider shape -- they have nothing to contribute to the
        # columns the merge just added.
        padded_old_rows = [
            (list(r) + [""] * width)[:width] for r in parent_rows
        ]
        # The parent's own last row, repeated once per child row -- this is
        # what "this cell contained a whole table" actually unpacks to.
        expanded_rows = [list(parent_prefix) + list(r) for r in child.rows]

        merged = TableBlock(
            kind="table",
            has_header=bool(parent.has_header or child.has_header),
            n_data_rows=len(padded_old_rows) + len(expanded_rows),
            n_cols=width, header=new_header,
            rows=padded_old_rows + expanded_rows,
        )

        label = "/".join(str(c) for c in parent_prefix if c) or "its parent row"
        merges.append(f"merged a {len(child.rows)}-row nested table into {label}")
        blocks[trigger_i:trigger_i + 2] = [merged]

    if not merges:
        return page, []
    return Page(analysis=page.analysis, blocks=blocks), merges


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
# SNAP TO THE TEXT LAYER -- correct the model's misreads from ground truth
# ===========================================================================

def snap_to_text_layer(page: Page, source_text: str,
                       min_similarity: float = 0.75):
    """Replace a block's misread words with the exact ones from the PDF.

    THE PROBLEM THIS SOLVES. On a digital page the model sometimes misreads a
    word or two ("Integre" for "Integer"). check_coverage() then finds the
    correct line "missing", and repair_missing_lines() INSERTS it as a new block
    while the misread paragraph stays: the page ends up with the paragraph twice,
    once wrong and once exact, and coverage reads 100% because every word is
    now present somewhere. Measured: a page came out 1.43x the PDF's length.

    THE FIX. Before that check runs, each block is matched to the stretch of the
    PDF's own text it was read from, and if the match is close, the block's text
    is replaced by the PDF's exact characters. A misread is then simply gone and
    nothing is "missing" to be re-inserted. Text the model invented, which has no
    close match anywhere, is left alone and is still caught by the grounding
    checks. This is the same principle already used for repairs: on a digital
    page the text layer is the author's actual characters, so copying from it is
    safe in a way that copying from OCR is not.

    HOW A BLOCK IS MATCHED
      - Paragraphs: the block's words are aligned to the page's word stream
        (difflib), the matching stretch is located, and if at least
        min_similarity of the words agree, the stretch replaces the block. A
        stretch already claimed by an earlier block is never reused, so two
        blocks cannot both snap onto the same passage.
      - A block with line breaks (a list, an address) is matched line by line so
        its line structure survives; a leading bullet is kept as it was.
      - Very short text (1-2 words) and table cells: the closest whole line of
        the PDF's text (cutoff 0.8), only when the text is not already an exact
        line of the PDF. A cell that is already exactly right is never touched.

    Returns (new_page, snaps) where snaps is a list of (before, after) strings
    for the caller to report. Blocks of kind "image" are skipped: text read out
    of a picture is not in the text layer, and dropping it is a different step.
    """
    import difflib

    src_tokens = source_text.split()
    if not src_tokens:
        return page, []
    norm_src = [normalize(t) for t in src_tokens]
    src_lines = [ln.strip() for ln in source_text.splitlines() if ln.strip()]
    line_by_norm = {}
    for ln in src_lines:
        line_by_norm.setdefault(normalize(ln), ln)
    line_keys = list(line_by_norm)
    claimed = [False] * len(src_tokens)
    snaps = []
    # Long words of the PDF, for the word-level pass below. Matched CASE-
    # SENSITIVELY and as written: lowercasing them merged "ABC..." with "abc...",
    # and a lowercase alphabet was "corrected" into the uppercase one.
    src_exact = set(src_tokens)
    src_long = sorted({t for t in src_tokens if len(t) >= 6})

    def close_line(text: str):
        """The PDF line closest to a short string, or None."""
        key = normalize(text)
        if not key or key in line_by_norm:
            return None
        hit = difflib.get_close_matches(key, line_keys, n=1, cutoff=0.8)
        if not hit:
            return None
        found = line_by_norm[hit[0]]
        if not 0.6 <= len(found) / max(len(text), 1) <= 1.6:
            return None
        return found

    def snap_span(text: str):
        """The PDF's exact words for a run of 3+ words, or None."""
        words = text.split()
        if len(words) < 3:
            return None
        nb = [normalize(w) for w in words]
        if " ".join(nb) in " ".join(norm_src):
            return None                       # already exactly in the PDF
        # Passages already given to an earlier block are hidden from the match,
        # so when the PDF genuinely repeats a paragraph, the second copy of it
        # in the model's output finds the PDF's second occurrence instead of
        # colliding with the first.
        visible = [t if not claimed[i] else "\x00" for i, t in enumerate(norm_src)]
        matches = [m for m in difflib.SequenceMatcher(
            None, nb, visible, autojunk=False).get_matching_blocks() if m.size]
        if not matches:
            return None
        matches.sort(key=lambda m: m.b)
        clusters, cur = [], [matches[0]]
        for m in matches[1:]:
            if m.b - (cur[-1].b + cur[-1].size) > len(words) + 5:
                clusters.append(cur)
                cur = [m]
            else:
                cur.append(m)
        clusters.append(cur)
        best = max(clusters, key=lambda c: sum(m.size for m in c))
        first, last = best[0], best[-1]
        # Words before the first agreeing word and after the last one are the
        # misread ones, and take the same number of words from the PDF.
        start = max(0, first.b - first.a)
        end = min(len(src_tokens),
                  last.b + last.size + (len(words) - (last.a + last.size)))
        if end <= start or not 0.7 <= (end - start) / len(words) <= 1.4:
            return None
        sim = difflib.SequenceMatcher(None, nb, norm_src[start:end],
                                      autojunk=False).ratio()
        if sim < min_similarity:
            return None
        if sum(claimed[start:end]) > 0.3 * (end - start):
            return None
        for i in range(start, end):
            claimed[i] = True
        return " ".join(src_tokens[start:end])

    def snap_words(body: str):
        """Fix individual long words that are not in the PDF but nearly are.

        The stretch match above gives up when too little of a line agrees, e.g. a
        line that is mostly one long, unusual string ("ABCDEFGHJLKMNOPQRSTUWXYZ"
        for the alphabet). Here each word of 6+ characters that the PDF does not
        contain is replaced by the PDF word it is at least 88% similar to. The high
        cutoff is deliberate: a real, different word must never be "corrected".
        """
        parts = re.split(r"(\s+)", body)
        changed = False
        for i, part in enumerate(parts):
            if len(part) < 6 or part.isspace() or part in src_exact:
                continue
            hit = difflib.get_close_matches(part, src_long, n=1, cutoff=0.88)
            if hit and hit[0] != part:
                parts[i] = hit[0]
                changed = True
        return "".join(parts) if changed else None

    def snap_text(text: str):
        """One line or paragraph -> corrected text, or None if unchanged."""
        body = text.lstrip(_BULLET_CHARS)
        prefix = text[:len(text) - len(body)]
        words = body.split()
        if not words:
            return None
        fixed = snap_span(body) if len(words) >= 3 else close_line(body)
        if fixed is None:
            fixed = snap_words(body)
        return None if fixed is None or fixed == body else prefix + fixed

    out_blocks = []
    for block in page.blocks:
        if isinstance(block, TableBlock):
            changed = False

            def fix_cell(c: str) -> str:
                nonlocal changed
                if len(c.strip()) < 4:
                    return c
                found = close_line(c)
                if found is None or found == c:
                    return c
                snaps.append((c, found))
                changed = True
                return found

            header = [fix_cell(c) for c in block.header]
            rows = [[fix_cell(c) for c in row] for row in block.rows]
            out_blocks.append(
                block.model_copy(update={"header": header, "rows": rows})
                if changed else block)
            continue

        if block.kind == "image" or not block.text.strip():
            out_blocks.append(block)
            continue

        if "\n" in block.text.strip():
            new_lines, changed = [], False
            for line in block.text.split("\n"):
                fixed = snap_text(line) if line.strip() else None
                if fixed is not None:
                    snaps.append((line.strip(), fixed.strip()))
                    changed = True
                new_lines.append(fixed if fixed is not None else line)
            new_text = "\n".join(new_lines)
        else:
            fixed = snap_text(block.text.strip())
            changed = fixed is not None
            new_text = fixed if changed else block.text
            if changed:
                snaps.append((block.text.strip(), fixed))
        out_blocks.append(
            block.model_copy(update={"text": new_text}) if changed else block)

    if not snaps:
        return page, []
    return Page(analysis=page.analysis, blocks=out_blocks), snaps


# ===========================================================================
# THE MODEL CALL -- one per page
# ===========================================================================

# The default model endpoint every script talks to unless overridden. Kept as
# one named constant, not a literal repeated in every script's argparse
# default, for the same reason established earlier this session: a run that
# omits --base-url must still hit a KNOWN, INTENDED model, never silently
# fall back to something else (see the real incident this closes, below).
#
# Currently: a remote Qwen3-VL-8B-Instruct (Q4_K_M) endpoint, served by the
# same llama-server stack, over the local network -- switched from the local
# 2B setup on 2026-09-28 after a real, direct comparison confirmed the exact
# same client code, prompt, and JSON schema work unchanged against it (only
# the base URL differs), and measured it faster on both phases (prefill
# 14.07s vs ~68s, generate ~19.25 tok/s vs ~12-13 tok/s, on the identical test
# page). This is a genuine dependency change, not a tuning tweak: every run
# now requires network reachability to another machine (10.0.3.33:8080),
# which is not under this project's control the way the local server was.
# If that endpoint is unreachable, pass --base-url explicitly to fall back
# to the local server (still fully configured, see below) rather than assume
# the remote one is always up.
#
# The original incident this pattern closes, for context: a run of
# image_to_word.py that omitted --base-url once silently fell back to
# Ollama's own bundled qwen3-vl:2b-instruct instead of failing -- and that
# build measurably dropped content (a whole table, merged headings) on a
# page the intended model handled cleanly. Pass base_url=None explicitly to
# opt into Ollama on purpose (e.g. to reach its 4B model for a hard page).
DEFAULT_BASE_URL = "http://10.0.3.33:8080"

# The local, fully-controlled setup this replaced as the default -- still
# valid, still running, not removed. Use this explicitly (--base-url
# http://127.0.0.1:8090) if the remote endpoint above is unreachable or you
# want the original, locally-hosted 2B model specifically.
LOCAL_BASE_URL = "http://127.0.0.1:8090"


def extract_page(image_path: str, model: str = MODEL_NAME,
                 num_ctx: int = 8192, num_predict: int = 4000,
                 return_timing: bool = False, base_url: str = DEFAULT_BASE_URL,
                 include_look: bool = True):
    """Send one page image to the local model; get back analysis + blocks + review.

    include_look=False switches the request schema from Page/TextBlock to
    PageNoLook/TextBlockNoLook, dropping align/size/bold from what the model
    is asked to produce. Only pass False when the caller will immediately
    measure those three fields itself afterward (digital pages -- see
    measure_block_look() in test_pdf.py); otherwise every block silently gets
    placeholder appearance values that are never corrected. Default stays True
    so existing callers (image_to_pdf.py, which has no text layer to measure
    from) are unaffected.

    num_ctx is raised above Ollama's 4096 default because the reasoning fields
    plus a dense page's JSON can exceed it, and an overflowing context silently
    truncates the output rather than erroring.

    return_timing=True additionally returns a dict broken down from the
    server's OWN internal timers (nanoseconds since epoch-relative durations
    it tracks server-side, not anything measured on the Python side): how long
    loading the model took, how long it spent reading the page image + prompt
    ("prefill"), and how long it spent writing the actual output token by
    token ("generation"). Default is False so this stays a no-op for existing
    callers (test_json.py) that only expect a Page back.

    base_url, when given, bypasses Ollama's daemon entirely and calls a raw
    llama-server instance directly via its OpenAI-compatible API (e.g.
    "http://127.0.0.1:8090"). This exists because Ollama's own Modelfile
    format has no documented way to attach a SEPARATE mmproj file to a model
    -- confirmed against Ollama's own docs and matching GitHub issues, where
    attempting it silently produces a model with no vision capability. Every
    Ollama registry build we inspected bundles model+mmproj into one file for
    exactly this reason. Since Ollama's own llama-server binary genuinely does
    accept separate --model/--mmproj flags when run directly, this lets us
    test GGUF files pulled straight from a source like Hugging Face -- e.g.
    Qwen's official Q4_K_M model paired with their standalone Q8_0 mmproj --
    without Ollama's packaging getting in the way. The model/format/schema
    logic is otherwise identical; only the transport changes.
    """
    if base_url:
        return _extract_page_raw_server(
            image_path, base_url, num_ctx, num_predict, return_timing,
            include_look,
        )

    schema_cls = Page if include_look else PageNoLook
    response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT, "images": [image_path]},
        ],
        format=schema_cls.model_json_schema(),   # decoder cannot emit invalid JSON
        options={
            "temperature": 0,
            "num_ctx": num_ctx,
            # Without this, greedy decoding (temperature 0) has NOTHING
            # discouraging it from re-emitting a block it already wrote.
            # Confirmed directly: on a real digital page, the model correctly
            # wrote 4 blocks (heading, paragraph, table, paragraph) and then,
            # with repeat_penalty at its default of 1.0 (off), started
            # re-generating blocks 1 and 3 verbatim -- twice each -- before
            # finally closing the array. The blocks array has to stay
            # unbounded (page content varies), so unlike block_kinds below it
            # cannot be fixed by removing the array; this is the standard,
            # correct lever for exactly this failure mode.
            "repeat_penalty": 1.15,
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

    page = schema_cls.model_validate_json(response["message"]["content"])
    if not include_look:
        page = _add_look_placeholders(page)

    if not return_timing:
        return page

    # Ollama returns every duration in nanoseconds; /1e9 converts to seconds.
    # These three numbers are computed by Ollama itself while it processes the
    # request -- not something guessed or timed from outside.
    timing = {
        "load_s": (response.get("load_duration") or 0) / 1e9,
        "prefill_s": (response.get("prompt_eval_duration") or 0) / 1e9,
        "prefill_tokens": response.get("prompt_eval_count") or 0,
        "generate_s": (response.get("eval_duration") or 0) / 1e9,
        "generate_tokens": response.get("eval_count") or 0,
    }
    return page, timing


def _mime_of(raw: bytes) -> str:
    """The image's real type. The request used to label every image image/png,
    even a JPEG; a strict server may refuse a mislabelled one."""
    if raw[:2] == b"\xff\xd8":
        return "image/jpeg"
    return "image/png"


def _fit_max_side(image_path: str, max_side: int):
    """PNG bytes of the image shrunk so its longest side is max_side, or None
    when it is already that small (the caller then sends the file untouched)."""
    import io

    from PIL import Image

    with Image.open(image_path) as im:
        w, h = im.size
        if max(w, h) <= max_side:
            return None
        scale = max_side / max(w, h)
        small = im.convert("RGB").resize(
            (max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "PNG")
    return buf.getvalue()


def _extract_page_raw_server(image_path: str, base_url: str,
                             num_ctx: int, num_predict: int,
                             return_timing: bool, include_look: bool = True):
    """The base_url path: talk to a raw llama-server directly, not Ollama.

    Uses its OpenAI-compatible /v1/chat/completions endpoint. Same system
    prompt, same user prompt, same JSON schema as the Ollama path -- only the
    transport and the base64 image-embedding differ, since a raw llama-server
    has no equivalent of Ollama's images=[path] convenience.

    REMOTE, AUTHENTICATED SERVERS. Four optional settings are read from the
    environment, so every script picks them up without a new flag. With none of
    them set the request is exactly what it always was (the local server case).

        IDOX_API_KEY          sent as "Authorization: Bearer <key>". Never
                              printed, logged or written anywhere by this code.
        IDOX_THINKING         "off" or "on": sent as chat_template_kwargs
                              {"enable_thinking": ...}. Off is right for
                              extraction; on can add minutes per page.
        IDOX_MAX_IMAGE_SIDE   shrink the image so its longest side is at most
                              this many pixels before sending (e.g. 1600).
        IDOX_TIMEOUT          seconds to wait for a reply (default 600).
        IDOX_MODEL            the "model" field (default "manual"; ignored by
                              llama-server).
    """
    import base64
    import time as _time

    import requests

    api_key = os.environ.get("IDOX_API_KEY", "").strip()
    thinking = os.environ.get("IDOX_THINKING", "").strip().lower()
    max_side = int(os.environ.get("IDOX_MAX_IMAGE_SIDE", "0") or 0)
    timeout = int(os.environ.get("IDOX_TIMEOUT", "600") or 600)

    raw = _fit_max_side(image_path, max_side) if max_side > 0 else None
    if raw is None:
        with open(image_path, "rb") as f:
            raw = f.read()
    img_b64 = base64.b64encode(raw).decode()
    mime = _mime_of(raw)

    schema_cls = Page if include_look else PageNoLook
    started = _time.time()
    resp = requests.post(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        json={
            "model": os.environ.get("IDOX_MODEL", "manual") or "manual",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": USER_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                ]},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "page", "schema": schema_cls.model_json_schema()},
            },
            "temperature": 0,
            # See the matching comment in the Ollama path above -- confirmed
            # directly on a real page: without this, greedy decoding re-emitted
            # already-written blocks verbatim instead of stopping the array.
            "repeat_penalty": 1.15,
            "n_predict": num_predict,
            **({"chat_template_kwargs": {"enable_thinking": thinking == "on"}}
               if thinking in ("on", "off") else {}),
        },
        timeout=timeout,
    )
    wall_elapsed = _time.time() - started
    if resp.status_code == 401:
        raise RuntimeError("the server rejected the API key (401): the key is "
                           "missing or wrong")
    if not resp.ok:
        raise RuntimeError(f"the server answered {resp.status_code}: "
                           f"{resp.text[:200].strip()}")
    d = resp.json()

    finish_reason = d["choices"][0].get("finish_reason")
    if finish_reason == "length":
        raise ValueError(
            f"model hit the {num_predict}-token generation limit without "
            f"finishing -- output was truncated and cannot be parsed."
            + (" Thinking is on, which spends those tokens before the answer."
               if thinking == "on" else "")
        )

    content = d["choices"][0]["message"].get("content") or ""
    if not content.strip():
        raise RuntimeError("the model returned no text (thinking may be on, "
                           "or the answer was cut off)")
    page = schema_cls.model_validate_json(content)
    if not include_look:
        page = _add_look_placeholders(page)

    if not return_timing:
        return page

    # A raw llama-server DOES report real prefill/generate sub-durations --
    # a "timings" block (a llama.cpp server extension, not standard OpenAI
    # API): prompt_ms and predicted_ms, both server-measured. Confirmed by
    # inspecting a live response rather than assumed. load_s is left at 0
    # because a model already resident (as ours is, kept warm across this
    # session) has nothing to separately report -- unlike Ollama, this
    # endpoint has no distinct load_duration field either way.
    usage = d.get("usage", {})
    t = d.get("timings", {})
    timing = {
        "load_s": 0.0,
        "prefill_s": (t.get("prompt_ms") or 0) / 1000,
        "prefill_tokens": usage.get("prompt_tokens") or t.get("prompt_n") or 0,
        "generate_s": (t.get("predicted_ms") or 0) / 1000,
        "generate_tokens": usage.get("completion_tokens") or t.get("predicted_n") or 0,
    }
    return page, timing
