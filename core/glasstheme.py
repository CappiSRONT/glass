"""glasstheme - VS-style editor colour themes for Glass.

A .glasstheme file is just JSON mapping roles -> hex colours. Themes recolour the
editor background/gutter AND the syntax highlighting (keywords, types, elements,
functions, strings, numbers, comments, etc). Built-ins are provided, users can
import a .glasstheme, or build one in the theme creator.
"""

import os
import json

import atomicio

HERE = os.path.dirname(os.path.abspath(__file__))
THEME_DIR = os.path.join(HERE, "themes")          # imported / user-made themes

# every role a theme can set (missing keys fall back to DEFAULT)
ROLES = [
    ("background", "Editor background"),
    ("foreground", "Default text"),
    ("gutterBg", "Line-number background"),
    ("gutterFg", "Line-number text"),
    ("currentLine", "Current line"),
    ("selection", "Selection"),
    ("keyword", "Keywords (setup, if, snip, return\u2026)"),
    ("type", "Types (int, string, bool\u2026)"),
    ("boolean", "Booleans (true / false)"),
    ("element", "Elements (button, holder, text\u2026)"),
    ("function", "Engine functions (input, adjVCR, audio\u2026)"),
    ("snipname", "Snip names"),
    ("property", "Property names (before ':')"),
    ("operator", "Operators (+ - * / == && \u2026)"),
    ("string", "Strings"),
    ("number", "Numbers"),
    ("hexcolor", "Hex colours (#rrggbb)"),
    ("comment", "Comments  >> \u2026 <<"),
    ("punctuation", "Punctuation  { } ( ) . ,"),
]

DEFAULT = {
    "name": "Frosted Dark",
    "background": "#0d1117", "foreground": "#d7e0ea",
    "gutterBg": "#0a0e13", "gutterFg": "#3a4653",
    "currentLine": "#161d27", "selection": "#2a3a4d",
    "keyword": "#c792ea", "type": "#82aaff", "element": "#c792ea",
    "function": "#f78c6c", "property": "#7fdbca", "string": "#c3e88d",
    "number": "#f78c6c", "hexcolor": "#ffcb6b", "comment": "#5f7383",
    "punctuation": "#8aa0b2",
    "boolean": "#e5c07b", "operator": "#89ddff", "snipname": "#e0a0ff",
}

_BUILTINS = {
    "Frosted Dark": DEFAULT,
    "Midnight": {
        "name": "Midnight",
        "background": "#0a0c12", "foreground": "#c8d3e0",
        "gutterBg": "#070910", "gutterFg": "#39435a",
        "currentLine": "#121826", "selection": "#243149",
        "keyword": "#7aa2f7", "type": "#2ac3de", "element": "#bb9af7",
        "function": "#7dcfff", "property": "#9ece6a", "string": "#e0af68",
        "number": "#ff9e64", "hexcolor": "#ff9e64", "comment": "#565f89",
        "punctuation": "#6b7594",
    },
    "Ember Dark": {
        "name": "Ember Dark",
        "background": "#14100e", "foreground": "#e8ddd4",
        "gutterBg": "#0e0b09", "gutterFg": "#4a3f38",
        "currentLine": "#1f1814", "selection": "#3a2a1f",
        "keyword": "#ff8f6b", "type": "#e0c56e", "element": "#ff8f6b",
        "function": "#f0a35e", "property": "#9ecf97", "string": "#c3e88d",
        "number": "#f0a35e", "hexcolor": "#ffcb6b", "comment": "#6b5d53",
        "punctuation": "#8a7a6d",
    },
    "Paper Light": {
        "name": "Paper Light",
        "background": "#f6f7f9", "foreground": "#1c2530",
        "gutterBg": "#eceef1", "gutterFg": "#9aa4b0",
        "currentLine": "#e9edf2", "selection": "#cfe0ff",
        "keyword": "#8a3ffc", "type": "#0b69c7", "element": "#8a3ffc",
        "function": "#c05314", "property": "#0f7a6c", "string": "#2c8a2c",
        "number": "#c05314", "hexcolor": "#b06a00", "comment": "#8a95a1",
        "punctuation": "#5a6673",
    },
}


def _hex_to_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def shade(hexc, factor):
    """Lighten (factor>0) or darken (factor<0) a hex colour, factor in -1..1."""
    try:
        r, g, b = _hex_to_rgb(hexc)
    except Exception:
        return hexc
    if factor >= 0:
        r += (255 - r) * factor; g += (255 - g) * factor; b += (255 - b) * factor
    else:
        f = 1 + factor
        r *= f; g *= f; b *= f
    return "#%02x%02x%02x" % (max(0, min(255, int(r))),
                              max(0, min(255, int(g))),
                              max(0, min(255, int(b))))


def is_light(hexc):
    """True if `hexc` reads as a light colour, using real perceptual luminance
    (all three channels) rather than a single channel - a background like a
    dark maroon (#8a0505, red-heavy but still dark) is correctly seen as dark."""
    try:
        r, g, b = _hex_to_rgb(hexc)
    except Exception:
        return False
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return luminance > 140


def builtin_names():
    return list(_BUILTINS.keys())


_INVALID_NAME_CHARS = '<>:"/\\|?*\x00'


def sanitize_name(name):
    """Make a theme name safe to use as a filename on Windows/Mac/Linux alike.
    Strips characters that are illegal (or awkward) in filenames so a theme
    name typed in the UI can never produce a broken/invalid save path."""
    name = (name or "").strip()
    cleaned = "".join(c for c in name if c not in _INVALID_NAME_CHARS and ord(c) >= 32)
    cleaned = cleaned.strip(" .")          # Windows disallows trailing dot/space
    return cleaned or "My Theme"


def name_collides_with_builtin(name):
    """True if `name` matches a built-in theme name (case-insensitive)."""
    low = (name or "").strip().lower()
    return any(low == b.lower() for b in _BUILTINS)


def delete_file(path):
    """Remove a saved .glasstheme file. Returns True on success (or if the
    file was already gone); False if it exists but couldn't be removed."""
    try:
        if path and os.path.isfile(path):
            os.remove(path)
        return True
    except OSError:
        return False


def _norm(theme):
    """Fill any missing roles from DEFAULT."""
    out = dict(DEFAULT)
    out.update({k: v for k, v in (theme or {}).items() if v})
    return out


def load_file(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "name" not in data:
        data["name"] = os.path.splitext(os.path.basename(path))[0]
    return _norm(data)


def save_file(path, theme):
    if not path.endswith(".glasstheme"):
        path += ".glasstheme"
    atomicio.write_json(path, _norm(theme), indent=2)
    return path


def user_themes():
    """List .glasstheme files in the themes folder -> {name: path}."""
    out = {}
    if os.path.isdir(THEME_DIR):
        for fn in sorted(os.listdir(THEME_DIR)):
            if fn.endswith(".glasstheme"):
                try:
                    t = load_file(os.path.join(THEME_DIR, fn))
                    out[t.get("name", fn)] = os.path.join(THEME_DIR, fn)
                except Exception:
                    pass
    return out


def resolve(ref):
    """A theme name (built-in or user) or a file path -> a full theme dict."""
    if not ref:
        return dict(DEFAULT)
    if ref in _BUILTINS:
        return _norm(_BUILTINS[ref])
    ut = user_themes()
    if ref in ut:
        try:
            return load_file(ut[ref])
        except Exception:
            return dict(DEFAULT)
    if os.path.isfile(ref):
        try:
            return load_file(ref)
        except Exception:
            return dict(DEFAULT)
    return dict(DEFAULT)


def active():
    try:
        import prefs
        return resolve(prefs.load("editor_theme", "Frosted Dark"))
    except Exception:
        return dict(DEFAULT)


def set_active(ref):
    try:
        import prefs
        prefs.save("editor_theme", ref)
    except Exception:
        pass
