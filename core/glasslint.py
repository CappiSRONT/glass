"""glasslint - offline analysis of .glass source for the editor's debug log.

analyze(text) -> {"errors": [...], "warnings": [...], "debug": [...]}
Each item: {"line": int, "col": int, "span": (start,end)|None,
            "msg": str, "hint": str}

- errors  : the file won't parse; points at the broken spot + probable cause/fix.
- warnings: parses fine, but something looks wrong or improvable + how to fix.
- debug   : neutral facts about the file (counts, menu type, variables, snips).

Pure logic, no Qt - unit-testable on its own.
"""

import re
import dsl


def _linecol(text, pos):
    pos = max(0, min(pos, len(text)))
    line = text.count("\n", 0, pos) + 1
    col = pos - (text.rfind("\n", 0, pos) + 1) + 1
    return line, col


# (substring in the raw error) -> (probable cause, suggested fix)
_ERROR_HINTS = [
    ("Unterminated string", "A piece of text is missing its closing quote.",
     'Add a closing " to the end of the text.'),
    ("Unterminated ( ", "A ( ... ) group was opened but never closed.",
     "Add the missing ) ."),
    ("Unterminated { ", "A { ... } block was opened but never closed.",
     "Add the missing } - check that every { has a matching }."),
    ("Unterminated block", "A block was opened but never closed.",
     "Add the missing } at the end of the block."),
    ("button must be followed", 'A button needs a "label" or a { ... } body.',
     'Write  button "Play" { action: reload }  (or the styled form).'),
    ("Expected name", "A name (element or property) was expected here.",
     "Look for a stray symbol, or a missing name just before a ( or { ."),
    ("Expected", "A specific character was expected but something else was found.",
     "Check the brackets and quotes right around this spot."),
]

# bare calls that are engine builtins (not user snips)
_BUILTIN_CALLS = {"adjvcr", "create", "burst", "random", "rand", "lerp", "clamp", "min", "max", "abs", "sqrt", "sin", "cos", "floor", "round", "createMesh", "createmesh", "destroy", "spawnobject", "cast", "exists", "clone", "create", "lerp", "slerp", "lerpangle", "clamp", "alert"}
_CONTROL = {"if", "else", "return", "setup", "update", "snip", "repeat", "for", "to"}


def _scripts(text, doc):
    """All script text (setup/update/snip bodies) for call/assignment scanning."""
    chunks = [getattr(doc, "setup_script", "") or "", getattr(doc, "update_script", "") or ""]
    for r in doc:
        chunks.append(getattr(r, "setup_script", "") or "")
        chunks.append(getattr(r, "update_script", "") or "")
        for ch in (getattr(r, "children", None) or []):
            chunks.append(getattr(ch, "setup_script", "") or "")
            chunks.append(getattr(ch, "update_script", "") or "")
    for snip in (getattr(doc, "snippets", {}) or {}).values():
        chunks.append(snip.get("body", "") or "")
    return "\n".join(chunks)


def analyze(text):
    errors, warnings, debug = [], [], []
    text = text or ""

    # ---------- ERRORS: does it parse? --------------------------------------
    doc = None
    try:
        doc = dsl.parse(text)
    except dsl.DSLError as e:
        pos = getattr(e, "pos", 0)
        line, col = _linecol(text, pos)
        raw = str(e)
        cause, fix = "Syntax error.", "Check the highlighted line."
        for sub, c, f in _ERROR_HINTS:
            if sub in raw:
                cause, fix = c, f
                break
        errors.append({"line": line, "col": col, "span": (pos, pos + 1),
                       "msg": cause, "hint": fix})
        return {"errors": errors, "warnings": warnings, "debug": debug}
    except Exception as e:                       # never crash the editor
        errors.append({"line": 1, "col": 1, "span": None,
                       "msg": "Couldn't parse this file.", "hint": str(e)[:120]})
        return {"errors": errors, "warnings": warnings, "debug": debug}

    lines = text.split("\n")
    # comment-stripped view (same line numbers) for text checks, so we never flag
    # things that live inside a >> comment <<
    try:
        clean_lines = dsl._strip_comments(text).split("\n")
    except Exception:
        clean_lines = lines

    def add_w(lineno, msg, hint):
        warnings.append({"line": lineno, "col": 1, "span": None, "msg": msg, "hint": hint})

    # ---------- WARNINGS (line/text based) ----------------------------------
    for i, ln in enumerate(clean_lines, 1):
        s = ln
        instr = False
        for j in range(len(s) - 1):
            if s[j] == '"':
                instr = not instr
            elif not instr and s[j:j + 2] == "//" and (j == 0 or s[j - 1] != ":"):
                add_w(i, "'//' is no longer a comment.",
                      "Use  >> comment <<  instead.")
                break
        if re.search(r":\s*[A-Za-z0-9_#.]+\s*:\s*(,|\)|\}|$)", s):
            add_w(i, "A value looks like it ends with a stray ':'.",
                  "Remove the extra colon, e.g.  center: center  (not  center: center:).")
        m = re.search(r'\b(int|float|double|number)\s+\w+\s*=\s*"', s)
        if m:
            add_w(i, f"A {m.group(1)} variable is being set to a text value in quotes.",
                  f'Drop the quotes for a number, e.g.  {m.group(1)} X = 0  (not "0").')

    # ---------- WARNINGS (structure based) ----------------------------------
    declared = set((getattr(doc, "variables", {}) or {}).keys())
    snips = set((getattr(doc, "snippets", {}) or {}).keys())
    script_text = _scripts(text, doc)
    assigned = set(re.findall(r"(?<![.\w])([A-Za-z_]\w*)\s*(?:=|\+=|-=|\*=|/=)", script_text))
    # engine-provided runtime variables (set by widgets, not declared in source)
    _ENGINE_VARS = {"rayX", "rayY", "rayA", "hitSvc", "hitDist", "hitX", "hitY", "hitType", "dUMR"}
    known_names = declared | assigned | snips | _ENGINE_VARS

    # {Var} interpolations that were never declared/assigned
    seen_missing = set()
    for i, ln in enumerate(lines, 1):
        for var in re.findall(r"\{([A-Za-z_]\w*)\}", ln):
            if var not in known_names and var not in seen_missing:
                seen_missing.add(var)
                add_w(i, f"'{{{var}}}' is used but '{var}' is never declared or set.",
                      f"Declare it (e.g.  string {var} = \"\") or set it in a script.")

    # snip usage
    called = set(re.findall(r"(?<![.\w])([A-Za-z_]\w*)\s*\(", script_text))
    for name in sorted(snips):
        if name not in called:
            add_w(1, f"snip '{name}' is defined but never called.",
                  f"Call it from a script as  {name}(...)  - or remove it.")
    for name in sorted(called):
        if (name.lower() not in _BUILTIN_CALLS and name not in snips
                and name not in _CONTROL and name[0].isalpha()):
            add_w(1, f"'{name}(...)' is called but no snip named '{name}' exists.",
                  f"Define  snip \"{name}\" ( ) {{ ... }}  or fix the name.")

    # ---------- DEBUG (neutral facts) ---------------------------------------
    def add_d(msg):
        debug.append({"line": 0, "col": 0, "span": None, "msg": msg, "hint": ""})

    modes = []
    total_children = 0
    for r in doc:
        modes.append(getattr(r, "scope", "?"))
        total_children += len(getattr(r, "children", None) or [])
    add_d(f"Parsed OK: {len(list(doc))} screen(s), {total_children} top-level element(s).")
    if declared:
        add_d("Variables: " + ", ".join(sorted(declared)))
    if snips:
        add_d("Snips: " + ", ".join(sorted(snips)))
    imports = getattr(doc, "imports", []) or []
    if imports:
        add_d("Imports: " + ", ".join(a for a, _ in imports))
    if getattr(doc, "update_script", "") or any(getattr(r, "update_script", "") for r in doc):
        add_d("Has an update{} loop (runs every frame).")

    return {"errors": errors, "warnings": warnings, "debug": debug}
