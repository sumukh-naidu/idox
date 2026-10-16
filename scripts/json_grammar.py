"""json_grammar.py -- GBNF grammar from a pydantic JSON schema, in a LIGHT compact layout.

One space after ':' and ',', no newlines, no indentation. Used by blocks._extract_page_raw_server when the
environment variable IDOX_LIGHT_JSON=1 is set: the model writes the same JSON with fewer whitespace tokens
(measured 5-11% faster wall time on 5 routes, WORD accuracy unchanged). CAUTION: on a cold server it changed the
block structure of hi.png (two lines returned as one block plus an empty block, 5 of 5 cold runs; the word-level
gate cannot see this), so verify block counts against the normal request before enabling it. Zero-whitespace JSON
was tried and REJECTED: it broke table cells.
"""
import json, re

def _lit(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

def schema_to_gbnf(schema, colon=": ", comma=", "):
    """GBNF for the subset of JSON Schema that pydantic emits for the Page / PageNoLook models."""
    defs = schema.get("$defs", {})
    K, C = _lit(colon), _lit(comma)
    rules, used = {}, set()

    def rname(ref):
        return "r-" + re.sub(r"[^a-zA-Z0-9]+", "-", ref.split("/")[-1]).lower()

    def gen(s):
        if "$ref" in s:
            n = rname(s["$ref"])
            if n not in rules:
                rules[n] = None
                rules[n] = gen(defs[s["$ref"].split("/")[-1]])
            return n
        if "anyOf" in s:
            return "(" + " | ".join(gen(x) for x in s["anyOf"]) + ")"
        if "const" in s:
            return _lit(json.dumps(s["const"]))
        if "enum" in s:
            return "(" + " | ".join(_lit(json.dumps(v)) for v in s["enum"]) + ")"
        t = s.get("type")
        if t in ("string", "integer", "boolean"):
            used.add(t); return t
        if t == "array":
            item = gen(s["items"]); mx = s.get("maxItems")
            rep = f"{{0,{mx - 1}}}" if mx else "*"
            return f'("[" ({item} ({C} {item}){rep})? "]")'
        if t == "object":
            parts = [f'{_lit(json.dumps(k))} {K} {gen(v)}' for k, v in s["properties"].items()]
            return '("{" ' + f' {C} '.join(parts) + ' "}")'
        raise ValueError(f"unsupported schema node: {s}")

    root = gen(schema)
    prim = {
        "string": r'string ::= "\"" ( [^"\\\x7f\x00-\x1f] | "\\" ( ["\\/bfnrt] | "u" [0-9a-fA-F]{4} ) )* "\""',
        "integer": r'integer ::= "-"? ( [0-9] | [1-9] [0-9]{0,15} )',
        "boolean": r'boolean ::= "true" | "false"',
    }
    lines = [f"root ::= {root}"] + [f"{n} ::= {b}" for n, b in rules.items()] + [prim[t] for t in sorted(used)]
    return "\n".join(lines) + "\n"
