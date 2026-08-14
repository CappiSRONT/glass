"""
Glass UI Language (.glass files) - v2
=====================================
Declarative language for browser UI: menus, holders, panels, bars.

GRAMMAR (informal)
------------------
    document   := rule*
    rule       := dotted params? '{' body '}'
    dotted     := IDENT ('.' IDENT)*        # top-level: last seg=component, rest=scope
                                            # inside body: last seg = element kind
    params     := '(' item (sep item)* ')'
    item       := dotted (':' value)?       # flag trait  OR  key:value param
    body       := entry*
    entry      := property | container | button | textgroup | scale | element
    property   := IDENT ':' value
    container  := ('holder'|'menu'|'panel'|'bar') params? '{' body '}'
    button     := 'button' ( STRING block?                      # old form
                           | '{' propblock? STRING '}' block )  # new customizable form
    textgroup  := 'textgroup' '{' arg (',' arg)* '}'            # arg0=name, arg1=fonttype
    scale      := ('scale'|'*.scale') '{' num (',' num)* '}'
    element    := IDENT STRING? block?                          # text/link/label/input/separator
    block      := '{' body '}'
    value      := STRING | <until , } ) or newline>

Comments: >> comment <<   (can span multiple lines)

Everything from v1 still parses. Your line is still valid:

    websitename.menu ( menu.moveable ) {}
"""

from __future__ import annotations


class DSLError(Exception):
    pass


CONTAINERS = ("holder", "menu", "panel", "bar", "main")
TYPES = ("int", "float", "double", "bool", "boolean", "string", "list")
MODE_WORDS = {"full": "full", "fullscreen": "full", "dynamic": "full",
              "ui": "ui", "small": "ui", "draggable": "ui"}
_IDENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_*-")


import re as _re


def _tokenize_cond(expr):
    pat = r'\s*(==|!=|>=|<=|&&|\|\||[<>!()]|"[^"]*"|[A-Za-z_][\w.\-]*|-?\d+\.?\d*)'
    out = []
    i = 0
    s = expr
    while i < len(s):
        m = _re.match(pat, s[i:])
        if not m:
            break
        tok = m.group(1)
        out.append(tok)
        i += m.end()
    return out


class _CondParser:
    def __init__(self, toks, scope):
        self.t = toks
        self.i = 0
        self.scope = scope

    def _peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def _next(self):
        tok = self._peek(); self.i += 1; return tok

    def parse(self):
        return self._or()

    def _or(self):
        v = self._and()
        while self._peek() == "||":
            self._next(); v = self._and() or v
        return v

    def _and(self):
        v = self._not()
        while self._peek() == "&&":
            self._next(); r = self._not(); v = bool(v) and bool(r)
        return v

    def _not(self):
        if self._peek() == "!":
            self._next()
            return not self._not()
        return self._cmp()

    def _cmp(self):
        left = self._atom()
        if self._peek() in ("==", "!=", ">", "<", ">=", "<="):
            op = self._next()
            right = self._atom()
            return _compare(left, op, right)
        return left

    def _atom(self):
        tok = self._next()
        if tok == "(":
            v = self._or()
            if self._peek() == ")":
                self._next()
            return v
        if tok is None:
            return False
        low = tok.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if tok.startswith('"'):
            return tok[1:-1]
        try:
            return float(tok)
        except ValueError:
            pass
        return self.scope.get(tok, 0)   # variable lookup


def _compare(a, op, b):
    # numeric compare when both look numeric, else string/bool compare
    try:
        an, bn = float(a), float(b)
        a, b = an, bn
    except (ValueError, TypeError):
        if isinstance(a, bool) or isinstance(b, bool):
            a = bool(a); b = (b in (True, "true", "True", 1, "1")) if not isinstance(b, bool) else b
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    return False


def eval_condition(expr, scope):
    """Evaluate a condition string like 'ShowNews == true' or 'Count > 2'."""
    if expr is None:
        return True
    expr = str(expr).strip()
    if not expr:
        return True
    try:
        toks = _tokenize_cond(expr)
        if not toks:
            return bool(scope.get(expr, False))
        return bool(_CondParser(toks, scope).parse())
    except Exception:
        return bool(scope.get(expr, False))


def eval_number(expr, scope):
    """Evaluate arithmetic (+ - * /, parens, variables, numbers) -> float."""
    expr = str(expr).strip()
    low = expr.lower()
    if low in ("true", "false"):
        return 1.0 if low == "true" else 0.0
    toks = _re.findall(r'\d+\.?\d*|[A-Za-z_]\w*|[-+*/()]', expr)
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def nxt():
        t = peek(); pos[0] += 1; return t

    def num_of(name):
        v = scope.get(name, 0)
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    def atom():
        t = nxt()
        if t == "(":
            v = expr_()
            if peek() == ")":
                nxt()
            return v
        if t is None:
            return 0.0
        try:
            return float(t)
        except ValueError:
            return num_of(t)

    def term():
        v = atom()
        while peek() in ("*", "/"):
            op = nxt(); r = atom()
            v = v * r if op == "*" else (v / r if r else 0.0)
        return v

    def expr_():
        if peek() == "-":
            nxt(); v = -term()
        else:
            v = term()
        while peek() in ("+", "-"):
            op = nxt(); r = term()
            v = v + r if op == "+" else v - r
        return v

    try:
        return expr_()
    except Exception:
        return 0.0


def coerce_value(typ, raw):
    """Turn a declared value into a python value."""
    if raw is None:
        return {"int": 0, "float": 0.0, "double": 0.0,
                "bool": False, "boolean": False, "string": ""}.get(typ, None)
    raw = str(raw).strip()
    if typ in ("bool", "boolean"):
        return raw.lower() in ("true", "1", "yes", "on")
    if typ == "int":
        try:
            return int(float(raw))
        except ValueError:
            return 0
    if typ in ("float", "double"):
        try:
            return float(raw)
        except ValueError:
            return 0.0
    if typ == "string":
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            return raw[1:-1]
        return raw
    if typ == "list":
        return _parse_list_literal(raw)
    return raw


def _parse_list_literal(raw):
    """[]  ->  []   ;   [1, 2, "hi"]  ->  [1, 2, "hi"]"""
    raw = str(raw).strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return []
    inner = raw[1:-1].strip()
    if not inner:
        return []
    items, depth, buf, q = [], 0, "", None
    for ch in inner:
        if q:
            buf += ch
            if ch == q:
                q = None
            continue
        if ch in ('"', "'"):
            q = ch; buf += ch; continue
        if ch in "[{(":
            depth += 1; buf += ch; continue
        if ch in "]})":
            depth -= 1; buf += ch; continue
        if ch == "," and depth == 0:
            items.append(buf); buf = ""; continue
        buf += ch
    if buf.strip():
        items.append(buf)
    out = []
    for part in items:
        part = part.strip()
        if len(part) >= 2 and part[0] in ('"', "'") and part[-1] == part[0]:
            out.append(part[1:-1])
        else:
            try:
                out.append(int(part))
            except ValueError:
                try:
                    out.append(float(part))
                except ValueError:
                    out.append(part)
    return out


class Container:
    """Base for anything that can hold props/children/textgroups/scale."""
    def __init__(self):
        self.traits = []          # flag traits: moveable, resizable, ...
        self.params = {}          # key:value params (size, opacity, outline, ...)
        self.props = {}           # body properties (title, background, ...)
        self.children = []        # child Nodes
        self.textgroups = {}      # name -> fonttype
        self.scale = None         # (sx, sy, sf) or None
        self.mode = None          # 'full' | 'ui' | None  (for main screens)
        self.center = None        # alignment keyword (center/left/topright/...)
        self.grabs = []           # [(holderName, boolref)]
        self.variables = {}       # name -> value (typed)
        self.update_script = ""   # body of update { } blocks (run every frame)
        self.setup_script = ""    # body of setup { } blocks (run once)
        self.effect_script = ""   # body of effect { } blocks - only inside *.postEffect,
                                   # holds postEffects.* configuration calls (see engine.py)

    def has(self, trait):
        return trait in self.traits

    def find_textgroup(self, name):
        if name in self.textgroups:
            return self.textgroups[name]
        return None


class Document(list):
    """The parsed file: a list of Rules, plus script-level globals."""
    def __init__(self, *a):
        super().__init__(*a)
        self.variables = {}
        self.center = None
        self.grabs = []
        self.update_script = ""
        self.setup_script = ""
        self.imports = []         # [(alias, filename.py)] external Python modules
        self.snippets = {}        # name -> {"params": [(type, name)], "body": src}
        self.override_limits = {}  # from #overrideOPLim { ... } - see engine.py

    @property
    def rules(self):
        return list(self)


class Rule(Container):
    """Top-level definition: scope.component ( ... ) { ... }"""
    def __init__(self, scope, component):
        super().__init__()
        self.scope = scope
        self.component = component

    @property
    def name(self):
        return self.component

    def __repr__(self):
        return (f"Rule({self.scope}.{self.component} traits={self.traits} "
                f"params={self.params} props={self.props} tg={self.textgroups} "
                f"scale={self.scale} children={self.children})")


class Node(Container):
    """A child entry: container (holder/menu...), button, text, link, label, etc."""
    def __init__(self, kind, label=None):
        super().__init__()
        self.kind = kind
        self.label = label
        self.bind = None          # for  Name = input {}  -> variable to sync
        self.style = {}           # button-only: color/height/width/textgroup

    @property
    def name(self):
        return self.kind

    def is_container(self):
        return self.kind in CONTAINERS

    def __repr__(self):
        extra = f" style={self.style}" if self.style else ""
        body = ""
        if self.children or self.textgroups or self.scale:
            body = f" tg={self.textgroups} scale={self.scale} children={self.children}"
        return f"Node({self.kind} label={self.label!r} props={self.props}{extra}{body})"


class Parser:
    def __init__(self, text):
        self.s = text
        self.i = 0
        self.n = len(text)

    # ---- char helpers ------------------------------------------------------
    def _eof(self):
        return self.i >= self.n

    def _peek(self):
        return self.s[self.i] if self.i < self.n else ""

    def _ws(self):
        while not self._eof():
            c = self._peek()
            if c in " \t\r\n":
                self.i += 1
            else:
                break

    def _ws_inline(self):
        while not self._eof() and self._peek() in " \t":
            self.i += 1

    def _ident(self):
        start = self.i
        while not self._eof() and self._peek() in _IDENT_CHARS:
            self.i += 1
        if self.i == start:
            raise DSLError(f"Expected name at {self.i}, got {self._peek()!r}")
        return self.s[start:self.i]

    def _string(self):
        self.i += 1  # opening quote
        out = []
        while not self._eof():
            c = self.s[self.i]; self.i += 1
            if c == "\\" and not self._eof():
                e = self.s[self.i]; self.i += 1
                if e == "n":
                    out.append("\n")
                elif e == "t":
                    out.append("\t")
                elif e == "u":
                    hexd = self.s[self.i:self.i + 4]; self.i += 4
                    try:
                        out.append(chr(int(hexd, 16)))
                    except ValueError:
                        out.append("u" + hexd)
                else:
                    out.append(e)
            elif c == '"':
                return "".join(out)
            else:
                out.append(c)
        raise DSLError("Unterminated string")

    def _expect(self, ch):
        self._ws()
        if self._peek() != ch:
            raise DSLError(f"Expected {ch!r} at {self.i}, got {self._peek()!r}")
        self.i += 1

    def _value(self, stops="\n}"):
        """Quoted string, a [ ... ] array (may span lines), or raw text until a
        stop char / unquoted comma."""
        self._ws_inline()
        if self._peek() == '"':
            return self._string()
        if self._peek() == "[":                 # JSON-style array (can span lines)
            start = self.i
            depth = 0
            instr = False
            while not self._eof():
                c = self._peek()
                if instr:
                    if c == '"':
                        instr = False
                    self.i += 1
                    continue
                if c == '"':
                    instr = True
                elif c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                    self.i += 1
                    if depth == 0:
                        break
                    continue
                self.i += 1
            return self.s[start:self.i].strip()
        start = self.i
        while not self._eof() and self._peek() not in stops and self._peek() != ",":
            self.i += 1
        return self.s[start:self.i].strip()

    def _dotted(self):
        parts = [self._ident()]
        while self._peek() == ".":
            self.i += 1
            parts.append(self._ident())
        return parts

    # ---- params: ( flag, key: value, ... ) ---------------------------------
    def _params(self):
        traits, params = [], {}
        self._expect("(")
        self._ws()
        while self._peek() != ")":
            if self._eof():
                raise DSLError("Unterminated ( ... )")
            segs = self._dotted()
            name = segs[-1]
            self._ws_inline()
            if self._peek() == ":":
                self.i += 1
                val = self._value(stops="\n)")
                # tolerate a stray extra colon typo:  center: center:
                if val.endswith(":"):
                    val = val[:-1].rstrip()
                params[name] = val
            else:
                traits.append(name)
            self._ws()
            if self._peek() == ",":
                self.i += 1
                self._ws()
        self.i += 1  # ')'
        return traits, params

    # ---- a { a, b, c } comma list (textgroup / scale) ----------------------
    def _comma_block(self):
        self._expect("{")
        out = []
        self._ws()
        while self._peek() != "}":
            if self._eof():
                raise DSLError("Unterminated { ... }")
            out.append(self._value(stops="\n})"))
            self._ws()
            if self._peek() == ",":
                self.i += 1
                self._ws()
        self.i += 1  # '}'
        return [x for x in out if x != ""]

    # ---- a { key: value, ... } props-only block ----------------------------
    def _props_block(self):
        props = {}
        self._expect("{")
        while True:
            self._ws()
            if self._peek() == "}":
                self.i += 1
                return props
            if self._eof():
                raise DSLError("Unterminated { ... }")
            key = self._ident()
            self._ws_inline()
            if self._peek() == ":":
                self.i += 1
                val = self._value(stops="\n}")
                if val.endswith(":"):          # tolerate stray extra colon typo
                    val = val[:-1].rstrip()
                props[key] = val
            self._ws()
            if self._peek() == ",":
                self.i += 1

    # ---- main body ---------------------------------------------------------
    def parse(self):
        doc = Document()
        self._snippets = {}
        self._ws()
        doc.override_limits = self._parse_override_pragma()
        while not self._eof():
            self._top_entry(doc)
            self._ws()
        doc.snippets = self._snippets
        return doc

    def _parse_override_pragma(self):
        """#overrideOPLim { ramAllocated: g3  useCPU: true  useGPU: true  useARam: true }
        Must be the VERY first thing in the file - before any import, variable,
        or rule ("outside of any code before the *.menu thing"). Returns a
        plain {name: raw_value} dict for engine.py to interpret; {} if the
        pragma isn't present at all. See engine.py's apply_override_limits()
        for what each name actually does and why ramAllocated is a CAP, not a
        real memory reservation."""
        if self._peek() != "#":
            return {}
        self.i += 1
        name = self._ident()
        if name != "overrideOPLim":
            raise DSLError(f"Unknown pragma '#{name}' - only #overrideOPLim is supported")
        self._ws()
        limits = {}
        if self._peek() == "{":
            self.i += 1
            self._ws()
            while not self._eof() and self._peek() != "}":
                key = self._ident()
                self._ws()
                self._expect(":")
                self._ws()
                val = self._string() if self._peek() == '"' else self._ident()
                limits[key] = val
                self._ws()
                while self._peek() == ",":
                    self.i += 1
                    self._ws()
            if self._peek() != "}":
                raise DSLError("Unterminated #overrideOPLim { ... }")
            self.i += 1
            self._ws()
        return limits

    def _parse_snip(self):
        r"""snip "Name" ( type param, ... ) { body }  - a reusable script function.
        Callable from setup/update scripts as Name(args); may `return { .. }`."""
        self._ws()
        name = self._string() if self._peek() == '"' else self._ident()
        self._ws()
        params = []
        if self._peek() == "(":
            self.i += 1
            self._ws()
            while not self._eof() and self._peek() != ")":
                ptype = self._ident()
                self._ws_inline()
                pname = self._ident() if (not self._eof()
                                          and self._peek() in _IDENT_CHARS) else ptype
                params.append((ptype, pname))
                self._ws()
                if self._peek() == ",":
                    self.i += 1
                    self._ws()
            if self._peek() == ")":
                self.i += 1
        self._ws()
        body = self._raw_block() if self._peek() == "{" else ""
        self._snippets[name] = {"params": params, "body": body}

    def _top_entry(self, doc):
        if self._peek() == "#":
            raise DSLError("#overrideOPLim must be the very first thing in the "
                            "file - before any import, variable, or rule")
        segs = self._dotted()
        kind = segs[-1]
        # import alias                      -> loads alias.py from the project folder
        # import alias from "module.py"     -> loads a named file
        if len(segs) == 1 and segs[0] == "import":
            self._ws_inline()
            alias = self._ident()
            filename = alias + ".py"
            save = self.i
            self._ws_inline()
            nxt = self._ident() if (not self._eof() and self._peek() in _IDENT_CHARS) else ""
            if nxt == "from":
                self._ws_inline()
                filename = self._string()
            else:
                self.i = save
            doc.imports.append((alias, filename))
            return
        # script-level globals
        if len(segs) == 1 and segs[0] in TYPES and self._looks_like_varname():
            self._var_decl(doc, segs[0])
            return
        if kind == "center":
            args = self._comma_block()
            doc.center = args[0] if args else "center"
            return
        if kind == "grab":
            args = self._comma_block()
            if args:
                doc.grabs.append((args[0], args[1] if len(args) > 1 else "true"))
            return
        if kind == "update":
            doc.update_script += self._raw_block() + "\n"
            return
        if kind in ("snip", "function", "func"):
            self._parse_snip()
            return
        if kind == "setup":
            doc.setup_script += self._raw_block() + "\n"
            return
        # a top-level engine call like physics.ui.gravity(9.8) runs once at setup
        if segs[0].lower() in ("physics", "physcis", "adjvcr", "input", "time"):
            save = self.i
            self._ws_inline()
            if self._peek() == "(":
                expr = self._paren_expr()
                doc.setup_script += ".".join(segs) + "(" + expr + ")\n"
                return
            self.i = save
        doc.append(self._rule(segs))

    def _looks_like_varname(self):
        save = self.i
        self._ws_inline()
        ok = (not self._eof()) and (self._peek() in _IDENT_CHARS) and self._peek() != "."
        self.i = save
        return ok

    def _var_decl(self, target, typ):
        self._ws_inline()
        name = self._ident()
        val = None
        self._ws_inline()
        if self._peek() == "=":
            self.i += 1
            self._ws_inline()
            if self._peek() == '"':
                val = '"' + self._string() + '"'
            else:
                val = self._value(stops="\n}")
        target.variables[name] = coerce_value(typ, val)

    def _rule(self, parts=None):
        if parts is None:
            parts = self._dotted()
        if len(parts) == 1:
            scope, component = "*", parts[0]
        else:
            scope, component = ".".join(parts[:-1]), parts[-1]
        rule = Rule(scope, component)
        self._ws()
        if self._peek() == "(":
            rule.traits, rule.params = self._params()
        self._expect("{")
        self._fill(rule)
        return rule

    _MAX_NEST_DEPTH = 200

    def _fill(self, target):
        """Depth-tracked wrapper around _fill_body. Verified: '*.main {' * 3000
        used to raise an uncaught RecursionError past every DSLError handler
        in the app; this raises a normal, catchable DSLError instead, well
        before Python's actual stack limit."""
        self._depth = getattr(self, "_depth", 0) + 1
        try:
            if self._depth > self._MAX_NEST_DEPTH:
                raise DSLError(f"Too deeply nested (over {self._MAX_NEST_DEPTH} "
                                f"levels of '{{' ) - this looks like a runaway or "
                                f"corrupted file rather than a real UI/game layout")
            return self._fill_body(target)
        finally:
            self._depth -= 1

    def _fill_body(self, target):
        """Parse a '{ body }' (the opening '{' already consumed) into target."""
        while True:
            self._ws()
            while self._peek() == ",":   # entries may be comma-separated
                self.i += 1
                self._ws()
            if self._eof():
                raise DSLError("Unterminated block")
            if self._peek() == "}":
                self.i += 1
                return
            if self._peek() == "{":          # bare {w,h} -> collider size (vcr.colide)
                nums = self._comma_block()
                target.props["collider"] = ",".join(str(n) for n in nums)
                continue
            segs = self._dotted()
            kind = segs[-1]

            # bound input:  UsersInput = input "..." { }   (also webInput)
            if len(segs) == 1:
                save = self.i
                self._ws_inline()
                if self._peek() == "=":
                    self.i += 1
                    self._ws_inline()
                    el = (self._ident() if (not self._eof()
                          and self._peek() in _IDENT_CHARS) else "")
                    if el in ("input", "webInput"):
                        node = Node(el)
                        node.bind = segs[0]
                        self._ws()
                        if self._peek() == '"':
                            node.label = self._string(); self._ws()
                        if self._peek() == "{":
                            self._expect("{"); self._fill(node)
                        target.children.append(node)
                        continue
                    self.i = save
                else:
                    self.i = save

            if kind == "button":
                target.children.append(self._button())
                continue
            if kind == "textgroup":
                args = self._comma_block()
                if len(args) >= 2:
                    target.textgroups[args[0]] = args[1]
                elif len(args) == 1:
                    target.textgroups[args[0]] = ""
                continue
            if kind == "scale":
                nums = self._comma_block()
                target.scale = tuple(_to_float(x, 1.0) for x in nums)
                continue
            if kind == "center":
                self._ws_inline()
                if self._peek() == ":":
                    self.i += 1
                    target.props["center"] = self._value(stops="\n}")
                else:
                    args = self._comma_block()
                    target.center = args[0] if args else "center"
                continue
            if kind == "grab":
                args = self._comma_block()
                if args:
                    target.grabs.append((args[0], args[1] if len(args) > 1 else "true"))
                continue
            if kind == "if":
                target.children.append(self._ifchain())
                continue
            if kind == "update":
                target.update_script += self._raw_block() + "\n"
                continue
            if kind in ("snip", "function", "func"):
                self._parse_snip()
                continue
            if kind == "setup":
                target.setup_script += self._raw_block() + "\n"
                continue
            if kind == "effect":
                target.effect_script += self._raw_block() + "\n"
                continue
            if len(segs) == 2 and segs[0] == "post" and segs[1] in ("name", "cache", "quality"):
                # post.name = "testing" / post.cache = true / post.quality = 100
                # - simple typed properties on a *.postEffect rule, same idea
                # as ambient: N on a raycast, just written assignment-style
                # to match #overrideOPLim's own post.* naming precedent
                save = self.i
                self._ws_inline()
                if self._peek() == "=":
                    self.i += 1
                    self._ws_inline()
                    if self._peek() == '"':
                        val = '"' + self._string() + '"'
                    else:
                        val = self._value(stops="\n}")
                    target.props["post." + segs[1]] = val
                    continue
                self.i = save
            if kind == "collider":
                nums = self._comma_block()
                target.props["collider"] = ",".join(str(n) for n in nums)
                continue
            if segs[0] in ("vcr", "vrc") and len(segs) >= 2:
                sub = segs[-1]                 # image | gif | video | colide
                node = Node("vcr_" + sub)
                self._ws()
                if self._peek() == '"':
                    node.label = self._string()
                    self._ws()
                if self._peek() == "{":
                    self._expect("{")
                    self._fill(node)
                target.children.append(node)
                continue
            if len(segs) == 1 and segs[0] in TYPES and self._looks_like_varname():
                self._var_decl(target, segs[0])
                continue
            if kind in CONTAINERS:
                target.children.append(self._container(kind))
                continue

            # property or generic element
            self._ws_inline()
            if self._peek() == ":":
                self.i += 1
                target.props[kind] = self._value(stops="\n}")
                self._ws_inline()
                if self._peek() == "{":          # title: "X" { center, color, font }
                    target.props[kind + "_style"] = self._props_block()
                continue

            # mode keyword (menu.full / menu.ui / full / ui), no value/label/block
            if kind in MODE_WORDS:
                save = self.i
                self._ws()
                nxt = self._peek()
                self.i = save
                if nxt not in ('"', "{"):
                    target.mode = MODE_WORDS[kind]
                    if kind == "dynamic" and "dynamic" not in target.traits:
                        target.traits = target.traits + ["dynamic"]
                    continue

            node = Node(kind)
            self._ws()
            if self._peek() == '"':
                node.label = self._string()
                self._ws()
            if self._peek() == "{":
                self._expect("{")
                self._fill(node)
            target.children.append(node)

    def _match_word(self, word):
        save = self.i
        self._ws()
        start = self.i
        if self.s[start:start + len(word)] == word:
            after = start + len(word)
            nxt = self.s[after] if after < self.n else ""
            if nxt not in _IDENT_CHARS:
                self.i = after
                return True
        self.i = save
        return False

    def _paren_expr(self):
        self._ws()
        self._expect("(")
        depth = 1
        start = self.i
        while not self._eof() and depth > 0:
            c = self.s[self.i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            self.i += 1
        expr = self.s[start:self.i].strip()
        self._expect(")")
        return expr

    def _raw_block(self):
        """Consume a balanced { ... } and return the inner text verbatim."""
        self._ws()
        self._expect("{")
        depth = 1
        start = self.i
        while not self._eof() and depth > 0:
            c = self.s[self.i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            self.i += 1
        inner = self.s[start:self.i]
        self._expect("}")
        return inner

    def _ifchain(self):
        node = Node("__ifchain__")
        node.branches = []
        cond = self._paren_expr()
        self._ws(); self._expect("{")
        branch = Container(); self._fill(branch)
        node.branches.append((cond, branch))
        while True:
            if self._match_word("else"):
                if self._match_word("if"):
                    cond = self._paren_expr()
                    self._ws(); self._expect("{")
                    b = Container(); self._fill(b)
                    node.branches.append((cond, b))
                    continue
                self._ws(); self._expect("{")
                b = Container(); self._fill(b)
                node.branches.append((None, b))   # else
                break
            break
        return node

    def _container(self, kind):
        node = Node(kind)
        self._ws()
        if self._peek() == "(":
            node.traits, node.params = self._params()
        self._ws()
        if self._peek() == '"':
            node.label = self._string()
            self._ws()
        self._expect("{")
        self._fill(node)
        return node

    def _button(self):
        node = Node("button")
        self._ws()
        # OLD form:  button "name" { action: x }
        if self._peek() == '"':
            node.label = self._string()
            self._ws()
            if self._peek() == "{":
                node.props = self._props_block()
            return node
        # NEW form:  button { { style } "name" } { action: x }
        if self._peek() == "{":
            self._expect("{")
            self._ws()
            if self._peek() == "{":
                node.style = self._props_block()
                self._ws()
            if self._peek() == '"':
                node.label = self._string()
                self._ws()
            self._expect("}")            # close outer wrapper
            self._ws()
            if self._peek() == "{":
                node.props = self._props_block()  # the action block
            return node
        raise DSLError("button must be followed by \"name\" or { ... }")


def _to_float(x, default):
    try:
        return float(x)
    except (ValueError, TypeError):
        return default


def _strip_comments(text):
    r"""Remove Glass comments delimited by >>  <<  (they can span lines).
    String literals are respected, and newlines inside comments are preserved so
    error line numbers stay accurate."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':                       # copy string literals verbatim
            out.append(c); i += 1
            while i < n:
                out.append(text[i])
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i + 1]); i += 2; continue
                if text[i] == '"':
                    i += 1; break
                i += 1
            continue
        if text[i:i + 2] == ">>":           # comment: ends at << or end of line
            i += 2
            while i < n and text[i] != "\n" and text[i:i + 2] != "<<":
                i += 1
            if i < n and text[i:i + 2] == "<<":
                i += 2                       # consume the closing <<
            out.append(" ")                  # (a newline, if that's what stopped us,
            continue                         #  is left for the outer loop to copy)
        out.append(c); i += 1
    return "".join(out)


def parse(text):
    p = Parser(_strip_comments(text))
    try:
        return p.parse()
    except DSLError as e:
        if not hasattr(e, "pos"):
            e.pos = getattr(p, "i", 0)     # char index where parsing failed
        raise


def parse_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return parse(f.read())


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for r in parse_file(sys.argv[1]):
            print(r)
    else:
        for r in parse("websitename.menu ( menu.moveable ) {}"):
            print(r)
