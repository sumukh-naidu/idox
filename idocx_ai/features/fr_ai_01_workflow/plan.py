"""FR-AI-01 workflow planning (brief section 2.1): turn a free-text instruction into an ordered step plan.

The AI only plans; iDocx runs the steps. The model proposes the steps, and this code decides what is allowed:
every operation must be on the fixed list, every file must be one the user selected (or the output of an earlier
step), every setting must belong to its operation. Nothing is run here and nothing is guessed: an unclear
instruction, or a file that was not selected, gets an error or a question, never a plan.
"""

import json
import re

from core import llm, rules, store

MAX_INSTRUCTION_CHARS = 2000    # brief: "very long instruction": the limit is TBC (section 4, Q4)
MAX_FILES = 49                  # brief: "50+ files" is an error; the limit is TBC
LANGUAGES = ["English", "Hindi", "Tamil", "Spanish", "German"]
SUMMARY_TYPES = ["3-bullet summary", "key milestone and obligation dates", "signatories and compliance flags"]

# operation -> (required settings, optional settings, how many files, words in an instruction that point to it)
OPERATIONS: dict[str, dict] = {
    "merge": {"required": [], "optional": ["name"], "files": (2, None), "words": r"merge|combine|join|put together"},
    "split": {"required": ["parts"], "optional": [], "files": (1, 1), "words": r"split|separate|divide|break up"},
    "remove blank pages": {"required": [], "optional": [], "files": (1, None), "words": r"blank"},
    "remove duplicates": {"required": [], "optional": [], "files": (1, None), "words": r"duplicate|repeated|same page"},
    "clean": {"required": [], "optional": ["remove_metadata"], "files": (1, None),
              "words": r"clean|metadata|sanitis|sanitiz|strip"},
    "compress": {"required": [], "optional": ["level"], "files": (1, None),
                 "words": r"compress|smaller|reduce (the )?size|shrink"},
    "rename": {"required": ["name"], "optional": [], "files": (1, 1), "words": r"rename|call it|name it|named"},
    "extract": {"required": [], "optional": ["pages", "fields"], "files": (1, 1),
                "words": r"extract|pull out|take out|only pages?"},
    "create task": {"required": ["title"], "optional": ["due"], "files": (1, None),
                    "words": r"task|to-?do|remind|deadline|action item"},
    "summarise": {"required": [], "optional": ["type"], "files": (1, None), "words": r"summar|overview|gist|key points"},
    "translate": {"required": ["target_language"], "optional": ["source_language"], "files": (1, None),
                  "words": r"translat|hindi|tamil|spanish|german|english"},
    "redact": {"required": [], "optional": ["categories"], "files": (1, None),
               "words": r"redact|mask|hide|sensitive|pan\b|aadhaar|credit card|signature"},
    "compare": {"required": [], "optional": [], "files": (2, 2), "words": r"compar|differences?|what changed|diff\b"},
    "accessibility": {"required": [], "optional": [], "files": (1, None),
                      "words": r"accessib|alt text|screen reader|wcag"},
    "save": {"required": [], "optional": ["name"], "files": (1, None), "words": r"\bsave\b|store|keep a copy"},
}
OTHER_LANGUAGES = ["Japanese", "French", "Arabic", "Chinese", "Mandarin", "Russian", "Portuguese", "Italian", "Korean",
                   "Bengali", "Marathi", "Telugu", "Kannada", "Malayalam", "Gujarati", "Punjabi", "Urdu", "Dutch",
                   "Turkish", "Thai", "Vietnamese", "Indonesian", "Polish", "Swedish", "Greek", "Hebrew"]
EXAMPLES = ["Merge these two files and compress the result.",
            "Remove the blank pages from the contract, then translate it to Hindi.",
            "Summarise the report and list its deadlines as tasks."]


def _error(code: str, message: str, **extra) -> dict:
    return {"status": "error", "steps": [], "error": {"code": code, "message": message, **extra}}


def _files_of(file_ids: list[str]) -> list[dict]:
    out = []
    for fid in file_ids:
        meta, _ = store.get_file(fid)
        out.append({"id": fid, "name": meta["name"], "pages": meta.get("pages")})
    return out


def _planner_prompt(instruction: str, files: list[dict]) -> str:
    ops = "\n".join(f'- "{name}": required settings {spec["required"] or "none"}, optional {spec["optional"] or "none"}, '
                    f'files {spec["files"][0]} to {spec["files"][1] or "any number"}' for name, spec in OPERATIONS.items())
    listed = "\n".join(f'- {f["id"]}  "{f["name"]}"  ({f["pages"]} pages)' for f in files)
    return ("You plan document workflows. Turn the user's instruction into an ordered list of steps. You only plan; "
            "you do not run anything.\n\nAllowed operations (use these names exactly, nothing else):\n" + ops +
            f"\n\nSettings: translate target_language and source_language must be one of {LANGUAGES} and must differ; "
            f"summarise type one of {SUMMARY_TYPES}; split parts is a list of objects {{\"pages\": \"1-3\", \"name\": ...}}; "
            "extract pages is a page range like \"2-4\"; compress level is low, medium or high.\n\n"
            "Each step: \"operation\"; \"files\": the file ids it applies to, written \"f_...\" for a selected file or "
            "\"@N\" for the output of step N (a step that works on the result of an earlier step uses \"@N\"); "
            "\"settings\" object; \"reason\" one short plain sentence.\n\n"
            "Rules: use only the selected files below. If the instruction is not about these operations or you cannot "
            "tell what is wanted, set \"unclear\" true and give no steps. If the instruction names a file that is not "
            "in the list, put its name in \"missing_file\" and give no steps. Never guess a setting the user did not "
            "give: leave it out.\n\nSelected files:\n" + listed + f"\n\nInstruction: {instruction}\n\n"
            "Reply with JSON only: {\"unclear\": bool, \"missing_file\": string or null, \"steps\": [...]}")


_SCHEMA = {"type": "object", "properties": {
    "unclear": {"type": "boolean"}, "missing_file": {"type": ["string", "null"]},
    "steps": {"type": "array", "items": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": list(OPERATIONS)},
        "files": {"type": "array", "items": {"type": "string"}},
        "settings": {"type": "object"}, "reason": {"type": "string"}},
        "required": ["operation", "files", "settings", "reason"]}}},
    "required": ["unclear", "missing_file", "steps"]}


def _ask_model(instruction: str, files: list[dict]) -> dict:
    resp = llm.chat([{"role": "user", "content": _planner_prompt(instruction, files)}], temperature=0, max_tokens=1500,
                    response_format={"type": "json_schema", "json_schema": {"name": "plan", "schema": _SCHEMA}})
    text = (resp["choices"][0]["message"].get("content") or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text).strip()
    return json.loads(text)


def _check_settings(op: str, settings: dict) -> list[str]:
    spec, problems = OPERATIONS[op], []
    for key in spec["required"]:
        if key not in settings or settings[key] in (None, "", []):
            problems.append(f"'{op}' needs the setting '{key}'")
    for key in settings:
        if key not in spec["required"] + spec["optional"]:
            problems.append(f"'{op}' has no setting '{key}'")
    if op == "translate":
        target, source = settings.get("target_language"), settings.get("source_language")
        if target and target not in LANGUAGES:
            problems.append(f"target language '{target}' is not supported; supported: {', '.join(LANGUAGES)}")
        if source and source not in LANGUAGES:
            problems.append(f"source language '{source}' is not supported; supported: {', '.join(LANGUAGES)}")
        if target and source and target == source:
            problems.append("source and target language must differ")
    if op == "summarise" and settings.get("type") not in (None, *SUMMARY_TYPES):
        problems.append(f"summary type must be one of: {', '.join(SUMMARY_TYPES)}")
    if op == "compress" and settings.get("level") not in (None, "low", "medium", "high"):
        problems.append("compress level must be low, medium or high")
    if op == "extract" and not (settings.get("pages") or settings.get("fields")):
        problems.append("'extract' needs pages or fields")
    return problems


def _check_steps(steps: list, selected: set[str]) -> list[str]:
    problems = []
    for i, step in enumerate(steps, 1):
        op = step.get("operation")
        if op not in OPERATIONS:
            problems.append(f"step {i}: '{op}' is not an allowed operation")
            continue
        refs = step.get("files") or []
        for ref in refs:
            if ref.startswith("@"):
                if not ref[1:].isdigit() or not 1 <= int(ref[1:]) < i:
                    problems.append(f"step {i}: '{ref}' must point to an earlier step")
            elif ref not in selected:
                problems.append(f"step {i}: file '{ref}' was not selected")
        low, high = OPERATIONS[op]["files"]
        if len(refs) < low or (high is not None and len(refs) > high):
            problems.append(f"step {i}: '{op}' needs " + (f"{low}" if high == low else f"at least {low}" if high is None
                                                           else f"{low} to {high}") + " file(s)")
        if not str(step.get("reason") or "").strip():
            problems.append(f"step {i}: no reason given")
        settings = step.get("settings")
        problems += [f"step {i}: {p}" for p in _check_settings(op, settings if isinstance(settings, dict) else {})]
    return problems


def _values(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _values(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _values(v)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        yield str(value)


def _chosen_by_ai(settings: dict, instruction: str) -> list[str]:
    """Settings whose value the user never wrote (the model chose it): a guess, so the user must check it."""
    low = instruction.lower()
    return [key for key, value in settings.items() if any(v.lower() not in low for v in _values(value))]


def plan_workflow(file_ids: list[str], instruction: str, planner=None) -> dict:
    """Plan only. Returns {"status": "plan", "steps": [...]}, or an error / a question, and never runs a step."""
    instruction = (instruction or "").strip()
    if not instruction:
        return _error("instruction_not_understood", "The instruction is empty.", examples=EXAMPLES)
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        return _error("instruction_too_long", f"The instruction is too long; the limit is {MAX_INSTRUCTION_CHARS} "
                      "characters (TBC).", limit=MAX_INSTRUCTION_CHARS)
    if len(file_ids) > MAX_FILES:
        return _error("too_many_files", f"{len(file_ids)} files were selected; the limit is {MAX_FILES} (TBC).",
                      limit=MAX_FILES)
    if not file_ids:
        return {"status": "question", "steps": [], "question": "Which file?", "reason": "No file is selected."}
    files = _files_of(file_ids)
    names = {f["name"].lower() for f in files}
    # A file named in the instruction that was not selected: ask, never guess which file was meant.
    named = {m.lower() for m in re.findall(r"[\w\-. ]+?\.(?:pdf|docx?|xlsx?)\b", instruction, re.I)}
    named = {n.strip() for n in named}
    stray = sorted(n for n in named if not any(n == x or n.endswith(x) or x.endswith(n) for x in names))
    if stray:
        return {"status": "question", "steps": [], "question": "Which file?",
                "reason": f"The instruction mentions {', '.join(stray)}, which is not among the selected files."}

    if re.search(r"translat", instruction, re.I):
        other = [x for x in OTHER_LANGUAGES if re.search(rf"\b{x}\b", instruction, re.I)]
        if other:
            return _error("unsupported_language", f"{', '.join(other)} is not supported for translation. No steps were "
                          f"planned. Supported languages: {', '.join(LANGUAGES)}.", supported=LANGUAGES)

    ask = planner or _ask_model
    selected = {f["id"] for f in files}
    problems: list[str] = []
    for attempt in range(2):   # one repair try, told exactly what was wrong
        note = instruction if not problems else instruction + "\n\nYour last answer had these problems, fix them: " + \
            "; ".join(problems)
        raw = ask(note, files)
        if raw.get("missing_file"):
            return {"status": "question", "steps": [], "question": "Which file?",
                    "reason": f"The instruction mentions {raw['missing_file']}, which is not among the selected files."}
        if raw.get("unclear") or not raw.get("steps"):
            return _error("instruction_not_understood", "I could not tell which steps you want. No steps were planned.",
                          examples=EXAMPLES)
        problems = _check_steps(raw["steps"], selected)
        if not problems:
            break
    if problems:
        return _error("plan_not_valid", "The steps could not be made valid, so no plan is returned.", problems=problems)

    steps, lowest = [], 100
    for i, step in enumerate(raw["steps"], 1):
        op = step["operation"]
        pointed = bool(re.search(OPERATIONS[op]["words"], instruction, re.I))
        guessed = _chosen_by_ai(step.get("settings") or {}, instruction)
        # Asked for by name and every setting given by the user: 95. Inferred step or a setting the AI chose: 70, check.
        c = rules.confidence(95 if pointed and not guessed else 70)
        lowest = min(lowest, c["confidence"])
        steps.append({"step": i, "operation": op, "files": step["files"], "settings": step.get("settings") or {},
                      "reason": step["reason"].strip(), **c,
                      **({"chosen_by_ai": guessed} if guessed else {})})
    return {"status": "plan", "instruction": instruction, "steps": steps, **rules.confidence(lowest),
            "runs_nothing": "These steps are a suggestion. Nothing has been run; a person confirms first."}
