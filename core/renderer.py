"""
Glass renderer (v2)
===================
Turns parsed rules/nodes into real widgets:
  - floating menus/panels/bars (GlassPanel)
  - inline, toggleable holders + nested menus (HolderFrame)
  - customizable buttons, text, links, labels, inputs, separators
  - textgroups (fonts), scale, and a registry of named holders for toggling.
"""

from __future__ import annotations

import os
import copy
import re

import dsl
import theme

try:
    import numpy as _np           # optional - accelerates bulk pixel work (lightmap
    _HAS_NUMPY = True             # baking especially) when available. Never required:
except ImportError:               # every numpy-accelerated path has a pure-Python
    _np = None                    # fallback, so this stays a soft dependency.
    _HAS_NUMPY = False

from PyQt6.QtCore import Qt, QTimer, QEvent, QSize
from PyQt6.QtGui import QFont, QPixmap, QImage, QTransform, QMovie
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton, QLabel,
    QLineEdit, QSizeGrip,
)

# directories searched for background images referenced by `image:`
ASSET_DIRS = []

# live window size, updated by the browser/editor; screen.width / screen.height
# in .glass values resolve to these. Widgets sized with them re-stretch on resize.
SCREEN = [1280, 800]
_SCREEN_BINDINGS = []          # list of (widget, apply_fn) re-applied on resize
_VAR_BINDINGS = []             # list of (widget, template) re-applied when vars change


def refresh_var_bindings(variables):
    """Re-evaluate {Var} text on bound widgets. Only writes when the value
    actually changed, so unchanged text never triggers a relayout (this is what
    caused per-frame stutter when a holder was on screen)."""
    if not _VAR_BINDINGS:
        return
    vars_ = variables or {}
    alive = []
    for widget, template in _VAR_BINDINGS:
        try:
            val = _interp(template, vars_)
            if getattr(widget, "_glass_last_text", None) != val:
                widget.setText(val)
                widget._glass_last_text = val
            alive.append((widget, template))
        except RuntimeError:
            pass               # widget was deleted
    _VAR_BINDINGS[:] = alive


def _track_text(widget, template):
    """Remember a widget whose text uses {Var} so it can update live."""
    if template and "{" in template:
        _VAR_BINDINGS.append((widget, template))


_MEDIA_BINDINGS = []   # [widget, source_template, {"last": str}] - live video/image src


def _track_media(widget, template):
    """Remember a vcr widget whose SOURCE uses {Var} (e.g. vcr.video "{clip}")
    so the media reloads live when the variable changes."""
    if template and "{" in template and widget is not None:
        _MEDIA_BINDINGS.append([widget, template, {"last": None}])


def refresh_media_bindings(variables):
    """Re-resolve {Var} media sources; reload the player/image only when the
    resolved source actually changes (so it's cheap to call every frame)."""
    if not _MEDIA_BINDINGS:
        return
    vars_ = variables or {}
    alive = []
    for entry in _MEDIA_BINDINGS:
        widget, template, st = entry
        try:
            val = _interp(template, vars_)
        except Exception:
            val = ""
        if val != st["last"]:
            st["last"] = val
            path = _resolve_asset(val) if val else ""
            fn = getattr(widget, "_reload_media", None)
            if fn is not None:
                try:
                    fn(path)
                except RuntimeError:
                    continue        # widget deleted
                except Exception:
                    pass
        try:
            widget.isVisible()      # liveness probe
            alive.append(entry)
        except RuntimeError:
            pass
    _MEDIA_BINDINGS[:] = alive


def set_screen(w, h):
    """Update the live window size and re-stretch any screen-bound widgets."""
    SCREEN[0] = int(w)
    SCREEN[1] = int(h)
    try:
        import engine
        engine.SCREEN[0] = int(w)
        engine.SCREEN[1] = int(h)
    except Exception:
        pass
    alive = []
    for widget, fn in _SCREEN_BINDINGS:
        try:
            fn(widget)
            alive.append((widget, fn))
        except RuntimeError:
            pass               # widget was deleted
    _SCREEN_BINDINGS[:] = alive


def _track(widget, raw, apply_fn):
    """Apply a size now, and remember it for resize if it used screen.*"""
    apply_fn(widget)
    if raw and "screen." in str(raw).lower():
        _SCREEN_BINDINGS.append((widget, apply_fn))


def _safe_arith(expr):
    """Evaluate a +,-,*,/,() arithmetic expression WITHOUT eval(). The old
    implementation gated eval() with a regex that blocked letters/underscores
    (so no attribute-chaining sandbox escape was possible), but still let
    '**' (power) through - e.g. size: 9**9**9**9 - which eval() would happily
    hang on. Since .glass files can arrive from another PC via the LAN-share
    feature, that was a real DoS path. This tiny recursive-descent evaluator
    has no exponentiation operator at all, so there's nothing to abuse."""
    pos = 0
    n = len(expr)

    def peek():
        return expr[pos] if pos < n else ""

    def skip_ws():
        nonlocal pos
        while pos < n and expr[pos] == " ":
            pos += 1

    def parse_expr():
        nonlocal pos
        val = parse_term()
        while True:
            skip_ws()
            if peek() == "+":
                pos += 1; val += parse_term()
            elif peek() == "-":
                pos += 1; val -= parse_term()
            else:
                return val

    def parse_term():
        nonlocal pos
        val = parse_factor()
        while True:
            skip_ws()
            if peek() == "*":
                pos += 1; val *= parse_factor()
            elif peek() == "/":
                pos += 1
                d = parse_factor()
                val /= d                     # ZeroDivisionError bubbles up -> caller falls back to raw text
            else:
                return val

    def parse_factor():
        nonlocal pos
        skip_ws()
        neg = False
        while peek() in ("+", "-"):
            neg ^= (peek() == "-")
            pos += 1
            skip_ws()
        if peek() == "(":
            pos += 1
            val = parse_expr()
            skip_ws()
            if peek() != ")":
                raise ValueError("unbalanced parens")
            pos += 1
        else:
            start = pos
            while pos < n and (expr[pos].isdigit() or expr[pos] == "."):
                pos += 1
            if start == pos:
                raise ValueError("expected a number")
            val = float(expr[start:pos])
        return -val if neg else val

    skip_ws()
    result = parse_expr()
    skip_ws()
    if pos != n:
        raise ValueError("trailing characters")
    return result


def _resolve_expr(s):
    """Resolve screen.width / screen.height and simple +-*/ arithmetic."""
    if s is None:
        return s
    t = str(s).strip()
    if not t:
        return t
    low = t.lower()
    if "screen." in low:
        low = low.replace("screen.width", str(SCREEN[0]))
        low = low.replace("screen.height", str(SCREEN[1]))
        t = low
    if re.fullmatch(r"[0-9.+\-*/() ]+", t):     # pure numeric expression -> evaluate
        try:
            v = _safe_arith(t)
            return str(int(v)) if v == int(v) else str(v)
        except Exception:
            return t
    return t


def _size_tokens(size):
    """Split a size value into (width_token, height_token) raw strings."""
    if not size:
        return (None, None)
    s = str(size).strip()
    if s.lower() in ("", "auto"):
        return (None, None)
    if "x" in s.lower():
        a, _, b = s.lower().partition("x")
        return (a.strip(), b.strip())
    return (s, None)


def _set_fixed_w(wd, tok, scale):
    v = _to_int(tok)
    if v > 0:
        wd.setFixedWidth(int(v * scale))


def _set_fixed_h(wd, tok, scale):
    v = _to_int(tok)
    if v > 0:
        wd.setFixedHeight(int(v * scale))


def _set_min_and_w(wd, wtok, htok, sx, sy):
    w = _to_int(wtok); h = _to_int(htok)
    if w and h:
        wd.setMinimumSize(int(w * sx), int(h * sy))
    if w:
        wd.setFixedWidth(int(w * sx))
# the engine World that vcr.* elements register into for the current render
ACTIVE_WORLD = None
SCENE_VIEW = False          # editor sets this True to draw raycasters top-down (2D)
_RENDER_DYNAMIC = False    # True only while rendering a menu.dynamic (game) screen

# Baked-lighting quality (the editor's Lighting tab sets these).
LIGHT_QUALITY = "medium"   # draft / low / medium / high / ultra / extreme
LIGHT_SHADOWS = True       # master shadow switch (per-light shadowCaster refines it)
_LIGHT_QUALITY_TABLE = {
    # tile: floor/roof atlas tile size in px. softness: the blur-pass factor
    # _bake_lightmap actually reads (1 = off/crisp, higher = smoother
    # penumbra) - matches each tier's own description below instead of a
    # dead 'smooth' bool that was never read anywhere.
    "draft":   {"tile": 8,  "softness": 1},
    "low":     {"tile": 12, "softness": 1},
    "medium":  {"tile": 24, "softness": 2},
    "high":    {"tile": 40, "softness": 2},
    "ultra":   {"tile": 64, "softness": 2},
    "extreme": {"tile": 96, "softness": 2},
}
_LIGHT_QUALITY_ORDER = ["draft", "low", "medium", "high", "ultra", "extreme"]

# Shadow / lightmap resolution - raycasted per pixel, so shadows are pixel-perfect
# rather than tile-based. The live 3D view is capped for responsiveness; explicit
# bakes (Bake button / lightmap.generate) use the full resolution, up to 4K.
SHADOW_RESOLUTION = 1024        # 512 / 1024 / 2048 / 4096
_LIVE_SHADOW_CAP = 768          # live render never bakes bigger than this (stays snappy)
RENDER_INTERVAL_MS = 0          # 0 = fire again as soon as the event loop is idle,
                                 # i.e. no artificial fps ceiling - real per-frame
                                 # cost is the only limiter (was 16, a hard 60fps cap
                                 # regardless of how fast a machine actually was)

# Baked lightmaps are cached by their lighting signature so repeated live re-renders
# (e.g. while typing) reuse the bake instead of recomputing it every time.
_LIGHTMAP_CACHE = {}
_LIGHTMAP_ORDER = []


def _lightmap_cache_get(key):
    return _LIGHTMAP_CACHE.get(key)


def _lightmap_cache_put(key, img):
    _LIGHTMAP_CACHE[key] = img
    _LIGHTMAP_ORDER.append(key)
    while len(_LIGHTMAP_ORDER) > 10:                # cap memory: keep the last 10
        old = _LIGHTMAP_ORDER.pop(0)
        _LIGHTMAP_CACHE.pop(old, None)


def _light_quality():
    return _LIGHT_QUALITY_TABLE.get(str(LIGHT_QUALITY).lower(),
                                    _LIGHT_QUALITY_TABLE["medium"])


def _qcolor(s, default="#ffffff"):
    """Parse a colour, including #RRGGBBAA (alpha last, CSS-style) for transparency.
    Qt's own 8-digit form is #AARRGGBB, so we handle alpha-last ourselves."""
    from PyQt6.QtGui import QColor
    if s is None:
        return QColor(default)
    s = str(s).strip()
    if s.startswith("#") and len(s) == 9:
        try:
            return QColor(int(s[1:3], 16), int(s[3:5], 16),
                          int(s[5:7], 16), int(s[7:9], 16))
        except ValueError:
            pass
    c = QColor(s)
    return c if c.isValid() else QColor(default)


def _avg_material_color(mat):
    """A material's average colour as (r,g,b) 0..1 - its flat color: if
    set, else a cheap downscale-to-1x1 average of its texture (a fast,
    genuine average, not a guess - scaling to 1 pixel with smooth
    interpolation IS the average). None if there's no material at all.
    Cached on the material dict itself, so this runs once per material
    total, not once per light or per bake - used for the bounce-tint
    approximation below, not for anything visual."""
    if mat is None:
        return None
    cached = mat.get("_avg_rgb")
    if cached is not None:
        return cached
    rgb = (1.0, 1.0, 1.0)
    if mat.get("color") is not None:
        c = mat["color"]
        rgb = (c.red() / 255.0, c.green() / 255.0, c.blue() / 255.0)
    elif mat.get("pix") is not None:
        from PyQt6.QtCore import Qt as _Qt
        pm = mat["pix"]
        if not pm.isNull():
            tiny = pm.scaled(1, 1, _Qt.AspectRatioMode.IgnoreAspectRatio,
                             _Qt.TransformationMode.SmoothTransformation)
            c = tiny.toImage().pixelColor(0, 0)
            rgb = (c.red() / 255.0, c.green() / 255.0, c.blue() / 255.0)
    mat["_avg_rgb"] = rgb
    return rgb


def _qss_color(s, default="#14181d"):
    """A DSL colour string, made safe to drop straight into a Qt stylesheet.
    Qt's own stylesheet parser reads an 8-digit hex as #AARRGGBB (alpha
    FIRST); Glass's colours are #RRGGBBAA (alpha LAST, CSS-style, per
    _qcolor's docstring and the wiki). Passing an alpha-last hex straight
    into a stylesheet string made Qt misread it: an opaque, wrong-hued
    colour instead of the intended transparency. Routing it through
    _qcolor() first (which already parses alpha-last correctly) and
    re-emitting it as rgba(...) - which Qt's stylesheet parser reads as
    0-255 per channel, unambiguously - fixes that everywhere at once."""
    c = _qcolor(s, default)
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


def _apply_opacity(widget, value):
    """The `opacity:` DSL property used to call setWindowOpacity(), which
    Qt only actually applies to real top-level windows - GlassPanel/
    HolderFrame/FullScreenPanel are all plain child widgets, so it was a
    silent no-op everywhere. QGraphicsOpacityEffect works correctly on an
    ordinary child widget too - verified directly against the same
    non-toplevel setup that made setWindowOpacity do nothing."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return
    from PyQt6.QtWidgets import QGraphicsOpacityEffect
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(max(0.0, min(1.0, v)))
    widget.setGraphicsEffect(effect)


_CELL = {
    "topleft": (0, 0), "topcenter": (0, 1), "top": (0, 1), "up": (0, 1), "topright": (0, 2),
    "left": (1, 0), "center": (1, 1), "middle": (1, 1), "right": (1, 2),
    "bottomleft": (2, 0), "bottomcenter": (2, 1), "bottom": (2, 1), "down": (2, 1), "bottomright": (2, 2),
}


def _cell_align(kw):
    kw = (kw or "center").lower()
    v = Qt.AlignmentFlag.AlignVCenter
    if "top" in kw or kw == "up":
        v = Qt.AlignmentFlag.AlignTop
    elif "bottom" in kw or kw == "down":
        v = Qt.AlignmentFlag.AlignBottom
    h = Qt.AlignmentFlag.AlignHCenter
    if "left" in kw:
        h = Qt.AlignmentFlag.AlignLeft
    elif "right" in kw:
        h = Qt.AlignmentFlag.AlignRight
    return v | h


def _h_align(kw):
    kw = (kw or "center").lower()
    if "left" in kw:
        return Qt.AlignmentFlag.AlignLeft
    if "right" in kw:
        return Qt.AlignmentFlag.AlignRight
    return Qt.AlignmentFlag.AlignHCenter


_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".mp4", ".webm", ".mov")


def _resolve_asset(path):
    if not path:
        return path
    p = path.strip().strip('"').strip("'")
    p = p.lstrip("/\\")                 # "/dir/name" -> project-relative "dir/name"
    if os.path.isabs(p) and os.path.exists(p):
        return p.replace("\\", "/")
    for base in ASSET_DIRS:
        cand = os.path.join(base, p)
        if os.path.exists(cand):
            return cand.replace("\\", "/")
        if not os.path.splitext(p)[1]:        # no extension given -> try common ones
            for ext in _IMG_EXTS:
                if os.path.exists(cand + ext):
                    return (cand + ext).replace("\\", "/")
    return ""                          # not found -> caller draws a color box


def _sprite_css(node):
    """Return a border-image CSS fragment if the node has a resolvable sprite,
    else "". border-image stretches to the widget, so it auto-scales."""
    style = getattr(node, "style", None) or {}
    sprite = node.props.get("sprite") or style.get("sprite")
    spath = _resolve_asset(sprite) if sprite else ""
    if spath:
        return f"border-image:url('{spath}') 0 0 0 0 stretch stretch;border:0;"
    return ""


def _apply_center(layout, keyword):
    """Horizontal alignment of a vertical layout's children (menus/holders)."""
    if not keyword:
        return
    h = _h_align(keyword)
    for idx in range(layout.count()):
        w = layout.itemAt(idx).widget()
        if w is not None:
            layout.setAlignment(w, h)


def _flatten(spec, scope):
    """Resolve `if/else` chains and drop `if:`-false elements.

    Returns (resolved_spec, local_scope). The resolved spec is the same kind
    of object (Rule/Node/Container) with conditionals removed so the rest of
    the renderer can treat it normally.
    """
    local = dict(scope or {})
    local.update(getattr(spec, "variables", {}) or {})
    new = copy.copy(spec)
    new.props = dict(getattr(spec, "props", {}) or {})
    new.textgroups = dict(getattr(spec, "textgroups", {}) or {})
    new.grabs = list(getattr(spec, "grabs", []) or [])
    new.center = getattr(spec, "center", None)
    new.mode = getattr(spec, "mode", None)
    new.children = []
    for ch in (getattr(spec, "children", []) or []):
        if getattr(ch, "kind", None) == "__ifchain__":
            chosen = None
            for cond, branch in ch.branches:
                if cond is None or dsl.eval_condition(cond, local):
                    chosen = branch
                    break
            if chosen is not None:
                fb, _ = _flatten(chosen, local)
                new.props.update(fb.props)
                new.textgroups.update(fb.textgroups)
                new.grabs.extend(fb.grabs)
                if fb.center:
                    new.center = fb.center
                if fb.mode:
                    new.mode = fb.mode
                new.children.extend(fb.children)
            continue
        ifx = getattr(ch, "props", {}).get("if") if hasattr(ch, "props") else None
        if ifx is not None and not dsl.eval_condition(ifx, local):
            continue
        new.children.append(ch)
    return new, local

_FONT_MAP = {
    "timesnewroman": "Times New Roman", "times": "Times New Roman",
    "arial": "Arial", "helvetica": "Helvetica",
    "couriernew": "Courier New", "courier": "Courier New",
    "verdana": "Verdana", "georgia": "Georgia", "tahoma": "Tahoma",
    "comicsans": "Comic Sans MS", "calibri": "Calibri",
    "consolas": "Consolas", "monospace": "Consolas",
}


def _num(d, key, default):
    try:
        return int(float(_resolve_expr(d.get(key, default))))
    except (ValueError, TypeError):
        return default


def _bool(v, default=False):
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def resolve_font(name):
    if not name:
        return None
    key = name.strip().lower().replace(" ", "")
    return _FONT_MAP.get(key, name)


def _font_from_group(font_ref, textgroups):
    """font_ref may be a textgroup name or a direct font family."""
    if not font_ref:
        return None
    if font_ref in textgroups:
        return resolve_font(textgroups[font_ref])
    return resolve_font(font_ref)


def _css_set(css, prop, val):
    """Set/replace a single CSS declaration in a simple stylesheet string."""
    import re as _r
    pat = _r.compile(rf"{prop}\s*:[^;]*;?")
    decl = f"{prop}:{val};"
    if pat.search(css or ""):
        return pat.sub(decl, css, count=1)
    return (css or "") + decl


def _apply_title_style(lbl, style, textgroups, fontscale=1.0):
    """Apply an optional `title: "x" { center, color, font }` style to a label."""
    if not style:
        return
    fam = _font_from_group(style.get("font"), textgroups)
    if fam:
        f = lbl.font(); f.setFamily(fam); lbl.setFont(f)
    color = style.get("color")
    if color:
        lbl.setStyleSheet(_css_set(lbl.styleSheet(), "color", color))
    cen = style.get("center")
    if cen:
        lbl.setAlignment(_h_align(cen) | Qt.AlignmentFlag.AlignVCenter)
        lbl._glass_align = _h_align(cen)


def _scale_of(spec):
    """Return (scaleW, scaleH, scaleContent).

    Missing values repeat the last one given, so `scale { 1.5 }` and
    `scale { 1.5, 1.5 }` both mean a uniform 1.5x zoom of the whole menu
    (box AND contents), not just the box.
    """
    s = getattr(spec, "scale", None)
    if not s:
        return (1.0, 1.0, 1.0)
    vals = [v for v in s if v]
    if not vals:
        return (1.0, 1.0, 1.0)
    last = vals[-1]
    while len(vals) < 3:
        vals.append(last)
    return (vals[0], vals[1], vals[2])


def _scale_font(widget, fontscale):
    if fontscale and abs(fontscale - 1.0) > 1e-3:
        f = widget.font()
        base = f.pointSizeF() if f.pointSizeF() > 0 else 10.0
        f.setPointSizeF(base * fontscale)
        widget.setFont(f)


def _fmtval(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _interp(s, scope):
    """Replace {VarName} in text with the variable's current value. Tolerates
    stray spaces - {Var}, { Var }, {Var } all work the same, since a silently
    unmatched brace (showing literal '{Var }' instead of the real value) is a
    genuinely confusing thing to debug around."""
    if not s or "{" not in s or not scope:
        return s
    import re as _r
    return _r.sub(r"\{\s*([A-Za-z_]\w*)\s*\}",
                  lambda m: _fmtval(scope.get(m.group(1), m.group(0))), s)


def _parse_size(s, default=(0, 0)):
    if not s:
        return default
    s = str(s).strip().lower()
    if "x" in s:
        a, _, b = s.partition("x")
        return (int(_to_int(a)), int(_to_int(b)))
    n = _to_int(s)
    return (n, n) if n else default


def _to_int(v, d=0):
    try:
        return int(float(str(_resolve_expr(v)).strip()))
    except (ValueError, TypeError):
        return d


def _to_float(v, d=0.0):
    try:
        return float(str(_resolve_expr(v)).strip())
    except (ValueError, TypeError):
        return d


def compress_image(img, type_str, target_w, target_h):
    """Return a QImage compressed per `type_str`.
       - none:        shrink to <=500 each side, or half size (whichever smaller)
       - pixel:       pixelate
       - standR(v):   posterize colors; v (0..1) lower = stronger compression
    """
    if img.isNull():
        return img
    ow, oh = img.width(), img.height()
    t = (type_str or "").strip().lower()

    if not t:
        bw = min(500, ow // 2 or ow)
        bh = min(500, oh // 2 or oh)
        return img.scaled(max(1, bw), max(1, bh),
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)

    tw = target_w or min(500, ow)
    th = target_h or min(500, oh)

    if t.startswith("pixel"):
        factor = 8
        m = re.search(r"\(([^)]*)\)", t)
        if m:
            factor = max(2, _to_int(m.group(1), 8))
        small = img.scaled(max(1, tw // factor), max(1, th // factor),
                           Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.FastTransformation)
        return small.scaled(tw, th, Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.FastTransformation)

    if t.startswith("standr"):
        v = 0.5
        m = re.search(r"\(([^)]*)\)", t)
        if m:
            try:
                v = float(m.group(1).strip())
            except ValueError:
                v = 0.5
        levels = max(2, int(max(0.0, min(1.0, v)) * 64))
        step = max(1, 256 // levels)
        # posterize on a small copy (so it stays fast even on full video frames),
        # then scale up to the target size.
        cap = 160
        sw, sh = tw, th
        if max(sw, sh) > cap:
            if sw >= sh:
                sh = max(1, sh * cap // max(1, sw)); sw = cap
            else:
                sw = max(1, sw * cap // max(1, sh)); sh = cap
        small = img.scaled(sw, sh, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        out = small.convertToFormat(QImage.Format.Format_ARGB32)
        for y in range(out.height()):
            for x in range(out.width()):
                c = out.pixelColor(x, y)
                c.setRed((c.red() // step) * step)
                c.setGreen((c.green() // step) * step)
                c.setBlue((c.blue() // step) * step)
                out.setPixelColor(x, y, c)
        return out.scaled(tw, th, Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.FastTransformation)

    # unknown type -> just scale
    return img.scaled(tw, th, Qt.AspectRatioMode.KeepAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)


def _register_vcr(node, scope):
    """Build the engine object for a vcr.* node and register it in ACTIVE_WORLD."""
    import engine
    p = node.props or {}
    kind = node.kind.replace("vcr_", "")
    svc = _interp(str(p.get("svc", "")), scope) or node.label or kind
    tw, th = _parse_size(p.get("size"))
    obj = engine.VCRObject(svc, kind)
    obj.name = node.label or str(svc)        # so raycast parent: <name> can find it
    obj.w = tw or 64
    obj.h = th or 64
    if "collider" in p:
        parts = [x for x in re.split(r"[ ,]+", str(p["collider"]).strip()) if x]
        if len(parts) >= 2:
            obj.collider = (_to_int(parts[0]), _to_int(parts[1]))
        elif len(parts) == 1:
            obj.collider = (_to_int(parts[0]), _to_int(parts[0]))
    obj.friction = _to_float(p.get("friction", 0), 0.0)
    obj.istrigger = str(p.get("istrigger", "false")).lower() in ("true", "1", "yes")
    obj.tag = _interp(str(p.get("tag", "")), scope).strip()
    spr = p.get("sprite") or p.get("image")      # remember for clone()
    obj.sprite = _resolve_asset(spr) if spr else None
    if ACTIVE_WORLD is not None:
        ACTIVE_WORLD.add(obj)
    return obj, tw, th


def _vcr(node, scope):
    p = node.props or {}
    name = _interp(node.label or "", scope)
    obj, tw, th = _register_vcr(node, scope)
    kind = node.kind.replace("vcr_", "")

    if kind.lower() == "raycastobject":         # a Doom-style billboard for raycast
        obj.kind = "raycastobject"
        obj.x = _num(p, "x", 0)
        obj.y = _num(p, "y", 0)
        obj.collide = str(p.get("collide", "false")).lower() in ("1", "true", "yes")
        obj.rc_scale = _to_float(p.get("size", p.get("scale", p.get("size_scale", 1))), 1.0)
        if obj.rc_scale <= 0:
            obj.rc_scale = 1.0
        spr = p.get("sprite") or p.get("image")
        obj.sprite = _resolve_asset(spr) if spr else None
        obj.rc_color = p.get("color")
        obj.rc_opacity = _to_float(p.get("opacity", 1), 1.0)
        obj.widget = None                        # drawn by the raycaster, not as a widget
        return None

    if kind == "video":
        w = _vcr_video(name, p, tw, th)
    elif kind == "gif":
        w = _vcr_gif(name, p, tw, th)
    else:                                   # image or colide
        w = _vcr_image(name, p, tw, th)
    obj.widget = w
    if w is not None:
        w._vcr_obj = obj
        # initial position from x/y if given
        obj.x = _num(p, "x", 0)
        obj.y = _num(p, "y", 0)
        # if center: is set, lay it out centered in its container instead of
        # floating at x/y (works in ui AND non-ui menus)
        w._vcr_center = p.get("center")
        w._vcr_floating = ("x" in p or "y" in p)   # explicit position => free-floating
        # live source: if the filename uses {Var}, reload when it changes
        _track_media(w, node.label or "")
    return w


def _parse_tvec(s):
    s = str(s or "1x1").replace(",", "x").replace(" ", "")
    parts = [p for p in s.split("x") if p]
    try:
        if len(parts) >= 2:
            return (float(parts[0]), float(parts[1]))
        if len(parts) == 1:
            return (float(parts[0]), float(parts[0]))
    except ValueError:
        pass
    return (1.0, 1.0)


def _rc_rows(mapval):
    """Turn a raycaster map into a list of row strings. Accepts:
      - a .json filename (resolved like images): array of rows, or {"map":[...]}
      - a JSON-style array of strings written inline
      - a |-separated string"""
    s = str(mapval or "").strip()
    if s.lower().endswith(".json"):
        path = _resolve_asset(s)
        if path:
            try:
                import json
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    data = data.get("map", [])
                if isinstance(data, list):
                    return [str(r) for r in data]
            except Exception:
                pass
        return []
    if s.startswith("["):
        try:
            import json
            rows = json.loads(s)
            if isinstance(rows, list):
                return [str(r) for r in rows]
        except Exception:
            pass
    return s.split("|")


_RC_EMPTY = {".", "0", " ", "", "_"}


def _blend_pixmaps(a, b, t):
    """Cross-fade from pixmap a to pixmap b by factor t (0..1) - one
    composite operation via QPainter opacity, not a per-pixel Python loop.
    Used to ease a post-effect profile in smoothly (loadPost's smoothness)
    instead of snapping fully on the instant it loads."""
    from PyQt6.QtGui import QPainter
    out = QPixmap(a)
    p = QPainter(out)
    p.setOpacity(max(0.0, min(1.0, t)))
    p.drawPixmap(0, 0, b)
    p.end()
    return out


def _fx_vignette(pixmap, kwargs, quality, state=None):
    """Darkens toward the screen edges via one QRadialGradient fill - a
    native Qt gradient composite, not a per-pixel Python loop, so this is
    essentially free regardless of resolution or quality.
    intensity (0..1, default 0.5): how dark the corners get.
    smoothness (0..1, default 0.5): how gradual the falloff is - lower is
    a harder-edged dark ring, higher fades in more gently."""
    from PyQt6.QtGui import QPainter, QRadialGradient, QColor
    from PyQt6.QtCore import QPointF
    intensity = max(0.0, min(1.0, _to_float(kwargs.get("intensity", 0.5), 0.5)))
    smoothness = max(0.05, min(1.0, _to_float(kwargs.get("smoothness", 0.5), 0.5)))
    if intensity <= 0.001:
        return pixmap
    w, h = pixmap.width(), pixmap.height()
    if w < 1 or h < 1:
        return pixmap
    out = QPixmap(pixmap)
    p = QPainter(out)
    cx, cy = w / 2.0, h / 2.0
    radius = (cx * cx + cy * cy) ** 0.5
    grad = QRadialGradient(QPointF(cx, cy), radius)
    inner_stop = max(0.0, min(0.99, 1.0 - smoothness))
    grad.setColorAt(inner_stop, QColor(0, 0, 0, 0))
    grad.setColorAt(1.0, QColor(0, 0, 0, int(intensity * 255)))
    p.fillRect(out.rect(), grad)
    p.end()
    return out


def _fx_bloom(pixmap, kwargs, quality, state=None):
    """A soft glow, not true HDR bloom - a real per-pixel brightness
    threshold (only bright areas glow) would need a per-pixel Python
    pass, which doesn't fit the same budget everything else here does.
    This blurs the WHOLE frame using the exact same scale-down/up trick
    already used for the lightmap's own softening pass (Qt's native
    image scaling, not a Python loop), then adds a soft copy back on top
    with an additive composite. Still a real, visible glow - just not a
    threshold-isolated one.
    intensity (0..1, default 0.4): how strong the glow is.
    radius (1..20, default 6): how far the glow spreads, in pixels at
    full quality - quality scales the blur cost down at lower settings."""
    from PyQt6.QtGui import QPainter
    from PyQt6.QtCore import Qt as _Qt
    intensity = max(0.0, min(1.0, _to_float(kwargs.get("intensity", 0.4), 0.4)))
    radius = max(1.0, _to_float(kwargs.get("radius", 6.0), 6.0))
    if intensity <= 0.001:
        return pixmap
    w, h = pixmap.width(), pixmap.height()
    if w < 4 or h < 4:
        return pixmap
    q = max(0.2, min(1.0, quality / 100.0))
    soft = max(2, int(radius * q))
    dw, dh = max(1, w // soft), max(1, h // soft)
    small = pixmap.scaled(dw, dh, _Qt.AspectRatioMode.IgnoreAspectRatio,
                          _Qt.TransformationMode.SmoothTransformation)
    glow = small.scaled(w, h, _Qt.AspectRatioMode.IgnoreAspectRatio,
                        _Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(pixmap)
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
    p.setOpacity(intensity)
    p.drawPixmap(0, 0, glow)
    p.end()
    return out


def _fx_colorgrading(pixmap, kwargs, quality, state=None):
    """Tint/saturation/contrast/brightness, each a native Qt composite
    operation - no per-pixel Python loop anywhere in this.
    tint (hex, default none): multiplied over the frame, same technique
    already used for baked wall lighting elsewhere in this file.
    saturation (0..2, default 1.0): 0 = grayscale (a REAL luminosity-
    weighted grayscale via QImage's own conversion, not a channel
    average), 1 = unchanged, 2 = strongly oversaturated.
    contrast (0..2, default 1.0): 1 = unchanged; above 1 blends in a
    self-Overlay pass (verified directly: this genuinely pushes darks
    darker and brights brighter, a real contrast boost); below 1 blends
    toward 50% gray, flattening it.
    brightness (-1..1, default 0.0): blends toward white (positive) or
    black (negative)."""
    from PyQt6.QtGui import QPainter, QColor, QImage
    tint = kwargs.get("tint")
    saturation = max(0.0, min(2.0, _to_float(kwargs.get("saturation", 1.0), 1.0)))
    contrast = max(0.0, min(2.0, _to_float(kwargs.get("contrast", 1.0), 1.0)))
    brightness = max(-1.0, min(1.0, _to_float(kwargs.get("brightness", 0.0), 0.0)))
    if (not tint and abs(saturation - 1.0) < 0.001
            and abs(contrast - 1.0) < 0.001 and abs(brightness) < 0.001):
        return pixmap
    out = QPixmap(pixmap)
    if abs(saturation - 1.0) > 0.001:
        gray_img = out.toImage().convertToFormat(QImage.Format.Format_Grayscale8) \
                                 .convertToFormat(QImage.Format.Format_RGB32)
        gray_pm = QPixmap.fromImage(gray_img)
        p = QPainter(out)
        if saturation < 1.0:
            p.setOpacity(1.0 - saturation)
            p.drawPixmap(0, 0, gray_pm)
        else:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            p.setOpacity(min(1.0, saturation - 1.0))
            p.drawPixmap(0, 0, out)      # push further from gray by re-adding itself
        p.end()
    if abs(contrast - 1.0) > 0.001:
        p = QPainter(out)
        if contrast > 1.0:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Overlay)
            p.setOpacity(min(1.0, contrast - 1.0))
            p.drawPixmap(0, 0, out)
        else:
            p.setOpacity(1.0 - contrast)
            p.fillRect(out.rect(), QColor(128, 128, 128))
        p.end()
    if abs(brightness) > 0.001:
        p = QPainter(out)
        p.setOpacity(min(1.0, abs(brightness)))
        p.fillRect(out.rect(), QColor(255, 255, 255) if brightness > 0 else QColor(0, 0, 0))
        p.end()
    if tint:
        p = QPainter(out)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        p.fillRect(out.rect(), _qcolor(tint))
        p.end()
    return out


_GRAIN_TILE_CACHE = {}   # size -> cached noise QPixmap, generated once


def _grain_tile(size):
    """A small tileable noise texture - generated ONCE per size and cached,
    not regenerated every frame (that per-pixel randomization is the one
    genuinely per-pixel-Python part of this whole system, so it's kept to
    a one-time cost, the same principle as the material bounce-tint's
    cached average color)."""
    cached = _GRAIN_TILE_CACHE.get(size)
    if cached is not None:
        return cached
    import random
    from PyQt6.QtGui import QImage, qRgb
    img = QImage(size, size, QImage.Format.Format_RGB32)
    for y in range(size):
        for x in range(size):
            v = random.randint(0, 255)
            img.setPixel(x, y, qRgb(v, v, v))
    tile = QPixmap.fromImage(img)
    _GRAIN_TILE_CACHE[size] = tile
    return tile


def _fx_filmgrain(pixmap, kwargs, quality, state=None):
    """Adds noise via a cached, tiled texture (native QBrush tiling - the
    noise pixels themselves are generated once per size and cached, not
    regenerated every call). A random tile offset each call gives it a
    flickering, animated look without regenerating the noise data.
    Measured directly: ~3.2ms on a 640x480 frame (~19% of a 60fps budget)
    - real but non-trivial, so it's one to be mindful of if stacking
    several effects at once.
    intensity (0..1, default 0.15): grain strength.
    size (8..128, default 48): the noise tile's resolution - smaller is
    finer grain, larger is chunkier; quality scales this down further at
    lower settings for a cheaper tile."""
    from PyQt6.QtGui import QPainter, QBrush
    from PyQt6.QtCore import QRectF
    import random
    intensity = max(0.0, min(1.0, _to_float(kwargs.get("intensity", 0.15), 0.15)))
    if intensity <= 0.001:
        return pixmap
    base_size = max(8, min(128, int(_to_float(kwargs.get("size", 48), 48))))
    q = max(0.3, min(1.0, quality / 100.0))
    size = max(8, int(base_size * q))
    tile = _grain_tile(size)
    w, h = pixmap.width(), pixmap.height()
    out = QPixmap(pixmap)
    p = QPainter(out)
    # measured directly: SoftLight costs ~13x more than Overlay for this
    # fill (6.9ms vs 0.5ms on a 640x480 frame) for no meaningful visual
    # difference here - Overlay gives the same highlights-and-shadows
    # grain character at a quarter of the cost
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Overlay)
    p.setOpacity(intensity)
    brush = QBrush(tile)
    ox, oy = random.randint(0, size - 1), random.randint(0, size - 1)
    p.setBrushOrigin(-ox, -oy)
    p.fillRect(QRectF(0, 0, w, h), brush)
    p.end()
    return out


def _fx_tonemapping(pixmap, kwargs, quality, state=None):
    """A filmic look, not true HDR tonemapping (there's no HDR data here -
    everything's already 8-bit by the time a post effect sees it). Two
    native Qt composites: a midtone contrast boost (the same self-Overlay
    technique as colorGrading's contrast>1 branch), then a highlight
    rolloff via a Darken composite against a light-gray ceiling - Darken
    keeps whichever value is lower per channel, so only pixels ABOVE the
    ceiling get pulled down; darks are completely untouched by that pass.
    strength (0..1, default 0.5): overall effect strength."""
    from PyQt6.QtGui import QPainter, QColor
    strength = max(0.0, min(1.0, _to_float(kwargs.get("strength", 0.5), 0.5)))
    if strength <= 0.001:
        return pixmap
    out = QPixmap(pixmap)
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Overlay)
    p.setOpacity(strength * 0.6)
    p.drawPixmap(0, 0, out)
    p.end()
    ceiling = int(255 - strength * 60)   # strength=1 -> highlights capped near 195
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Darken)
    p.setOpacity(strength)
    p.fillRect(out.rect(), QColor(ceiling, ceiling, ceiling))
    p.end()
    return out


def _fx_whitebalance(pixmap, kwargs, quality, state=None):
    """Warm/cool and green/magenta shift, using the same multiply-composite
    technique as colorGrading's tint. multiply can only ever darken, never
    brighten, so a warm/orange shift is really "darken blue slightly" and a
    magenta shift is really "darken green slightly" - the opposite channel
    gets pulled down instead of the target channel being pushed up.
    temperature (-100..100, default 0): negative is cooler/blue (darkens
    red), positive is warmer/orange (darkens blue).
    tint (-100..100, default 0): negative is green (darkens red+blue),
    positive is magenta (darkens green)."""
    from PyQt6.QtGui import QPainter, QColor
    temperature = max(-100.0, min(100.0, _to_float(kwargs.get("temperature", 0), 0)))
    tint = max(-100.0, min(100.0, _to_float(kwargs.get("tint", 0), 0)))
    if abs(temperature) < 0.001 and abs(tint) < 0.001:
        return pixmap
    r, g, b = 255, 255, 255
    if temperature > 0:
        b = int(255 - (temperature / 100.0) * 90)
    elif temperature < 0:
        r = int(255 - (-temperature / 100.0) * 90)
    if tint > 0:
        g = min(g, int(255 - (tint / 100.0) * 90))
    elif tint < 0:
        r = min(r, int(255 - (-tint / 100.0) * 60))
        b = min(b, int(255 - (-tint / 100.0) * 60))
    out = QPixmap(pixmap)
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
    p.fillRect(out.rect(), QColor(r, g, b))
    p.end()
    return out


def _fx_antialiasing(pixmap, kwargs, quality, state=None):
    """Softens the raycaster's hard per-column pixel edges - a light blur,
    not true FXAA/MSAA (real edge detection doesn't fit this engine's
    per-frame budget). Reuses the exact scale-down/up trick already proven
    for bloom and the lightmap's own softening pass (native Qt scaling,
    no per-pixel Python loop), then blends the softened copy back at
    partial opacity instead of adding it - that's what keeps this a genuine
    edge-softening rather than a heavy blur.
    strength (0..1, default 0.5): how much softening; quality scales the
    downscale cost at lower settings, same convention as bloom."""
    from PyQt6.QtCore import Qt as _Qt
    strength = max(0.0, min(1.0, _to_float(kwargs.get("strength", 0.5), 0.5)))
    if strength <= 0.001:
        return pixmap
    w, h = pixmap.width(), pixmap.height()
    if w < 4 or h < 4:
        return pixmap
    q = max(0.3, min(1.0, quality / 100.0))
    soft = max(1, int(1 + strength * 5 * q))
    dw, dh = max(1, w // soft), max(1, h // soft)
    small = pixmap.scaled(dw, dh, _Qt.AspectRatioMode.IgnoreAspectRatio,
                          _Qt.TransformationMode.SmoothTransformation)
    blurred = small.scaled(w, h, _Qt.AspectRatioMode.IgnoreAspectRatio,
                           _Qt.TransformationMode.SmoothTransformation)
    return _blend_pixmaps(pixmap, blurred, strength)


def _fx_motionblur(pixmap, kwargs, quality, state=None):
    """A fading trail of recent frames - an accumulation-buffer trail, not
    true per-pixel velocity vectors (this engine's raycaster doesn't expose
    per-pixel motion data). This is the first STATEFUL effect: state is a
    plain dict, one per position in the effect chain (see
    RaycasterWidget._apply_post_effects), that this function is free to
    read and mutate - it persists across frames for as long as the profile
    stays loaded, and resets naturally on the next loadPost.
    strength (0..0.95, default 0.4): how much of the trail persists each
    frame - higher is a longer trail. A single isolated call does almost
    nothing (there's no previous frame yet to blend with); it needs to run
    every frame, same as it will for real in effect{}, to build a trail."""
    strength = max(0.0, min(0.95, _to_float(kwargs.get("strength", 0.4), 0.4)))
    if strength <= 0.001 or state is None:
        return pixmap
    prev = state.get("accum")
    if prev is None or prev.size() != pixmap.size():
        state["accum"] = QPixmap(pixmap)
        return pixmap
    # last frame's accumulated trail blended UNDER the new frame - moving
    # edges leave a fading ghost that decays by (1-strength) every call
    out = _blend_pixmaps(prev, pixmap, 1.0 - strength)
    state["accum"] = QPixmap(out)
    return out


def _fx_autoexposure(pixmap, kwargs, quality, state=None):
    """Gradually brightens dark scenes and dims bright ones toward a target
    level, like a camera's exposure adjusting. The second STATEFUL effect:
    measures the current frame's average luma via the same cheap
    scale-to-1x1-pixel trick already used for material bounce-tint
    averaging elsewhere in this file (no per-pixel Python loop), then eases
    a persistent correction value (state["ev"]) toward the ideal correction
    by `speed` each call rather than snapping straight to it - that eased
    value is the "memory" that needs repeated frames to converge.
    speed (0..1, default 0.05): how fast it adjusts, lower is slower.
    target (0..1, default 0.5): the brightness level aimed for.
    strength (0..1, default 0.5): how strongly the correction applies."""
    from PyQt6.QtCore import Qt as _Qt
    from PyQt6.QtGui import QPainter, QColor
    speed = max(0.0, min(1.0, _to_float(kwargs.get("speed", 0.05), 0.05)))
    target = max(0.0, min(1.0, _to_float(kwargs.get("target", 0.5), 0.5)))
    strength = max(0.0, min(1.0, _to_float(kwargs.get("strength", 0.5), 0.5)))
    if strength <= 0.001 or state is None:
        return pixmap
    tiny = pixmap.scaled(1, 1, _Qt.AspectRatioMode.IgnoreAspectRatio,
                         _Qt.TransformationMode.SmoothTransformation)
    c = tiny.toImage().pixelColor(0, 0)
    luma = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255.0
    ev = state.get("ev", 0.0)
    ideal = max(-1.0, min(1.0, (target - luma) * 2.0))
    ev += (ideal - ev) * speed
    state["ev"] = ev
    if abs(ev) <= 0.005:
        return pixmap
    out = QPixmap(pixmap)
    p = QPainter(out)
    p.setOpacity(min(1.0, abs(ev) * strength))
    p.fillRect(out.rect(), QColor(255, 255, 255) if ev > 0 else QColor(0, 0, 0))
    p.end()
    return out


_POST_EFFECTS = {
    "vignette": _fx_vignette,
    "bloom": _fx_bloom,
    "colorGrading": _fx_colorgrading,
    "filmGrain": _fx_filmgrain,
    "tonemapping": _fx_tonemapping,
    "whiteBalance": _fx_whitebalance,
    "antiAliasing": _fx_antialiasing,
    "motionBlur": _fx_motionblur,
    "autoExposure": _fx_autoexposure,
}   # effect name -> fn(pixmap, kwargs, quality, state) -> pixmap


class RaycasterWidget(QWidget):
    """A Wolfenstein/Doom-style raycaster. Draws a fake-3D first-person view of a
    grid `map`, where each non-empty cell references a `material` (colour or image).
    Reads WASD/arrows from the engine each frame to walk (with wall collision) and
    writes the camera to rayX / rayY / rayA so scripts can read it."""
    def __init__(self, node, world):
        super().__init__()
        import math
        from PyQt6.QtCore import QTimer
        from PyQt6.QtGui import QColor, QPixmap
        p = node.props or {}
        self.grid = [list(r) for r in _rc_rows(p.get("map", ""))]
        self.mh = len(self.grid)
        self.mats = {}                 # wall materials by char
        self.floor_mats = {}           # floor tile materials by char (for floorMap)
        self.roof_mats = {}            # roof tile materials by char (for roofMap)
        self.floor_single = None       # material "floor" -> whole floor
        self.roof_single = None        # material "roof"  -> whole ceiling
        self.lights = []               # baked point lights
        self.dynamic_lights = {}       # id -> {x,y (world px), r,g,b (0-1),
                                        # radius (cells), intensity} - cheap,
                                        # no shadow-casting, safe to move/
                                        # recolor every frame. See light.create
                                        # in engine.py and _light_sample below.
        self._room_scale_cache = {}    # (cx,cy) -> avg cells-to-wall, for audio reverb baking
        for c in (node.children or []):
            ck = getattr(c, "kind", "")
            if ck == "light":
                lp = c.props or {}
                sc = lp.get("shadowCaster", lp.get("shadowcaster", True))
                self.lights.append({
                    "x": _to_float(lp.get("x", 0), 0.0),
                    "y": _to_float(lp.get("y", 0), 0.0),
                    "color": _qcolor(lp.get("color", "#ffffff")),
                    "radius": _to_float(lp.get("radius", 4), 4.0),      # in cells
                    "intensity": _to_float(lp.get("intensity", 1), 1.0),
                    "shadow": str(sc).strip().lower() not in ("false", "0", "no", "off"),
                })
                continue
            if ck != "material":
                continue
            key = (c.label or "").strip()
            mp = c.props or {}
            mtype = str(mp.get("type", "")).strip().lower()
            mat = {"color": None, "pix": None,
                   "tiling": str(mp.get("tiling", "false")).lower() in ("1", "true", "yes"),
                   "tvec": _parse_tvec(mp.get("tilingVector", "1x1"))}
            if mp.get("color"):
                mat["color"] = _qcolor(mp["color"])
            if mp.get("image"):
                path = _resolve_asset(mp["image"])
                if path:
                    pm = QPixmap(path)
                    if not pm.isNull():
                        mat["pix"] = pm
            kl = key.lower()
            if mtype == "floor":
                self.floor_mats[key] = mat          # per-cell floor tile
            elif mtype in ("roof", "ceiling"):
                self.roof_mats[key] = mat           # per-cell roof tile
            elif kl == "floor":
                self.floor_single = mat             # whole floor
            elif kl in ("roof", "ceiling"):
                self.roof_single = mat              # whole ceiling
            else:
                self.mats[key] = mat                # wall
        self.floormap = _rc_rows(p.get("floormap", p.get("floorMap", ""))) or None
        self.roofmap = _rc_rows(p.get("roofmap", p.get("roofMap", ""))) or None
        self._floor_atlas = self._roof_atlas = None
        self.lightgrid = None            # (legacy) unused; kept for compatibility
        self._lm_img = None              # cached raycasted lightmap (live resolution)
        self._lit_wall_cache = {}        # pre-tinted wall textures per face (perf)
        # ambient is full-bright (no-op) when there are no lights, so old levels
        # look identical; with lights, unlit areas fall back to this dim level.
        self.ambient = _to_float(p.get("ambient", 1.0 if not self.lights else 0.28),
                                 1.0 if not self.lights else 0.28)
        self.scene = SCENE_VIEW          # top-down (editor) vs 3D (runner)
        self.fov = math.radians(float(p.get("fov", 66) or 66))
        self.columns = max(40, int(float(p.get("columns", 160) or 160)))
        self.ceil = QColor(p.get("ceiling", "#0b1622"))
        self.floorc = QColor(p.get("floor", "#14202b"))
        self.movespeed = float(p.get("movespeed", p.get("moveSpeed", 3.0)) or 3.0)
        self.turnspeed = float(p.get("turnspeed", p.get("turnSpeed", 2.6)) or 2.6)
        self.parent = (str(p.get("parent", "")).strip() or None)   # follow an object
        self.cellsize = float(p.get("cellsize", p.get("cellSize", 64)) or 64)
        self._cellsize_explicit = ("cellsize" in p or "cellSize" in p)
        self.collide = str(p.get("collide", "false")).lower() in ("1", "true", "yes")
        # fog
        fogc = p.get("fogcolor", p.get("fogColor", ""))
        self.fog = QColor(fogc) if fogc else None
        self.fogrange = float(p.get("fograng", p.get("fogRange", 12)) or 12)
        self.fogamt = max(0.0, min(1.0, float(p.get("fogamount", p.get("fogAmount", 1.0)) or 1.0)))
        # maze id -> register as a collidable mesh on the world
        mid = p.get("mazeid", p.get("mazeID"))
        self.maze_id = None
        if mid not in (None, ""):
            try:
                self.maze_id = int(float(mid))
            except (TypeError, ValueError):
                self.maze_id = str(mid)
        # imported OBJ mesh: raycast { mesh: N } renders the flattened mesh instead
        # of an inline map. Loaded lazily on first paint (setup{} runs after build).
        self._mesh_id = None
        self._mesh_loaded = False
        mprop = p.get("mesh")
        if mprop not in (None, ""):
            try:
                self._mesh_id = int(float(mprop))
            except (TypeError, ValueError):
                self._mesh_id = str(mprop)
            if self.maze_id is None:               # default the maze id to the mesh id
                self.maze_id = self._mesh_id
        self.world = world
        self._asset_dir = (ASSET_DIRS[0] if ASSET_DIRS else ".")   # where lightmaps save
        self._load_room_scale()        # pick up a previous bake, if one was saved -
                                        # needs _asset_dir, so this must come after it
        self._lightmap_path = None
        if world is not None and self.maze_id is not None:
            world.mazes[self.maze_id] = self           # auto-mesh when maze spawns
        self.cx, self.cy = self._spawn()
        self.ang = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background:#000;")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)   # take keyboard directly
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(max(0, int(RENDER_INTERVAL_MS)))

    # feed our captured world's input directly, so movement works even when the
    # page has no update{}/objects and even if the WebEngine view is present
    _KEYMAP = None

    def _kname(self, ev):
        from PyQt6.QtCore import Qt as _Qt
        if RaycasterWidget._KEYMAP is None:
            RaycasterWidget._KEYMAP = {
                _Qt.Key.Key_Left: "left", _Qt.Key.Key_Right: "right",
                _Qt.Key.Key_Up: "up", _Qt.Key.Key_Down: "down",
                _Qt.Key.Key_Space: "space", _Qt.Key.Key_Shift: "shift",
            }
        k = ev.key()
        if k in RaycasterWidget._KEYMAP:
            return RaycasterWidget._KEYMAP[k]
        t = ev.text()
        return t.lower() if t and t.strip() else ""

    def showEvent(self, e):
        super().showEvent(e)
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def mousePressEvent(self, e):
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        if self.world is not None and not e.isAutoRepeat():
            n = self._kname(e)
            if n:
                self.world.input.key_down(n)
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if self.world is not None and not e.isAutoRepeat():
            n = self._kname(e)
            if n:
                self.world.input.key_up(n)
        super().keyReleaseEvent(e)

    def _row(self, y):
        return self.grid[y] if 0 <= y < self.mh else []

    def _cell(self, ix, iy):
        r = self._row(iy)
        if 0 <= ix < len(r):
            return r[ix]
        return "1"                                   # out of bounds = wall

    def _solid(self, x, y):
        return self._cell(int(x), int(y)) not in _RC_EMPTY

    def _blocked(self, x, y):
        if self._solid(x, y):
            return True
        cx, cy = int(x), int(y)
        for o in self._sprites():
            if getattr(o, "collide", False):
                if int(o.x / self.cellsize) == cx and int(o.y / self.cellsize) == cy:
                    return True
        return False

    def _spawn(self):
        for y, row in enumerate(self.grid):
            for x, ch in enumerate(row):
                if ch in _RC_EMPTY:
                    return x + 0.5, y + 0.5
        return 1.5, 1.5

    def _find_parent(self):
        w = self.world
        if not w or not self.parent:
            return None
        o = w.objects.get(str(self.parent))              # by svc
        if o is not None:
            return o
        for o in w.objects.values():                      # by name/label
            if getattr(o, "name", None) == self.parent:
                return o
        return None

    def _tick(self):
        import math
        if self._mesh_id is not None and not self._mesh_loaded:
            self._ensure_mesh()
        w = self.world
        po = self._find_parent()
        if po is not None:
            nx, ny = po.x / self.cellsize, po.y / self.cellsize
            if self.collide:
                # block per-axis so you slide along walls instead of stopping dead
                if not self._blocked(nx, self.cy):
                    self.cx = nx
                else:
                    po.x = self.cx * self.cellsize      # push the object back out
                if not self._blocked(self.cx, ny):
                    self.cy = ny
                else:
                    po.y = self.cy * self.cellsize
            else:
                self.cx, self.cy = nx, ny
            self.ang = math.radians(po.rot)
        else:
            held = w.input.get_held if (w and getattr(w, "input", None)) else (lambda k: False)
            import time as _time
            now = _time.perf_counter()
            last = getattr(self, "_last_tick_ts", None)
            dt = min(0.05, max(0.0, now - last)) if last is not None else (1.0 / 60.0)
            self._last_tick_ts = now
            mv, tr = self.movespeed * dt, self.turnspeed * dt
            if held("left") or held("q"):
                self.ang -= tr
            if held("right") or held("e"):
                self.ang += tr
            dx = dy = 0.0
            if held("w") or held("up"):
                dx += math.cos(self.ang); dy += math.sin(self.ang)
            if held("s") or held("down"):
                dx -= math.cos(self.ang); dy -= math.sin(self.ang)
            if held("a"):
                dx += math.cos(self.ang - math.pi / 2); dy += math.sin(self.ang - math.pi / 2)
            if held("d"):
                dx += math.cos(self.ang + math.pi / 2); dy += math.sin(self.ang + math.pi / 2)
            n = math.hypot(dx, dy)
            if n > 0:
                dx, dy = dx / n * mv, dy / n * mv
                if not self._blocked(self.cx + dx, self.cy):
                    self.cx += dx
                if not self._blocked(self.cx, self.cy + dy):
                    self.cy += dy
        if w is not None:
            w.vars["rayX"] = self.cx
            w.vars["rayY"] = self.cy
            w.vars["rayA"] = self.ang
        self.update()

    def _cast_dda(self, rdx, rdy):
        """DDA along a camera-plane ray. Returns (perpDist, side, cell, wallX)
        where perpDist is already perpendicular (no fisheye) and wallX is the
        0..1 hit position across the wall face (for texturing)."""
        import math
        mapx, mapy = int(self.cx), int(self.cy)
        ddx = abs(1.0 / rdx) if rdx else 1e30
        ddy = abs(1.0 / rdy) if rdy else 1e30
        if rdx < 0:
            stepx, sdx = -1, (self.cx - mapx) * ddx
        else:
            stepx, sdx = 1, (mapx + 1 - self.cx) * ddx
        if rdy < 0:
            stepy, sdy = -1, (self.cy - mapy) * ddy
        else:
            stepy, sdy = 1, (mapy + 1 - self.cy) * ddy
        side = 0
        for _ in range(128):
            if sdx < sdy:
                sdx += ddx; mapx += stepx; side = 0
            else:
                sdy += ddy; mapy += stepy; side = 1
            cell = self._cell(mapx, mapy)
            if cell not in _RC_EMPTY:
                if side == 0:
                    perp = (mapx - self.cx + (1 - stepx) / 2) / rdx if rdx else 1e9
                    wallx = self.cy + perp * rdy
                else:
                    perp = (mapy - self.cy + (1 - stepy) / 2) / rdy if rdy else 1e9
                    wallx = self.cx + perp * rdx
                wallx -= math.floor(wallx)
                return abs(perp), side, cell, wallx, mapx, mapy
        return 1e9, 0, None, 0.0, 0, 0

    def paintEvent(self, _e):
        import math
        from PyQt6.QtGui import QPainter, QColor
        if self._mesh_id is not None and not self._mesh_loaded:
            self._ensure_mesh()
        W, H = self.width(), self.height()
        if W < 2 or H < 2:
            return
        if self._mesh_id is not None and not self._mesh_loaded:
            # mesh referenced but not imported yet (setup{} hasn't run) - wait
            qp = QPainter(self)
            qp.fillRect(self.rect(), self.floorc if hasattr(self, "floorc") else QColor("#0b1622"))
            qp.setPen(QColor("#5b6472"))
            qp.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "loading mesh\u2026")
            return
        if getattr(self, "scene", False):
            self._paint_topdown(QPainter(self), W, H)
            return
        active_post = self.world.active_post if self.world is not None else None
        offscreen = None
        if active_post is not None:
            from PyQt6.QtGui import QPixmap
            offscreen = QPixmap(W, H)
            offscreen.fill(self.floorc if hasattr(self, "floorc") else QColor("#0b1622"))
            qp = QPainter(offscreen)
        else:
            qp = QPainter(self)
        cols = self.columns
        colw = W / cols
        # camera-plane basis: straight walls stay straight (no fisheye curve)
        dirx, diry = math.cos(self.ang), math.sin(self.ang)
        plane = math.tan(self.fov / 2.0)
        planex, planey = -diry * plane, dirx * plane
        self._surfaces(qp, W, H, dirx, diry, planex, planey)
        zbuffer = [1e9] * cols
        for i in range(cols):
            camx = 2.0 * (i + 0.5) / cols - 1.0        # -1..1 across the screen
            rdx = dirx + planex * camx
            rdy = diry + planey * camx
            perp, side, cell, wallx, mcx, mcy = self._cast_dda(rdx, rdy)
            if perp < 0.0001:
                perp = 0.0001
            zbuffer[i] = perp
            lineh = H / perp
            drawstart = -lineh / 2.0 + H / 2.0          # may be off-screen
            drawend = lineh / 2.0 + H / 2.0
            y0 = max(0, int(drawstart))
            y1 = min(H, int(drawend))
            if y1 <= y0:
                continue
            x = int(i * colw)
            cw = int(colw) + 1
            fogf = 0.0
            if self.fog is not None:
                fogf = min(1.0, perp / self.fogrange) * self.fogamt
            mat = self.mats.get(cell)
            if mat and mat["pix"] is not None:
                pos = (rdx > 0) if side == 0 else (rdy > 0)
                pix = self._lit_wall_pixmap(cell, mcx, mcy, side, pos) or mat["pix"]
                tw, th = pix.width(), pix.height()
                tx = int((wallx * mat["tvec"][0]) % 1.0 * tw)
                if tx >= tw:
                    tx = tw - 1
                sy0 = (y0 - drawstart) / lineh * th
                sh = max(1, int((y1 - drawstart) / lineh * th - sy0))
                qp.drawPixmap(x, y0, cw, y1 - y0, pix, tx, int(sy0), 1, sh)
                if self.fog is not None and fogf > 0.01:
                    fc = QColor(self.fog); fc.setAlpha(int(fogf * 255))
                    qp.fillRect(x, y0, cw, y1 - y0, fc)
                elif self.fog is None:
                    d = min(150, int(perp * 11))       # subtle default depth cue
                    if d > 4:
                        qp.fillRect(x, y0, cw, y1 - y0, QColor(0, 0, 0, d))
                continue
            base = mat["color"] if (mat and mat["color"]) else QColor("#8a8f98")
            shade = max(0.22, min(1.0, 1.0 - perp / 13.0))
            if side == 1:
                shade *= 0.7
            lr, lg, lb = self._wall_face_light(mcx, mcy, side, wallx, rdx, rdy)
            r = base.red() * shade * lr
            g = base.green() * shade * lg
            b = base.blue() * shade * lb
            if self.fog is not None and fogf > 0.0:
                fr, fg, fb = self.fog.red(), self.fog.green(), self.fog.blue()
                r = r * (1 - fogf) + fr * fogf
                g = g * (1 - fogf) + fg * fogf
                b = b * (1 - fogf) + fb * fogf
            qp.fillRect(x, y0, cw, y1 - y0, QColor(int(r), int(g), int(b)))
        self._draw_sprites(qp, W, H, cols, colw, dirx, diry, planex, planey, zbuffer)
        self._draw_particles_3d(qp, W, H, cols, colw, dirx, diry, planex, planey, zbuffer)
        if offscreen is not None:
            qp.end()             # must end before the pixmap can be read/processed
            processed = self._apply_post_effects(offscreen, active_post)
            final_qp = QPainter(self)
            final_qp.drawPixmap(0, 0, processed)
            final_qp.end()

    def _apply_post_effects(self, pixmap, active_post):
        """Run the active post-effect profile's effect chain over the
        rendered frame. blend (0..1, from loadPost's smoothness lerp)
        cross-fades between the untouched frame and the fully-processed
        one, so a profile eases in instead of snapping on. quality (0..100,
        from post.quality / postQuality()) is passed to each effect so it
        can scale its own cost - see each effect's own docstring for what
        quality actually changes for it. state_by_pos hands each effect a
        plain dict, keyed by its position in the chain, that persists on
        active_post for as long as this profile stays loaded (a fresh
        loadPost gets a fresh active_post dict, so state naturally resets
        on profile switch) - this is what lets motionBlur/autoExposure
        remember something from the previous frame instead of being pure
        functions like the other seven effects."""
        profile = active_post["profile"]
        blend = active_post["blend"]
        quality = profile.get("quality", 100)
        if blend <= 0.001 or not profile.get("effects"):
            return pixmap
        state_by_pos = active_post.setdefault("state", {})
        result = pixmap
        for i, (name, kwargs) in enumerate(profile["effects"]):
            fn = _POST_EFFECTS.get(name)
            if fn is None:
                continue
            try:
                result = fn(result, kwargs, quality, state_by_pos.setdefault(i, {}))
            except Exception:
                pass          # one bad effect shouldn't blank the whole frame
        if blend >= 0.999:
            return result
        return _blend_pixmaps(pixmap, result, blend)

    def _occluded(self, x0, y0, x1, y1):
        """True if a wall lies strictly between two points (cell units). Grid DDA
        (Amanatides-Woo) - steps only the cells the ray actually crosses, so it's
        fast enough to run per lightmap pixel. Start and target cells never block."""
        cx, cy = int(x0), int(y0)
        tcx, tcy = int(x1), int(y1)
        dx, dy = x1 - x0, y1 - y0
        if dx == 0.0 and dy == 0.0:
            return False
        stepx = 1 if dx >= 0 else -1
        stepy = 1 if dy >= 0 else -1
        inf = 1e30
        invdx = (1.0 / dx) if dx != 0 else inf
        invdy = (1.0 / dy) if dy != 0 else inf
        tmax_x = (((cx + (1 if stepx > 0 else 0)) - x0) * invdx) if dx != 0 else inf
        tmax_y = (((cy + (1 if stepy > 0 else 0)) - y0) * invdy) if dy != 0 else inf
        tdx, tdy = abs(invdx), abs(invdy)
        for _ in range(1024):
            if tmax_x < tmax_y:
                t = tmax_x; tmax_x += tdx; cx += stepx
            else:
                t = tmax_y; tmax_y += tdy; cy += stepy
            if t >= 1.0:
                return False                      # reached the target - clear
            if cx == tcx and cy == tcy:
                return False
            if self._solid(cx + 0.5, cy + 0.5):
                return True
        return False

    def _occluded_batch(self, x0, y0, tx, ty, solid_np):
        """Vectorized equivalent of _occluded for many targets from the SAME
        light position at once - same Amanatides-Woo DDA, same semantics
        (start and target cells never block, out-of-bounds cells beyond
        solid_np's edges are clipped to the nearest edge cell rather than
        forced solid - a deviation from _cell's true "out of bounds = wall"
        only reachable if a light itself sits outside the grid, which
        doesn't happen for any real content this was built against), but
        traces every ray together as numpy arrays instead of one Python
        function call + inner loop per pixel. tx,ty are numpy float arrays
        (cell units); solid_np is the (gh,gw) boolean wall grid. Returns a
        boolean array, True where occluded - verified to match _occluded
        exactly, ray for ray, against real level data before use."""
        gh, gw = solid_np.shape
        n = tx.shape[0]
        cx = _np.full(n, int(x0), dtype=_np.int64)
        cy = _np.full(n, int(y0), dtype=_np.int64)
        tcx = tx.astype(_np.int64)
        tcy = ty.astype(_np.int64)
        dx = tx - x0
        dy = ty - y0
        same = (dx == 0.0) & (dy == 0.0)
        stepx = _np.where(dx >= 0, 1, -1)
        stepy = _np.where(dy >= 0, 1, -1)
        dx_safe = _np.where(dx == 0.0, 1.0, dx)
        dy_safe = _np.where(dy == 0.0, 1.0, dy)
        invdx = _np.where(dx == 0.0, 1e30, 1.0 / dx_safe)
        invdy = _np.where(dy == 0.0, 1e30, 1.0 / dy_safe)
        tmax_x = _np.where(dx == 0.0, 1e30, ((cx + _np.where(stepx > 0, 1, 0)) - x0) * invdx)
        tmax_y = _np.where(dy == 0.0, 1e30, ((cy + _np.where(stepy > 0, 1, 0)) - y0) * invdy)
        tdx = _np.abs(invdx)
        tdy = _np.abs(invdy)

        occluded = _np.zeros(n, dtype=bool)
        active = ~same
        for _ in range(1024):
            if not active.any():
                break
            step_x = active & (tmax_x < tmax_y)
            step_y = active & ~step_x
            t = _np.empty(n)
            t[step_x] = tmax_x[step_x]
            t[step_y] = tmax_y[step_y]
            cx[step_x] += stepx[step_x]
            tmax_x[step_x] += tdx[step_x]
            cy[step_y] += stepy[step_y]
            tmax_y[step_y] += tdy[step_y]
            reached = active & ((t >= 1.0) | ((cx == tcx) & (cy == tcy)))
            check = active & ~reached
            if check.any():
                cxi = _np.clip(cx[check], 0, gw - 1)
                cyi = _np.clip(cy[check], 0, gh - 1)
                solid_here = solid_np[cyi, cxi]
                idx = _np.flatnonzero(check)
                newly_idx = idx[solid_here]
                if newly_idx.size:
                    occluded[newly_idx] = True
                    active[newly_idx] = False
            active &= ~reached
        return occluded

    def _bounce_tint_for_light(self, lcx, lcy, radius):
        """A cheap stand-in for GI: the average color of nearby WALL
        materials, weighted by distance - NOT real bounce ray tracing.
        Real multi-bounce GI needs every surface to gather light from
        every other surface; a single direct-lighting bake already takes
        over a second (measured), so that's a different order of expense
        entirely. This is the same trick a lot of real-time engines use as
        a GI stand-in: let a light pick up a hint of whatever's actually
        around it. One pass over a small bounding box per LIGHT (not per
        pixel) - for a radius-7 light that's at most a 15x15 area checked
        once for the whole bake, not once per pixel.
        Returns (r,g,b) 0..1, or None if there's nothing nearby to bounce off."""
        gh = len(self.grid) or 1
        gw = max((len(row) for row in self.grid), default=1) if self.grid else 1
        x0 = max(0, int(lcx - radius))
        x1 = min(gw - 1, int(lcx + radius))
        y0 = max(0, int(lcy - radius))
        y1 = min(gh - 1, int(lcy + radius))
        total_w = 0.0
        acc_r = acc_g = acc_b = 0.0
        for cy in range(y0, y1 + 1):
            row = self.grid[cy]
            for cx in range(x0, x1 + 1):
                if cx >= len(row) or row[cx] in _RC_EMPTY:
                    continue
                d = ((cx + 0.5 - lcx) ** 2 + (cy + 0.5 - lcy) ** 2) ** 0.5
                if d >= radius:
                    continue
                rgb = _avg_material_color(self.mats.get(row[cx]))
                if rgb is None:
                    continue
                w = 1.0 - d / radius
                acc_r += rgb[0] * w
                acc_g += rgb[1] * w
                acc_b += rgb[2] * w
                total_w += w
        if total_w <= 0.001:
            return None
        return (acc_r / total_w, acc_g / total_w, acc_b / total_w)

    def _bake_lightmap(self, res, progress=None):
        """Bake a raycasted lightmap QImage at `res` (longest side). Every pixel
        gets its own distance falloff and, for shadow-casting lights, a per-pixel
        wall ray-test - so shadow edges are pixel-perfect, not tile-based.
        `progress`, if given, is called with a 0..1 fraction as rows complete."""
        from PyQt6.QtGui import QImage
        gh = len(self.grid) or 1
        gw = max((len(r) for r in self.grid), default=1) if self.grid else 1
        res = max(8, int(res))
        soft = int(_light_quality()["softness"])
        mat_sig = tuple(sorted(
            (k, tuple(round(v, 3) for v in (_avg_material_color(m) or (1.0, 1.0, 1.0))))
            for k, m in self.mats.items()))
        # reuse an identical bake if we've done one (keeps live editing snappy)
        sig = (tuple("".join(r) for r in self.grid), round(self.ambient, 3),
               res, bool(LIGHT_SHADOWS), round(self.cellsize, 2), soft, mat_sig,
               tuple((round(L["x"], 1), round(L["y"], 1), round(L["radius"], 2),
                      round(L["intensity"], 2), L["color"].rgb(),
                      bool(L.get("shadow", True))) for L in self.lights))
        cached = _lightmap_cache_get(sig)
        if cached is not None:
            return cached
        if gw >= gh:
            iw = res; ih = max(1, round(res * gh / gw))
        else:
            ih = res; iw = max(1, round(res * gw / gh))
        amb = self.ambient
        master = LIGHT_SHADOWS
        cs = self.cellsize
        BOUNCE_STRENGTH = 0.22   # tunable - how strongly nearby materials tint a light
        lights = []
        for L in self.lights:
            c = L["color"]
            lcx, lcy = L["x"] / cs, L["y"] / cs
            lrad = max(0.1, L["radius"])
            lr, lg, lb = c.red() / 255.0, c.green() / 255.0, c.blue() / 255.0
            bounce = self._bounce_tint_for_light(lcx, lcy, lrad)
            if bounce is not None:
                lr = lr * (1.0 - BOUNCE_STRENGTH) + bounce[0] * BOUNCE_STRENGTH
                lg = lg * (1.0 - BOUNCE_STRENGTH) + bounce[1] * BOUNCE_STRENGTH
                lb = lb * (1.0 - BOUNCE_STRENGTH) + bounce[2] * BOUNCE_STRENGTH
            lights.append((lcx, lcy, lrad, L["intensity"], lr, lg, lb,
                           bool(L.get("shadow", True))))
        sx, sy = gw / iw, gh / ih
        buf = self._bake_lightmap_rows_numpy(iw, ih, sx, sy, amb, lights, master, progress) \
              if _HAS_NUMPY else \
              self._bake_lightmap_rows_python(iw, ih, sx, sy, amb, lights, master, progress)
        if progress is not None:
            progress(1.0)
        img = QImage(bytes(buf), iw, ih, iw * 4, QImage.Format.Format_RGBA8888)
        # soften hard shadow spikes and grid seams (native scale down/up = fast blur)
        if soft > 1 and iw > 8 and ih > 8:
            from PyQt6.QtCore import Qt as _Qt
            dw, dh = max(1, iw // soft), max(1, ih // soft)
            img = img.scaled(dw, dh, _Qt.AspectRatioMode.IgnoreAspectRatio,
                             _Qt.TransformationMode.SmoothTransformation) \
                     .scaled(iw, ih, _Qt.AspectRatioMode.IgnoreAspectRatio,
                             _Qt.TransformationMode.SmoothTransformation)
        result = img.copy()                       # detach from the Python buffer
        _lightmap_cache_put(sig, result)
        return result

    def _bake_lightmap_rows_python(self, iw, ih, sx, sy, amb, lights, master, progress):
        """The original per-pixel bake loop, unchanged - used when numpy isn't
        available. See _bake_lightmap_rows_numpy for the accelerated version;
        both must produce the same result, since this is the correctness
        reference the numpy path was verified against."""
        buf = bytearray(iw * ih * 4)
        step = max(1, ih // 100)                       # report ~100 progress ticks
        # cells that are walls: skip shadow-testing pixels inside them, so wall
        # regions stay smooth and don't bleed jagged spikes into floor edges
        solidmask = [[0 if ch in _RC_EMPTY else 1 for ch in row] for row in self.grid]
        # wall-cell occlusion only ever depends on (light, cell) - not the exact
        # pixel - so cache it per light per cell instead of re-running the DDA
        # for every pixel inside that cell (a cell can be 1000+ pixels at full
        # bake resolution)
        wall_occ_cache = {}
        for py in range(ih):
            cy = (py + 0.5) * sy
            base = py * iw * 4
            icy = int(cy)
            srow = solidmask[icy] if 0 <= icy < len(solidmask) else None
            for px in range(iw):
                cx = (px + 0.5) * sx
                r = g = b = amb
                icx = int(cx)
                in_wall = bool(srow and 0 <= icx < len(srow) and srow[icx])
                for li, (lx, ly, rad, inten, lr, lg, lb, shadow) in enumerate(lights):
                    ddx = cx - lx; ddy = cy - ly
                    d = (ddx * ddx + ddy * ddy) ** 0.5
                    if d >= rad:
                        continue
                    a = inten * (1.0 - d / rad)
                    a *= a
                    if a <= 0.001:
                        continue
                    if shadow and master:
                        # floor pixels test their exact position (pixel-perfect
                        # shadow edges); wall pixels test their CELL CENTER,
                        # cached per (light, cell) - every pixel inside the
                        # same wall cell then gets the identical occlusion
                        # result, which is what keeps wall faces smooth (no
                        # per-pixel spikes), while still correctly blocking a
                        # light when a NEARER wall stands between it and this
                        # wall cell (previously wall pixels skipped the test
                        # entirely and always lit up regardless of what stood
                        # between them and the light - see wiki for the bug
                        # this fixed)
                        if in_wall:
                            key = (li, icx, icy)
                            occ = wall_occ_cache.get(key)
                            if occ is None:
                                occ = self._occluded(lx, ly, icx + 0.5, icy + 0.5)
                                wall_occ_cache[key] = occ
                        else:
                            occ = self._occluded(lx, ly, cx, cy)
                        if occ:
                            continue
                    r += lr * a; g += lg * a; b += lb * a
                i = base + px * 4
                buf[i] = 255 if r >= 1.0 else int(r * 255)
                buf[i + 1] = 255 if g >= 1.0 else int(g * 255)
                buf[i + 2] = 255 if b >= 1.0 else int(b * 255)
                buf[i + 3] = 255
            if progress is not None and (py % step == 0):
                progress((py + 1) / ih)
        return buf

    def _bake_lightmap_rows_numpy(self, iw, ih, sx, sy, amb, lights, master, progress):
        """Vectorized equivalent of _bake_lightmap_rows_python - same formulas,
        same per-pixel occlusion semantics (still calls self._occluded exactly
        the way the pure-Python path does, so pixel-perfect floor shadows are
        unchanged), verified to match it within floating-point rounding.
        Measured directly on a real 26x26, 6-light level at 1024px: ~2.4s pure
        Python vs a fraction of that here - the difference is where the work
        happens, not what gets computed. Two things change:
          1. Distance/falloff/masking runs as bulk numpy array ops instead of
             a Python-level loop over every pixel x every light - most of
             that loop's iterations were wasted anyway (a light only reaches
             a small fraction of the image).
          2. The expensive per-pixel occlusion DDA now only runs for pixels
             that survive that masking (in a light's radius, meaningfully
             lit, not a wall cell - wall cells still use the per-cell
             memoized test) instead of touching every pixel."""
        xs = (_np.arange(iw) + 0.5) * sx
        ys = (_np.arange(ih) + 0.5) * sy
        cx2d, cy2d = _np.meshgrid(xs, ys)                    # both (ih, iw)
        icx2d = cx2d.astype(_np.int64)
        icy2d = cy2d.astype(_np.int64)

        gh = len(self.grid) or 1
        gw = max((len(r) for r in self.grid), default=1) if self.grid else 1
        solid_np = _np.zeros((gh, gw), dtype=bool)
        for ry, row in enumerate(self.grid):
            for rx, ch in enumerate(row):
                if ch not in _RC_EMPTY:
                    solid_np[ry, rx] = True
        icy_c = _np.clip(icy2d, 0, gh - 1)
        icx_c = _np.clip(icx2d, 0, gw - 1)
        in_wall = solid_np[icy_c, icx_c]                     # (ih, iw) bool
        # a unique id per grid cell, so "which pixels belong to this wall
        # cell" is one vectorized np.isin() call instead of an O(pixels)
        # comparison per occluded cell
        cell_id = icy2d * (gw + 2) + icx2d

        r_buf = _np.full((ih, iw), amb, dtype=_np.float64)
        g_buf = _np.full((ih, iw), amb, dtype=_np.float64)
        b_buf = _np.full((ih, iw), amb, dtype=_np.float64)

        wall_occ_cache = {}
        for li, (lx, ly, rad, inten, lr, lg, lb, shadow) in enumerate(lights):
            ddx = cx2d - lx
            ddy = cy2d - ly
            d = _np.sqrt(ddx * ddx + ddy * ddy)
            in_radius = d < rad
            if not in_radius.any():
                if progress is not None:
                    progress((li + 1) / len(lights))
                continue
            a = inten * (1.0 - d / rad)
            a = a * a
            relevant = in_radius & (a > 0.001)
            if not relevant.any():
                if progress is not None:
                    progress((li + 1) / len(lights))
                continue
            a_final = _np.where(relevant, a, 0.0)

            if shadow and master:
                occluded = _np.zeros((ih, iw), dtype=bool)
                wall_relevant = relevant & in_wall
                if wall_relevant.any():
                    occluded_ids = []
                    for cid in _np.unique(cell_id[wall_relevant]).tolist():
                        wcy, wcx = divmod(cid, gw + 2)
                        key = (li, wcx, wcy)
                        occ = wall_occ_cache.get(key)
                        if occ is None:
                            occ = self._occluded(lx, ly, wcx + 0.5, wcy + 0.5)
                            wall_occ_cache[key] = occ
                        if occ:
                            occluded_ids.append(cid)
                    if occluded_ids:
                        occluded |= wall_relevant & _np.isin(cell_id, occluded_ids)
                floor_relevant = relevant & ~in_wall
                fy, fx = _np.nonzero(floor_relevant)
                if fy.size:
                    fcx = cx2d[fy, fx]
                    fcy = cy2d[fy, fx]
                    occ_flags = self._occluded_batch(lx, ly, fcx, fcy, solid_np)
                    if occ_flags.any():
                        occluded[fy[occ_flags], fx[occ_flags]] = True
                a_final = _np.where(occluded, 0.0, a_final)

            r_buf += lr * a_final
            g_buf += lg * a_final
            b_buf += lb * a_final
            if progress is not None:
                progress((li + 1) / len(lights))

        rgba = _np.empty((ih, iw, 4), dtype=_np.uint8)
        rgba[..., 0] = (_np.clip(r_buf, 0.0, 1.0) * 255).astype(_np.uint8)
        rgba[..., 1] = (_np.clip(g_buf, 0.0, 1.0) * 255).astype(_np.uint8)
        rgba[..., 2] = (_np.clip(b_buf, 0.0, 1.0) * 255).astype(_np.uint8)
        rgba[..., 3] = 255
        return rgba.tobytes()

    def _live_lightmap(self):
        """The cached lightmap used by the live 3D view (resolution capped so the
        editor stays responsive; explicit bakes use the full resolution)."""
        if self._lm_img is None:
            self._lit_wall_cache = {}                  # tints depend on the lightmap
            self._lm_img = self._bake_lightmap(min(int(SHADOW_RESOLUTION),
                                                   _LIVE_SHADOW_CAP))
        return self._lm_img

    def _light_sample(self, cx, cy):
        """Sample the raycasted lightmap at any point (cell units, floats),
        plus any DYNAMIC lights added on top (see self.dynamic_lights /
        light.create in engine.py). Baked lights give pixel-perfect
        shadows but can never move without a full re-bake - measured
        directly: over a second even at the capped live resolution, ~72x
        a 60fps frame budget, so moving one every frame would freeze the
        game, not just stutter it. Dynamic lights are the opposite
        tradeoff: cheap enough to move and recolor every single frame,
        but no shadow-casting - they shine through walls, since there's
        no per-pixel occlusion test for them at all."""
        if not self.lights:
            r = g = b = 1.0
        else:
            img = self._live_lightmap()
            gh = len(self.grid) or 1
            gw = max((len(row) for row in self.grid), default=1) if self.grid else 1
            if gw < 1 or gh < 1:
                r = g = b = 1.0
            else:
                px = int(cx / gw * img.width())
                py = int(cy / gh * img.height())
                px = 0 if px < 0 else (img.width() - 1 if px >= img.width() else px)
                py = 0 if py < 0 else (img.height() - 1 if py >= img.height() else py)
                c = img.pixelColor(px, py)
                r, g, b = c.red() / 255.0, c.green() / 255.0, c.blue() / 255.0
        if self.dynamic_lights:
            cs = self.cellsize or 40.0
            for L in self.dynamic_lights.values():
                dx = cx - L["x"] / cs
                dy = cy - L["y"] / cs
                d = (dx * dx + dy * dy) ** 0.5
                rad = L["radius"]
                if d >= rad:
                    continue
                a = L["intensity"] * (1.0 - d / rad)
                a *= a
                if a <= 0.001:
                    continue
                r += L["r"] * a
                g += L["g"] * a
                b += L["b"] * a
        return (min(1.0, r), min(1.0, g), min(1.0, b))

    def _cell_light(self, cx, cy):
        """The baked (r,g,b) light multiplier at a cell centre (sampled from the
        raycasted lightmap), or full white if the maze has no lights."""
        return self._light_sample(cx + 0.5, cy + 0.5)

    def room_scale_at(self, cx, cy, realtime=False, max_r=14.0, step=0.5):
        """Rough enclosure estimate at a cell: the average distance (in
        cells) to the nearest wall across 8 directions, capped at max_r.
        Small = a tight hallway/small room (more reflective, more reverb).
        Large = an open area, or no wall found within range (drier, less
        reverb). This is a simple approximation, not real acoustics - it's
        cheap, deterministic, and gives a genuinely different, sensible
        answer for a broom closet vs. a stadium, which is what it's for.

        Cached per (int(cx), int(cy)) since the map doesn't move - pass
        realtime=True (audio.playSound's realtimeRef: true) to bypass the
        cache and measure fresh right now instead of using the baked value.
        The cache is loaded from roomscale_<mazeID>.json at construction if
        a previous bake saved one (see save_room_scale) - see that method's
        docstring for why baking used to be lost every session."""
        key = (int(cx), int(cy))
        if not realtime and key in self._room_scale_cache:
            return self._room_scale_cache[key]
        dirs = ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0),
                (0.70711, 0.70711), (-0.70711, 0.70711), (0.70711, -0.70711), (-0.70711, -0.70711))
        total = 0.0
        for dx, dy in dirs:
            d = 0.0
            while d < max_r and not self._solid(cx + dx * d, cy + dy * d):
                d += step
            total += min(d, max_r)
        scale = total / len(dirs)
        self._room_scale_cache[key] = scale
        return scale

    def _room_scale_path(self, path=None):
        if path is not None:
            return path
        d = getattr(self, "_asset_dir", ".") or "."
        return os.path.join(d, "roomscale_%s.json" % self.maze_id)

    def save_room_scale(self, path=None):
        """Write the current room_scale_at cache to disk (roomscale_<mazeID>.json
        next to the project, same convention as lightmap_<mazeID>.png), so a
        bake actually survives closing the editor/game - previously this
        cache lived ONLY on this one in-memory RaycasterWidget instance,
        which gets thrown away and rebuilt from scratch on every single
        render (every keystroke in Live mode, every reload in the real
        game), so 'baking' had no lasting effect at all: the very next
        render started from a completely empty cache again, editor or not.
        Returns the path written, or '' on failure."""
        path = self._room_scale_path(path)
        try:
            import json
            data = {f"{cx},{cy}": v for (cx, cy), v in self._room_scale_cache.items()}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return path
        except Exception:
            return ""

    def _load_room_scale(self):
        path = self._room_scale_path()
        try:
            import json
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            loaded = {}
            for k, v in data.items():
                cx, _, cy = k.partition(",")
                loaded[(int(cx), int(cy))] = float(v)
            self._room_scale_cache.update(loaded)
        except Exception:
            pass          # no bake saved yet, or it's unreadable - fine,

    def _lit_wall_pixmap(self, cell, mcx, mcy, side, pos):
        """A copy of a wall texture with its baked face-light multiplied in ONCE
        (and side-shade baked too), cached per face. Lets the wall loop just draw a
        pre-lit pixmap - no per-column multiply/compositing, which is a big speedup."""
        key = (cell, mcx, mcy, side, pos)
        hit = self._lit_wall_cache.get(key, 0)
        if hit != 0:
            return hit
        mat = self.mats.get(cell)
        base = mat["pix"] if mat else None
        if base is None:
            self._lit_wall_cache[key] = None
            return None
        if not self.lights and side == 0:
            self._lit_wall_cache[key] = base
            return base
        # face light at the face centre (one sample, not per column)
        if self.lights:
            if side == 0:
                nx = -1.0 if pos else 1.0
                lr, lg, lb = self._light_sample(mcx + 0.5 + nx * 0.55, mcy + 0.5)
            else:
                ny = -1.0 if pos else 1.0
                lr, lg, lb = self._light_sample(mcx + 0.5, mcy + 0.5 + ny * 0.55)
        else:
            lr = lg = lb = 1.0
        if side == 1:                                 # y-faces a touch darker (depth cue)
            lr *= 0.82; lg *= 0.82; lb *= 0.82
        from PyQt6.QtGui import QPixmap, QPainter, QColor
        tinted = QPixmap(base)                        # copy the texture
        p = QPainter(tinted)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        p.fillRect(tinted.rect(),
                   QColor(int(lr * 255), int(lg * 255), int(lb * 255)))
        p.end()
        self._lit_wall_cache[key] = tinted
        return tinted

    def _mesh_material(self, spec):
        """Build a raycaster material dict from a glassmesh material spec."""
        from PyQt6.QtGui import QColor, QPixmap
        mat = {"color": None, "pix": None, "tiling": False, "tvec": (1.0, 1.0)}
        if spec.get("color"):
            mat["color"] = QColor(spec["color"])
        img = spec.get("image")
        if img:
            pm = QPixmap(img)
            if not pm.isNull():
                mat["pix"] = pm
        return mat

    def _load_mesh(self, md):
        """Populate this raycaster's grid + materials from an imported MeshData."""
        self.grid = [list(r) for r in md.grid]
        self.mh = len(self.grid)
        self.mats = {ch: self._mesh_material(s) for ch, s in md.mats.items()}
        self.floormap = list(md.floormap) if md.floormap else None
        self.floor_mats = {ch: self._mesh_material(s)
                           for ch, s in md.floor_mats.items()}
        self.roofmap = list(md.roofmap) if getattr(md, "roofmap", None) else None
        self.roof_mats = {ch: self._mesh_material(s)
                          for ch, s in getattr(md, "roof_mats", {}).items()}
        if self.roofmap:
            self.roof_single = None            # use the per-cell roof tiles from the mesh
        if not self._cellsize_explicit:       # keep the raycast's cellSize if it set one
            self.cellsize = md.cellsize
        self._floor_atlas = self._roof_atlas = None      # rebuild atlases        self._lm_img = None
        self._lit_wall_cache = {}
        spawn = getattr(md, "spawn", None)
        po = self._find_parent()

        def _on_floor(cx, cy):
            icx, icy = int(cx), int(cy)
            if self.floormap is not None:
                if 0 <= icy < len(self.floormap):
                    row = self.floormap[icy]
                    if 0 <= icx < len(row):
                        return row[icx] not in _RC_EMPTY
                return False
            return not self._blocked(cx, cy)   # no floormap: walkable = not a wall

        if spawn:
            if po is not None:
                # if the followed object isn't standing on the mesh's walkable floor,
                # drop it onto the spawn cell so the camera starts INSIDE the level
                if not _on_floor(po.x / self.cellsize, po.y / self.cellsize):
                    po.x = spawn[0] * self.cellsize
                    po.y = spawn[1] * self.cellsize
                    self.cx, self.cy = spawn
            else:
                self.cx, self.cy = spawn
        elif po is None:
            self.cx, self.cy = self._spawn()             # re-place free camera
        if self.world is not None and self.maze_id is not None:
            self.world.mazes[self.maze_id] = self         # collidable now

    def _ensure_mesh(self):
        """Lazily load a  mesh: N  reference once glassmesh has imported it (the
        import happens in setup{}, which runs after widgets are built)."""
        if self._mesh_id is None or self._mesh_loaded:
            return
        try:
            import glassmesh
            md = glassmesh.get(self._mesh_id)
        except Exception:
            md = None
        if md is not None:
            self._load_mesh(md)
            self._mesh_loaded = True

    def _wall_face_light(self, mcx, mcy, side, wallx, rdx, rdy):
        """Light on a wall's visible face: sampled in the OPEN cell just outside the
        hit face (along the face normal), so walls pick up the corridor's light
        instead of their dark interior. side 0 = vertical face, 1 = horizontal."""
        if not self.lights:
            return (1.0, 1.0, 1.0)
        if side == 0:                                 # vertical face: normal is +/-x
            nx = -1.0 if rdx > 0 else 1.0
            sx = mcx + 0.5 + nx * 0.55
            sy = mcy + wallx
        else:                                         # horizontal face: normal +/-y
            ny = -1.0 if rdy > 0 else 1.0
            sx = mcx + wallx
            sy = mcy + 0.5 + ny * 0.55
        return self._light_sample(sx, sy)

    def bake_lightmap_image(self, tile=None, smooth=None, progress=None):
        """The full-resolution raycasted lightmap (used for PNG export). Uses the
        current SHADOW_RESOLUTION (up to 4K). Empty 1x1 white if unlit."""
        from PyQt6.QtGui import QImage, QColor
        if not self.lights:
            img = QImage(1, 1, QImage.Format.Format_ARGB32)
            img.fill(QColor(255, 255, 255))
            return img
        return self._bake_lightmap(int(SHADOW_RESOLUTION), progress=progress)

    def save_lightmap(self, path=None, progress=None):
        """Bake and write the maze's lightmap to a PNG. Returns the file path
        (or '' if there are no lights). Saved as lightmap_<mazeID>.png by default."""
        if not self.lights:
            return ""
        import os
        img = self.bake_lightmap_image(progress=progress)
        if path is None:
            d = getattr(self, "_asset_dir", ".") or "."
            path = os.path.join(d, "lightmap_%s.png" % self.maze_id)
        try:
            img.save(path, "PNG")
            self._lightmap_path = path
            return path
        except Exception:
            return ""

    def lightmap_path(self):
        """Path to the saved lightmap (baking + saving it first if needed)."""
        if getattr(self, "_lightmap_path", None):
            return self._lightmap_path
        return self.save_lightmap()

    def _bake_atlas(self, cache_attr, per_cell_map, mats, single, default_color, tile=24):
        """Bake the level's floor/roof into ONE QImage (each cell stamped once).
        Pure Qt - no numpy. Cached."""
        cur = getattr(self, cache_attr, None)
        if cur is not None:
            return cur
        tile = _light_quality()["tile"] if self.lights else tile
        from PyQt6.QtGui import QImage, QPainter, QColor
        from PyQt6.QtCore import QRect
        gh = self.mh or 1
        gw = max((len(r) for r in self.grid), default=1) if self.grid else 1
        if per_cell_map:
            gh = max(gh, len(per_cell_map))
            gw = max(gw, max((len(r) for r in per_cell_map), default=1))
        img = QImage(max(1, gw) * tile, max(1, gh) * tile,
                     QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QColor(default_color))
        p = QPainter(img)
        for cy in range(gh):
            for cx in range(gw):
                mat = None
                if per_cell_map:
                    if cy < len(per_cell_map) and cx < len(per_cell_map[cy]):
                        mat = mats.get(per_cell_map[cy][cx])
                elif single:
                    mat = single
                if mat is None:
                    continue
                r = QRect(cx * tile, cy * tile, tile, tile)
                if mat["pix"] is not None:
                    p.drawPixmap(r, mat["pix"])
                elif mat["color"] is not None:
                    p.fillRect(r, mat["color"])
        # bake the raycasted lightmap straight into the texture (multiply pass) -
        # scaled to the atlas so floor/roof shadows are pixel-perfect, not tiled
        if self.lights:
            from PyQt6.QtCore import Qt
            lm = self._live_lightmap().scaled(
                img.width(), img.height(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            p.drawImage(0, 0, lm)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.end()
        baked = (img, gw, gh, tile)
        setattr(self, cache_attr, baked)
        return baked

    def _fog_overlay(self, qp, W, y0, y1, is_floor):
        from PyQt6.QtGui import QLinearGradient, QColor, QBrush
        a = int(min(1.0, self.fogamt) * 255)
        far = QColor(self.fog); far.setAlpha(a)
        near = QColor(self.fog); near.setAlpha(0 if is_floor else int(a * 0.35))
        grad = QLinearGradient(0, y0, 0, y1)
        if is_floor:                              # horizon(y0)=far -> bottom(y1)=near
            grad.setColorAt(0.0, far); grad.setColorAt(1.0, near)
        else:                                     # top(y0)=near -> horizon(y1)=far
            grad.setColorAt(0.0, near); grad.setColorAt(1.0, far)
        qp.fillRect(0, y0, W, y1 - y0, QBrush(grad))

    def _cast_surface(self, qp, W, H, dirx, diry, planex, planey, is_floor):
        from PyQt6.QtGui import QColor, QTransform, QPolygonF, QPainter
        from PyQt6.QtCore import QPointF
        horizon = H // 2
        if is_floor:
            y0, y1 = horizon, H
            pmap, mats, single = self.floormap, self.floor_mats, self.floor_single
            base_c = (single and single["color"]) or self.floorc
            cache = "_floor_atlas"
        else:
            y0, y1 = 0, horizon
            pmap, mats, single = self.roofmap, self.roof_mats, self.roof_single
            base_c = (single and single["color"]) or self.ceil
            cache = "_roof_atlas"
        need_atlas = (single and single["pix"]) or (pmap and mats)
        if not need_atlas:
            qp.fillRect(0, y0, W, y1 - y0, QColor(base_c))     # solid-colour fast path
            return
        atlas, aw, ah, tile = self._bake_atlas(cache, pmap, mats, single, base_c)
        qp.fillRect(0, y0, W, y1 - y0, QColor(base_c))         # under-fill (past atlas)
        posZ = 0.5 * H
        posx, posy = self.cx, self.cy
        rdx0, rdy0 = dirx - planex, diry - planey
        rdx1, rdy1 = dirx + planex, diry + planey

        def row_pts(sy):
            d = (sy - horizon) if is_floor else (horizon - sy)
            if d == 0:
                d = 0.0001
            rd = posZ / d
            return ((posx + rd * rdx0) * tile, (posy + rd * rdy0) * tile), \
                   ((posx + rd * rdx1) * tile, (posy + rd * rdy1) * tile)

        if is_floor:
            yn, yf = H - 1, horizon + max(2, (H - horizon) // 5)
        else:
            yn, yf = 0, horizon - max(2, horizon // 5)
        Ln, Rn = row_pts(yn)
        Lf, Rf = row_pts(yf)
        aq = QPolygonF([QPointF(*Ln), QPointF(*Rn), QPointF(*Rf), QPointF(*Lf)])
        sq = QPolygonF([QPointF(0, yn), QPointF(W, yn), QPointF(W, yf), QPointF(0, yf)])
        T = QTransform()
        if not QTransform.quadToQuad(aq, sq, T):
            return
        qp.save()
        qp.setClipRect(0, y0, W, y1 - y0)
        qp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        qp.setTransform(T)
        qp.drawImage(0, 0, atlas)
        qp.restore()
        if self.fog is not None:
            self._fog_overlay(qp, W, y0, y1, is_floor)

    def _surfaces(self, qp, W, H, dirx, diry, planex, planey):
        self._cast_surface(qp, W, H, dirx, diry, planex, planey, is_floor=False)
        self._cast_surface(qp, W, H, dirx, diry, planex, planey, is_floor=True)

    def _paint_topdown(self, qp, W, H):
        """Unity-style 2D scene view: the map from above, with the player and
        raycast objects. Used in the editor; the runner draws the 3D view."""
        import math
        from PyQt6.QtGui import QColor, QPen
        gh = self.mh or 1
        gw = max((len(r) for r in self.grid), default=1) if self.grid else 1
        qp.fillRect(0, 0, W, H, QColor("#0a0e14"))
        if gw < 1 or gh < 1:
            return
        cell = min(W / gw, H / gh)
        ox = (W - cell * gw) / 2.0
        oy = (H - cell * gh) / 2.0

        def cell_floor_color(cx, cy):
            if self.floormap and self.floor_mats:
                if cy < len(self.floormap) and cx < len(self.floormap[cy]):
                    m = self.floor_mats.get(self.floormap[cy][cx])
                    if m and m["color"]:
                        return m["color"]
            if self.floor_single and self.floor_single["color"]:
                return self.floor_single["color"]
            return self.floorc

        for cy in range(gh):
            row = self.grid[cy] if cy < len(self.grid) else ""
            for cx in range(gw):
                ch = row[cx] if cx < len(row) else "1"
                x, y = ox + cx * cell, oy + cy * cell
                if ch in _RC_EMPTY:
                    col = cell_floor_color(cx, cy)
                else:
                    mat = self.mats.get(ch)
                    col = (mat and mat["color"]) or QColor("#3a4351")
                qp.fillRect(int(x), int(y), int(cell) + 1, int(cell) + 1, QColor(col))
        # grid lines
        qp.setPen(QPen(QColor("#1b2431"), 1))
        for cx in range(gw + 1):
            qp.drawLine(int(ox + cx * cell), int(oy), int(ox + cx * cell), int(oy + cell * gh))
        for cy in range(gh + 1):
            qp.drawLine(int(ox), int(oy + cy * cell), int(ox + cell * gw), int(oy + cy * cell))
        # raycast objects (billboards) as dots
        for o in self._sprites():
            sx = ox + (o.x / self.cellsize) * cell
            sy = oy + (o.y / self.cellsize) * cell
            c = QColor(getattr(o, "rc_color", None) or "#ffd23c")
            qp.setBrush(c); qp.setPen(QPen(QColor("#05070b"), 1))
            qp.drawEllipse(int(sx - cell * 0.22), int(sy - cell * 0.22),
                           int(cell * 0.44), int(cell * 0.44))
        # the player / camera + its facing
        px, py = ox + self.cx * cell, oy + self.cy * cell
        qp.setBrush(QColor("#6cf09a")); qp.setPen(QPen(QColor("#05070b"), 1))
        qp.drawEllipse(int(px - cell * 0.24), int(py - cell * 0.24),
                       int(cell * 0.48), int(cell * 0.48))
        qp.setPen(QPen(QColor("#6cf09a"), 2))
        qp.drawLine(int(px), int(py),
                    int(px + math.cos(self.ang) * cell * 0.8),
                    int(py + math.sin(self.ang) * cell * 0.8))
        # FOV wedge
        qp.setPen(QPen(QColor(108, 240, 154, 90), 1))
        for s in (-1, 1):
            a = self.ang + s * self.fov / 2
            qp.drawLine(int(px), int(py),
                        int(px + math.cos(a) * cell * 1.6),
                        int(py + math.sin(a) * cell * 1.6))

    def hitscan(self, gx, gy, ang, ignore=None, maxdist=64.0):
        """Cast a ray from grid point (gx,gy) at angle `ang`. Returns
        (object_or_None, dist, hitX, hitY, hit_wall_bool) in grid units.
        Finds the nearest raycast object in front of the ray before a wall."""
        import math
        rdx, rdy = math.cos(ang), math.sin(ang)
        walld = maxdist
        t = 0.0
        while t < maxdist:                       # march to the first wall
            if self._solid(gx + rdx * t, gy + rdy * t):
                walld = t
                break
            t += 0.05
        best, bestd = None, walld
        for o in self._sprites():                # nearest object along the ray
            if ignore is not None and str(o.svc) == str(ignore):
                continue
            ox = o.x / self.cellsize - gx
            oy = o.y / self.cellsize - gy
            along = ox * rdx + oy * rdy
            if along <= 0 or along > bestd:
                continue
            perp = abs(ox * (-rdy) + oy * rdx)
            if perp < 0.4:                        # hit radius (grid units)
                best, bestd = o, along
        if best is not None:
            return best, bestd, gx + rdx * bestd, gy + rdy * bestd, False
        return None, walld, gx + rdx * walld, gy + rdy * walld, (walld < maxdist)

    def _sprites(self):
        w = self.world
        if not w:
            return []
        return [o for o in w.objects.values()
                if getattr(o, "kind", "") == "raycastobject"]

    def _sprite_pix(self, path):
        if not path:
            return None
        cache = getattr(self, "_pixcache", None)
        if cache is None:
            cache = self._pixcache = {}
        if path not in cache:
            from PyQt6.QtGui import QPixmap
            pm = QPixmap(path)
            cache[path] = pm if not pm.isNull() else None
        return cache[path]

    def _draw_sprites(self, qp, W, H, cols, colw, dirx, diry, planex, planey, zbuffer):
        import math
        from PyQt6.QtGui import QColor
        sprites = self._sprites()
        if not sprites:
            return
        det = planex * diry - dirx * planey
        if abs(det) < 1e-9:
            return
        inv = 1.0 / det
        items = []
        for o in sprites:
            gx, gy = o.x / self.cellsize, o.y / self.cellsize
            dx, dy = gx - self.cx, gy - self.cy
            items.append((dx * dx + dy * dy, o, dx, dy))
        items.sort(key=lambda t: t[0], reverse=True)   # far -> near (dist only)
        for _d2, o, dx, dy in items:
            tx = inv * (diry * dx - dirx * dy)
            ty = inv * (-planey * dx + planex * dy)     # depth along view
            if ty <= 0.02:
                continue
            scale = getattr(o, "rc_scale", 1.0) or 1.0
            screenx = int((W / 2.0) * (1 + tx / ty))
            sh = abs(H / ty) * scale
            sw = sh
            dsy = -sh / 2.0 + H / 2.0
            dsx = int(screenx - sw / 2.0)
            dex = int(screenx + sw / 2.0)
            vis_y0 = max(0, int(dsy))
            vis_y1 = min(H, int(dsy + sh))
            if vis_y1 <= vis_y0:
                continue
            pix = self._sprite_pix(getattr(o, "sprite", None))
            col = getattr(o, "rc_color", None)
            op = getattr(o, "rc_opacity", 1.0)
            if op < 1.0:
                qp.setOpacity(max(0.0, op))
            fillc = _qcolor(col) if col else None
            if fillc is not None and self.lights:
                lr, lg, lb = self._cell_light(int(o.x / self.cellsize),
                                              int(o.y / self.cellsize))
                fillc = QColor(int(fillc.red() * lr), int(fillc.green() * lg),
                               int(fillc.blue() * lb), fillc.alpha())
            for stripe in range(max(0, dsx), min(W, dex)):
                si = int(stripe / colw)
                if si < 0 or si >= cols or ty >= zbuffer[si]:
                    continue                     # behind a wall
                if pix is not None:
                    tw, th = pix.width(), pix.height()
                    texx = int((stripe - dsx) / sw * tw)
                    texx = max(0, min(tw - 1, texx))
                    sy0 = (vis_y0 - dsy) / sh * th
                    syh = max(1, int((vis_y1 - dsy) / sh * th - sy0))
                    qp.drawPixmap(stripe, vis_y0, 1, vis_y1 - vis_y0, pix,
                                  texx, int(sy0), 1, syh)
                elif fillc is not None:
                    qp.fillRect(stripe, vis_y0, 1, vis_y1 - vis_y0, fillc)
            if op < 1.0:
                qp.setOpacity(1.0)

    def _draw_particles_3d(self, qp, W, H, cols, colw, dirx, diry, planex, planey, zbuffer):
        """3D-mode particles (burst(...,true) / particleSystem(mode:3d)) - same
        camera-space projection as billboards above, so a particle lands at
        the correct screen spot, scales with distance, and hides behind a
        wall via the same z-buffer check, instead of floating as a flat 2D
        overlay unaware the camera even exists."""
        world = self.world
        parts = getattr(world, "particles_3d", None) if world is not None else None
        if not parts:
            return
        from PyQt6.QtGui import QColor
        det = planex * diry - dirx * planey
        if abs(det) < 1e-9:
            return
        inv = 1.0 / det
        items = []
        for pt in parts:
            gx, gy = pt["x"] / self.cellsize, pt["y"] / self.cellsize
            dx, dy = gx - self.cx, gy - self.cy
            items.append((dx * dx + dy * dy, pt, dx, dy))
        items.sort(key=lambda t: t[0], reverse=True)   # far -> near, like sprites
        for _d2, pt, dx, dy in items:
            tx = inv * (diry * dx - dirx * dy)
            ty = inv * (-planey * dx + planex * dy)
            if ty <= 0.02:
                continue
            screenx = (W / 2.0) * (1 + tx / ty)
            si = int(screenx / colw)
            if si < 0 or si >= cols or ty >= zbuffer[si]:
                continue                                # behind a wall
            # a billboard uses sh = abs(H/ty)*scale with scale~1.0 meaning
            # "full screen height at 1 cell away" - particles want to read
            # as small dots, not full-height sprites, hence the /34 term
            life = pt.get("life") or 1.0
            age_frac = max(0.0, min(1.0, pt["age"] / life))
            fade = 1.0 - age_frac
            base_size = pt.get("size", 5.0) * max(0.0, 1.0 - pt.get("sizeOverLife", 0.0) * age_frac)
            sh = max(1.5, abs(H / ty) * (base_size / 34.0))
            # height (z, world pixels above the floor) shifts the particle up
            # the screen the same way distance shrinks it - both scale by H/ty,
            # so something twice as far away needs half the pixel offset for
            # the same apparent height, exactly like real perspective.
            screeny = H / 2.0 - (pt.get("z", 0.0) / self.cellsize) * (H / ty)
            texture = pt.get("color", "#ffcb6b")
            if not str(texture).startswith("#"):
                pix = self._sprite_pix(_resolve_asset(texture))
                if pix is not None:
                    if pt.get("lit") and self.lights:
                        lr, lg, lb = self._cell_light(int(pt["x"] / self.cellsize),
                                                      int(pt["y"] / self.cellsize))
                    else:
                        lr, lg, lb = 1.0, 1.0, 1.0
                    qp.setOpacity(max(0.0, fade))
                    qp.drawPixmap(int(screenx - sh / 2.0), int(screeny - sh / 2.0),
                                  int(sh), int(sh), pix)
                    qp.setOpacity(1.0)
                    continue
            col = QColor(texture if str(texture).startswith("#") else "#ffcb6b")
            if pt.get("lit") and self.lights:
                # the exact same baked-light sample billboards use (_cell_light,
                # from _draw_sprites above) - so a lit particle shades in step
                # with the walls and objects around it, not a separate model.
                lr, lg, lb = self._cell_light(int(pt["x"] / self.cellsize),
                                              int(pt["y"] / self.cellsize))
                col = QColor(int(col.red() * lr), int(col.green() * lg),
                            int(col.blue() * lb))
            col.setAlphaF(max(0.0, fade))
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(col)
            qp.drawEllipse(int(screenx - sh / 2.0), int(screeny - sh / 2.0), int(sh), int(sh))



def _raycaster(node, scope):
    w = RaycasterWidget(node, ACTIVE_WORLD)
    p = node.props or {}
    tw, th = _parse_size(p.get("size"))
    if tw and th:
        w.setFixedSize(tw, th)
    else:
        from PyQt6.QtWidgets import QSizePolicy
        w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        w.setMinimumSize(320, 200)
    return w


def _parse_tvec_unused():
    pass


class ParticleWidget(QWidget):
    """A self-animating particle emitter.  particleSystem { count, life, speed,
    spread, direction, size, color, gravity, shape, rate, fade }."""
    def __init__(self, props):
        super().__init__()
        from PyQt6.QtCore import QTimer
        from PyQt6.QtGui import QColor

        def num(k, d):
            try:
                return float(props.get(k, d))
            except (TypeError, ValueError):
                return d
        self.count = int(num("count", 30))          # max alive / burst size
        self.rate = num("rate", 0)                   # per-second (0 => repeating burst)
        self.life = num("life", 1.2)                 # seconds each particle lives
        self.speed = num("speed", 90)                # px/sec
        self.spread = num("spread", 360)             # emission arc (degrees)
        self.direction = num("direction", -90)       # 0=right, -90=up, 90=down
        self.psize = num("size", 5)                  # particle size (px)
        self.gravity = num("gravity", 0)             # downward accel px/sec^2
        self.fade = str(props.get("fade", "true")).lower() not in ("false", "0", "no")
        self.shape = str(props.get("shape", "circle")).lower()
        self.color = QColor(props.get("color", "#6cf09a"))
        self.loop = str(props.get("loop", "true")).lower() not in ("false", "0", "no")
        self.burst = self.rate <= 0
        self.particles = []
        self._acc = 0.0
        self._did_burst = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background:transparent;")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    def _emit_one(self):
        import random
        import math
        ang = math.radians(self.direction + random.uniform(-self.spread / 2, self.spread / 2))
        sp = self.speed * random.uniform(0.5, 1.0)
        cx, cy = self.width() / 2 or 1, self.height() / 2 or 1
        self.particles.append([cx, cy, math.cos(ang) * sp, math.sin(ang) * sp,
                               0.0, self.life * random.uniform(0.7, 1.05)])

    def _tick(self):
        dt = 0.033
        if self.width() <= 1:
            return
        if self.burst:
            if not self._did_burst:
                for _ in range(self.count):
                    self._emit_one()
                self._did_burst = True
        else:
            self._acc += self.rate * dt
            while self._acc >= 1 and len(self.particles) < self.count * 4:
                self._emit_one(); self._acc -= 1
        alive = []
        for pt in self.particles:
            pt[4] += dt
            if pt[4] >= pt[5]:
                continue
            pt[3] += self.gravity * dt
            pt[0] += pt[2] * dt
            pt[1] += pt[3] * dt
            alive.append(pt)
        self.particles = alive
        if self.burst and self._did_burst and not self.particles:
            if self.loop:
                self._did_burst = False       # repeat the effect
            else:
                self._timer.stop()
                self.deleteLater()            # one-shot burst: clean up
                return
        self.update()

    def paintEvent(self, _e):
        from PyQt6.QtGui import QPainter, QColor
        from PyQt6.QtCore import QPointF
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        qp.setPen(Qt.PenStyle.NoPen)
        for x, y, vx, vy, age, life in self.particles:
            c = QColor(self.color)
            if self.fade and life:
                c.setAlphaF(max(0.0, min(1.0, 1.0 - age / life)))
            qp.setBrush(c)
            s = self.psize
            if self.shape == "square":
                qp.drawRect(int(x - s / 2), int(y - s / 2), int(s), int(s))
            else:
                qp.drawEllipse(QPointF(x, y), s / 2, s / 2)


def _particle_system(node, scope):
    p = node.props or {}
    mode = str(p.get("mode", "")).strip().lower()
    if mode in ("3d", "world"):
        # a continuous WORLD-space emitter - drawn by the raycaster's own
        # camera projection every frame (see particles_3d_step/_draw_particles_3d),
        # not a floating 2D overlay. Needs a world x/y instead of screen layout.
        def fnum(key, default):
            try:
                return float(_resolve_expr(p.get(key, default)))
            except (TypeError, ValueError):
                return default
        if ACTIVE_WORLD is not None:
            ACTIVE_WORLD.emitters_3d.append({
                "x": fnum("x", 0.0), "y": fnum("y", 0.0), "z": fnum("z", 0.0),
                "rate": fnum("rate", 20.0) or 20.0,
                "life": fnum("life", 1.2) or 1.2,
                "speed": fnum("speed", 90.0),
                "spread": fnum("spread", 360.0),
                "direction": fnum("direction", -90.0),
                "color": p.get("color", "#6cf09a"),   # a texture path works here too,
                "bounceA": fnum("bounceA", 0.0),        # same auto-detect as sprite:
                "sizeOverLife": fnum("sizeOverLife", 0.0),  # elsewhere in the file
                "size": fnum("size", 5.0) or 5.0,
                "gravity": fnum("gravity", 0.0),
                "lit": str(p.get("light", "false")).strip().lower() in ("true", "1", "yes"),
            })
        return None                      # nothing to lay out - the raycaster draws it
    w = ParticleWidget(p)
    tw, th = _parse_size(p.get("size"))
    w.setFixedSize(tw or 260, th or 200)
    w._vcr_center = p.get("center")          # so it can be centered like media
    return w


def spawn_pending(world, host):
    """Build Qt widgets for objects create()d and particle burst()s this frame."""
    dq = getattr(world, "destroy_queue", None)
    if dq:
        while dq:
            obj = dq.pop(0)
            wdg = getattr(obj, "widget", None)
            if wdg is not None:
                try:
                    wdg.setParent(None)
                    wdg.deleteLater()
                except Exception:
                    pass
                obj.widget = None
    bq = getattr(world, "burst_queue", None)
    if bq:
        while bq:
            b = bq.pop(0)
            reach = int(b["speed"] * 1.3) + int(b["count"]) + 40
            side = max(80, reach)
            pw = ParticleWidget({"count": str(b["count"]), "speed": str(b["speed"]),
                                 "life": "1.3", "spread": "360", "size": "5",
                                 "gravity": "60", "color": b["color"], "loop": "false"})
            pw.setFixedSize(side, side)
            if host is not None:
                pw.setParent(host)
            pw.move(int(b["x"] - side / 2), int(b["y"] - side / 2))
            pw.show()
    q = getattr(world, "spawn_queue", None)
    if not q:
        return
    while q:
        req = q.pop(0)
        obj = world.objects.get(req["svc"])
        if obj is None:
            continue
        tw, th = int(req["w"]), int(req["h"])
        p = {"size": f"{tw}x{th}"}
        try:
            lbl = _vcr_image(req["sprite"], p, tw, th)
        except Exception:
            lbl = None
        if lbl is None:
            continue
        obj.widget = lbl
        lbl._vcr_obj = obj
        lbl._vcr_center = None
        lbl._vcr_floating = True
        if host is not None:
            lbl.setParent(host)
        lbl.move(int(obj.x), int(obj.y))
        lbl.show()


def _color_box(p, tw, th):
    """A solid colored rectangle used when no media file is set/found."""
    from PyQt6.QtGui import QColor
    lbl = QLabel()
    w = tw or 48
    h = th or 48
    color = p.get("color") or "#3a3f44"     # default neutral box if no color given
    pix = QPixmap(w, h)
    pix.fill(QColor(color))
    lbl._vcr_base = pix
    lbl.setPixmap(pix)
    lbl.setFixedSize(pix.size())
    return lbl


def _vcr_image(name, p, tw, th):
    lbl = QLabel()
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def _apply(path, _lbl=lbl, _p=p, _tw=tw, _th=th):
        img = QImage(path) if path else QImage()
        if img.isNull():
            from PyQt6.QtGui import QColor
            w = _tw or 48; h = _th or 48
            pix = QPixmap(w, h); pix.fill(QColor(_p.get("color") or "#3a3f44"))
        else:
            out = compress_image(img, _p.get("compress") or _p.get("type"), _tw, _th)
            pix = QPixmap.fromImage(out)
            if _tw and _th:
                pix = pix.scaled(_tw, _th, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        _lbl._vcr_base = pix
        _lbl.setPixmap(pix)
        _lbl.setFixedSize(pix.size())

    _apply(_resolve_asset(name) if name else "")
    lbl._reload_media = _apply
    return lbl


def _vcr_gif(name, p, tw, th):
    path = _resolve_asset(name)
    movie = QMovie(path) if path else QMovie()
    if not path or not movie.isValid():      # no file -> just a color
        return _color_box(p, tw, th)
    lbl = QLabel()
    if tw and th:
        movie.setScaledSize(QSize(tw, th))
        lbl.setFixedSize(tw, th)
    lbl._vcr_movie = movie
    lbl.setMovie(movie)
    movie.start()
    return lbl


def _vcr_video(name, p, tw, th):
    try:
        from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
        from PyQt6.QtMultimediaWidgets import QVideoWidget
        from PyQt6.QtCore import QUrl
    except Exception:
        return _color_box(p, tw, th)         # no multimedia module -> color

    from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                                 QSlider, QLabel, QStyle)

    path = _resolve_asset(name)              # may be "" if the source is a {Var}
    box = QWidget()
    box.setStyleSheet("background:#000;")
    if tw and th:
        box.setFixedSize(tw, th)
    col = QVBoxLayout(box)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(0)

    player = QMediaPlayer(box)
    audio = QAudioOutput(box)
    player.setAudioOutput(audio)
    if path:
        player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
    vol = p.get("volume")
    audio.setVolume(0.8 if vol is None else max(0.0, min(1.0, _to_float(vol, 0.8))))
    if p.get("speed") is not None:           # playback rate (also shifts pitch)
        player.setPlaybackRate(max(0.1, min(4.0, _to_float(p.get("speed"), 1.0))))
    box._vcr_player = player
    box._vcr_audio = audio

    box._vcr_startpos = 0                     # ms to seek to on load (numeric startOn)

    # live source: reload when a {Var} in the source changes (see _track_media)
    def _reload(new_path, _pl=player, _box=box):
        try:
            if new_path:
                _pl.setSource(QUrl.fromLocalFile(os.path.abspath(new_path)))
                sp = getattr(_box, "_vcr_startpos", 0)
                if sp:
                    _pl.setPosition(int(sp))
                # only auto-play if this video is set to play (respects startOn:false)
                if getattr(_box, "_vcr_autoplay", True):
                    _pl.play()
            else:
                _pl.stop(); _pl.setSource(QUrl())
        except Exception:
            pass
    box._reload_media = _reload

    # compress: / pixelate -> process each decoded frame, like vcr.image does.
    comp = p.get("compress") or p.get("type")
    used_sink = False
    if comp is not None:
        try:
            from PyQt6.QtMultimedia import QVideoSink
            from PyQt6.QtGui import QPixmap
            display = QLabel()
            display.setStyleSheet("background:#000;")
            display.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sink = QVideoSink(box)
            player.setVideoSink(sink)

            def _on_frame(frame, _c=comp, _w=tw, _h=th, _lbl=display):
                try:
                    if not frame.isValid():
                        return
                    img = frame.toImage()
                    if img.isNull():
                        return
                    proc = compress_image(img, _c, _w, _h)
                    pm = QPixmap.fromImage(proc)
                    if _w and _h:
                        pm = pm.scaled(_w, _h, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.FastTransformation)
                    _lbl.setPixmap(pm)
                except Exception:
                    pass
            sink.videoFrameChanged.connect(_on_frame)
            col.addWidget(display, 1)
            box._vcr_sink = sink              # keep refs alive
            box._vcr_display = display
            used_sink = True
        except Exception:
            used_sink = False                # fall back to a plain video widget

    if not used_sink:
        video = QVideoWidget()
        player.setVideoOutput(video)
        col.addWidget(video, 1)

    show_controls = ("settings" in p
                     and str(p.get("settings", "true")).lower() not in ("false", "0", "no"))
    if show_controls:
        try:
            _add_video_controls(box, col, player, audio, QMediaPlayer, QSlider, QStyle)
        except Exception:
            pass                             # controls are a bonus; never break playback

    # autoplay + start position from startOn:
    #   startOn: false / 0   -> do NOT auto-play
    #   startOn: true        -> auto-play from the start
    #   startOn: <seconds>   -> seek to that time and play
    start_ms = 0
    if "startOn" in p:
        sv = str(p.get("startOn", "true")).strip().lower()
        if sv in ("false", "no", "0", ""):
            should_play = False
        elif sv in ("true", "yes"):
            should_play = True
        else:
            try:
                start_ms = int(float(sv) * 1000)
            except ValueError:
                start_ms = 0
            should_play = True
    else:
        should_play = str(p.get("paused", "false")).lower() not in ("true", "1", "yes")
    box._vcr_autoplay = should_play
    box._vcr_startpos = start_ms
    if start_ms:                                   # seek once the media has loaded
        def _seek_on_load(status, _pl=player, _ms=start_ms,
                          _Loaded=QMediaPlayer.MediaStatus.LoadedMedia):
            if status == _Loaded:
                _pl.setPosition(_ms)
        player.mediaStatusChanged.connect(_seek_on_load)
    if should_play:
        if start_ms:
            player.setPosition(start_ms)
        player.play()
    return box


def _add_video_controls(box, col, player, audio, QMediaPlayer, QSlider, QStyle):
    from PyQt6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QLabel

    st = box.style()
    ic_play = st.standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
    ic_pause = st.standardIcon(QStyle.StandardPixmap.SP_MediaPause)
    ic_vol = st.standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
    ic_mute = st.standardIcon(QStyle.StandardPixmap.SP_MediaVolumeMuted)

    bar = QWidget()
    bar.setFixedHeight(36)
    bar.setStyleSheet("background:rgba(0,0,0,0.72);")
    h = QHBoxLayout(bar)
    h.setContentsMargins(8, 4, 8, 4)
    h.setSpacing(8)

    btn_css = ("QPushButton{background:transparent;border:none;padding:2px;}"
               "QPushButton:hover{background:rgba(255,255,255,0.15);border-radius:4px;}")
    slider_css = (
        "QSlider::groove:horizontal{height:4px;background:rgba(255,255,255,0.25);border-radius:2px;}"
        "QSlider::sub-page:horizontal{height:4px;background:#ff3b30;border-radius:2px;}"
        "QSlider::handle:horizontal{width:12px;height:12px;margin:-4px 0;border-radius:6px;background:#ffffff;}")

    play = QPushButton(); play.setIcon(ic_play); play.setFixedSize(28, 26); play.setStyleSheet(btn_css)
    seek = QSlider(Qt.Orientation.Horizontal); seek.setStyleSheet(slider_css)
    tlabel = QLabel("0:00 / 0:00"); tlabel.setStyleSheet("color:#e8e8e8;font-size:11px;")
    mute = QPushButton(); mute.setIcon(ic_vol); mute.setFixedSize(26, 26); mute.setStyleSheet(btn_css)
    volsl = QSlider(Qt.Orientation.Horizontal); volsl.setFixedWidth(72)
    volsl.setRange(0, 100); volsl.setValue(int(audio.volume() * 100)); volsl.setStyleSheet(slider_css)

    for wdg in (play, seek, tlabel, mute, volsl):
        h.addWidget(wdg, 1 if wdg is seek else 0)
    col.addWidget(bar)

    def fmt(ms):
        s = max(0, int(ms / 1000))
        return f"{s // 60}:{s % 60:02d}"

    def on_dur(d):
        seek.setRange(0, max(0, int(d)))
        tlabel.setText(f"{fmt(player.position())} / {fmt(d)}")

    def on_pos(pos):
        if not seek.isSliderDown():
            seek.setValue(int(pos))
        tlabel.setText(f"{fmt(pos)} / {fmt(player.duration())}")

    def on_state(stt):
        play.setIcon(ic_pause if stt == QMediaPlayer.PlaybackState.PlayingState else ic_play)

    def toggle():
        if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            player.pause()
        else:
            player.play()

    def toggle_mute():
        audio.setMuted(not audio.isMuted())
        mute.setIcon(ic_mute if audio.isMuted() else ic_vol)

    def on_vol(val):
        audio.setVolume(val / 100.0)
        if val > 0 and audio.isMuted():
            audio.setMuted(False)
            mute.setIcon(ic_vol)

    player.durationChanged.connect(on_dur)
    player.positionChanged.connect(on_pos)
    player.playbackStateChanged.connect(on_state)
    seek.sliderMoved.connect(player.setPosition)
    seek.sliderReleased.connect(lambda: player.setPosition(seek.value()))
    play.clicked.connect(toggle)
    mute.clicked.connect(toggle_mute)
    volsl.valueChanged.connect(on_vol)
    box._controls = (play, seek, tlabel, mute, volsl)


def apply_transform(obj):
    """Apply an object's pos/rotation/scale to its Qt widget (called each frame).
    A dynamic world adds a display-only zoom + offset on top."""
    w = obj.widget
    if w is None:
        return
    wv = getattr(obj, "_world", None)
    vs = getattr(wv, "view_scale", 1.0) if wv else 1.0
    ox = getattr(wv, "view_offx", 0.0) if wv else 0.0
    oy = getattr(wv, "view_offy", 0.0) if wv else 0.0
    base = getattr(w, "_vcr_base", None)
    if base is not None:                    # image / colide: rotate+scale the pixmap
        pix = base
        tsx, tsy = obj.sx * vs, obj.sy * vs
        if abs(tsx - 1.0) > 1e-3 or abs(tsy - 1.0) > 1e-3:
            pix = pix.scaled(max(1, int(pix.width() * tsx)),
                             max(1, int(pix.height() * tsy)),
                             Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        if obj.rot:
            pix = pix.transformed(QTransform().rotate(obj.rot),
                                  Qt.TransformationMode.SmoothTransformation)
        w.setPixmap(pix)
        w.setFixedSize(pix.size())
    else:
        movie = getattr(w, "_vcr_movie", None)
        if movie is not None:
            movie.setScaledSize(QSize(max(1, int(obj.w * obj.sx * vs)),
                                      max(1, int(obj.h * obj.sy * vs))))
    if getattr(w, "_vcr_center", None):
        return                              # positioned by its container's layout
    w.move(int(ox + obj.x * vs), int(oy + obj.y * vs))


# ===========================================================================
#  child builders (shared by panels and holders)
# ===========================================================================
def build_children(spec, layout, api, registry, textgroups, fontscale, scope=None):
    tg = dict(textgroups)
    tg.update(getattr(spec, "textgroups", {}) or {})
    for node in spec.children:
        w = _build_node(node, api, registry, tg, fontscale, scope)
        if w is None:
            continue
        obj = getattr(w, "_vcr_obj", None)
        if obj is not None:
            cen = getattr(w, "_vcr_center", None)
            floating = _RENDER_DYNAMIC or getattr(w, "_vcr_floating", False)
            if floating and not cen:
                # game object / explicitly positioned -> float, engine moves it
                host = layout.parentWidget()
                if host is not None:
                    w.setParent(host)
                w.move(int(getattr(obj, "x", 0)), int(getattr(obj, "y", 0)))
                w.show()
                continue
            # UI media (image/video/gif): lay out in flow. Media reads best centred,
            # so default to horizontal-centre; an explicit center: value wins
            # (center / left / right / top... via _cell_align).
            if cen:
                align = _cell_align(cen) if isinstance(cen, str) else Qt.AlignmentFlag.AlignCenter
            else:
                align = Qt.AlignmentFlag.AlignHCenter
            layout.addWidget(w, alignment=align)
            w.show()
            continue
        layout.addWidget(w)
        align = getattr(w, "_glass_align", None)
        if align is not None:
            layout.setAlignment(w, align)


def _build_node(node, api, registry, textgroups, fontscale, scope=None):
    k = node.kind
    if k == "button":
        return _button(node, api, registry, textgroups, fontscale, scope=scope)
    if k == "link":
        return _button(node, api, registry, textgroups, fontscale, as_link=True, scope=scope)
    if k == "text":
        return _text(node, textgroups, fontscale, scope)
    if k == "label":
        ltmpl = node.label or node.props.get("text", "")
        lbl = QLabel(_interp(ltmpl, scope))
        _track_text(lbl, ltmpl)
        _scale_font(lbl, fontscale)
        return lbl
    if k in ("input", "webInput"):
        from PyQt6.QtWidgets import QSizePolicy
        edit = QLineEdit()
        default_ph = "Search or enter address" if k == "webInput" else ""
        edit.setPlaceholderText(node.label or node.props.get("text", default_ph))
        p = node.props
        raww = p.get("width")
        w = int(_num(p, "width", 0) * fontscale)
        h = int(_num(p, "height", 0) * fontscale)
        if w:
            _track(edit, raww, lambda wd, t=raww, s=fontscale: _set_fixed_w(wd, t, s))
        else:
            edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if h:
            edit.setFixedHeight(h)
        else:
            edit.setMinimumHeight(int(28 * fontscale))
        fam = _font_from_group(p.get("font"), textgroups)
        if fam:
            f = edit.font(); f.setFamily(fam); edit.setFont(f)
        _scale_font(edit, fontscale)
        bg = p.get("backgroundcolor") or p.get("background")
        edit.setStyleSheet(theme.input_field(bg))
        if k == "webInput":
            edit.returnPressed.connect(lambda e=edit: api.navigate(e.text()))
        else:
            bind = getattr(node, "bind", None)          # UsersInput = input { }
            if bind:
                world = ACTIVE_WORLD
                if world is not None and bind not in world.vars:
                    world.vars[bind] = ""

                def _sync(txt, b=bind, wd=world):
                    try:
                        if wd is not None:
                            wd.vars[b] = txt
                            refresh_var_bindings(wd.vars)
                    except Exception:
                        pass
                edit.textChanged.connect(_sync)
        return edit
    if k.lower() in ("particlesystem", "particles", "particle"):
        return _particle_system(node, scope)
    if k.lower() == "raycast":
        return _raycaster(node, scope)
    if k == "separator":
        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: rgba(255,255,255,0.15);")
        return line
    if k in ("holder", "menu", "panel", "bar", "main"):
        return HolderFrame(node, api, registry, textgroups, fontscale, scope)
    if k.startswith("vcr_"):
        return _vcr(node, scope)
    note = QLabel(f"[unknown element: {k}]")
    note.setStyleSheet("color:#ff6b6b;")
    return note


def _action_handler(props, api):
    def handler():
        did_set = False
        if "set" in props:
            api.run_set(props["set"]); did_set = True
        if "do" in props:
            did_set = api.run_do(props["do"]) or did_set
        if "call" in props:                      # call: alias.function (package import)
            api.pkg_call(fn=props["call"])
        if "url" in props:
            api.navigate(props["url"])
        elif "js" in props:
            api.js(code=props["js"])
        elif "action" in props:
            kwargs = {kk: vv for kk, vv in props.items()
                      if kk not in ("action", "url", "js", "if", "set", "do")}
            api.call(props["action"], **kwargs)
    return handler


def _button(node, api, registry, textgroups, fontscale, as_link=False, scope=None):
    tmpl = node.label or node.props.get("text", "button")
    btn = QPushButton(_interp(tmpl, scope))
    _track_text(btn, tmpl)
    style = node.style or {}
    bg = style.get("color")
    raww = style.get("width")
    w = _num(style, "width", 0)
    h = _num(style, "height", 0)
    if w:
        _track(btn, raww, lambda wd, t=raww, s=fontscale: _set_fixed_w(wd, t, s))
    if h:
        btn.setFixedHeight(int(h * fontscale))
    fam = _font_from_group(style.get("textgroup") or style.get("font"), textgroups)
    if fam:
        f = btn.font(); f.setFamily(fam); btn.setFont(f)
    cen = node.props.get("center") or style.get("center")
    talign = "center"
    if cen:
        c = str(cen).strip().lower()
        talign = "center" if c in ("center", "middle", "centre") else (
            "right" if c in ("right", "end") else "left")
    spr = _sprite_css(node)
    if spr:
        # custom sprite stretches to the button box -> auto-scales on resize.
        btn.setStyleSheet(
            f"QPushButton{{{spr}color:{theme.TEXT};border-radius:6px;padding:6px 12px;"
            f"text-align:{talign};font-weight:600;}}")
    else:
        btn.setStyleSheet(theme.button(face=bg, talign=talign))
    if as_link and "action" not in node.props and "url" not in node.props:
        node.props["url"] = node.props.get("url", "")
    _scale_font(btn, fontscale)
    btn.clicked.connect(_action_handler(node.props, api))
    return btn


def _text(node, textgroups, fontscale, scope=None):
    content = node.label or node.props.get("text", node.props.get("content", ""))
    lbl = QLabel(_interp(content, scope))
    _track_text(lbl, content)
    p = node.props
    color = p.get("color", "#e6e6e6")
    bg = p.get("backgroundcolor") or p.get("background")
    raww = p.get("width")
    w = _num(p, "width", 0)
    h = _num(p, "height", 0)
    if w:
        _track(lbl, raww, lambda wd, t=raww, s=fontscale: _set_fixed_w(wd, t, s))
    if h:
        lbl.setFixedHeight(int(h * fontscale))
    fam = _font_from_group(p.get("font"), textgroups)
    if fam:
        f = lbl.font(); f.setFamily(fam); lbl.setFont(f)
    css = f"color:{_qss_color(color)};"
    if bg:
        css += f"background:{_qss_color(bg)};padding:4px 6px;border-radius:6px;"
    spr = _sprite_css(node)
    if spr:
        css = f"{spr}color:{color};border-radius:6px;padding:4px 6px;"
    lbl.setStyleSheet(css)
    lbl.setWordWrap(True)
    _scale_font(lbl, fontscale)
    cen = p.get("center")
    if cen:
        lbl.setAlignment(_h_align(cen) | Qt.AlignmentFlag.AlignVCenter)
        lbl._glass_align = _h_align(cen)
    return lbl


# ===========================================================================
#  inline container: holders + nested menus
# ===========================================================================
class HolderFrame(QFrame):
    def __init__(self, node, api, registry, textgroups, fontscale, scope=None):
        super().__init__()
        node, local = _flatten(node, scope)
        self.node = node
        params = node.params or {}
        props = node.props or {}

        name = params.get("name") or props.get("name") or node.label
        bg = params.get("backgroundColor") or params.get("background") or props.get("background") or "#161b22"
        opacity = params.get("opacity") or props.get("opacity")
        outline = _bool(params.get("outline"), False)
        ocolor = params.get("outlinecolor", "#6cf09a")
        othick = _num(params, "outlineThickness", 1)

        spr = _sprite_css(node)
        if spr:
            self.setStyleSheet(
                f"HolderFrame{{{spr}border-radius:6px;}}QLabel{{color:{theme.TEXT};}}")
        elif outline:
            self.setStyleSheet(
                f"HolderFrame{{background:{_qss_color(bg)};border:{othick}px solid {_qss_color(ocolor)};"
                f"border-radius:6px;}}QLabel{{color:#e6e6e6;}}")
        else:
            self.setStyleSheet(
                f"HolderFrame{{{theme.frost(_qss_color(bg))}}}QLabel{{color:{theme.TEXT};}}")
        if opacity:
            _apply_opacity(self, opacity)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        sx, sy, sf = _scale_of(node)

        title = props.get("title")
        if title:
            t = QLabel(title); t.setStyleSheet("font-weight:600;color:#cdd6e0;")
            _scale_font(t, fontscale * sf)
            _apply_title_style(t, props.get("title_style"), textgroups, fontscale * sf)
            lay.addWidget(t)

        scrollable = node.has("scroll") or node.has("scrollable")
        size = params.get("size") or props.get("size")
        if scrollable:
            from PyQt6.QtWidgets import QScrollArea
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            # match the editor: a styled scroll area with the default scrollbar
            # (no custom QScrollBar rules) so both look identical
            area.setStyleSheet("QScrollArea{background:transparent;}")
            content = QWidget()
            content.setStyleSheet(f"background:{_qss_color(bg)};")   # match the holder, not Qt's default
            clay = QVBoxLayout(content)
            clay.setContentsMargins(2, 2, 6, 2)
            clay.setSpacing(6)
            build_children(node, clay, api, registry, textgroups, fontscale * sf, local)
            _apply_center(clay, node.center)
            clay.addStretch(1)
            area.setWidget(content)
            lay.addWidget(area, 1)
        else:
            build_children(node, lay, api, registry, textgroups, fontscale * sf, local)
            _apply_center(lay, node.center)

        # autosize: fit content instead of a fixed box (use width from size if given)
        autosize = (not scrollable and (
            node.has("autosize") or node.has("autogrow") or node.has("fit")
            or str(size or "").strip().lower() == "auto"))
        if scrollable:
            # a scroll box needs a bounded height; width/height from size, else defaults
            wtok, htok = _size_tokens(size)
            if htok:
                _track(self, htok, lambda wd, t=htok, s=sy: _set_fixed_h(wd, t, s))
            else:
                self.setMaximumHeight(int(260 * sy))
            if wtok:
                _track(self, wtok, lambda wd, t=wtok, s=sx: _set_fixed_w(wd, t, s))
        elif autosize:
            wtok, _ = _size_tokens(size)
            if wtok:
                _track(self, wtok, lambda wd, t=wtok, s=sx: _set_fixed_w(wd, t, s))
        elif size:
            wtok, htok = _size_tokens(size)
            if wtok and htok:
                _track(self, size,
                       lambda wd, a=wtok, b=htok, p=sx, q=sy: _set_min_and_w(wd, a, b, p, q))
            elif wtok:
                _track(self, wtok, lambda wd, t=wtok, s=sx: _set_fixed_w(wd, t, s))

        if name:
            registry[name] = self
        hidden = node.has("hidden") or _bool(params.get("hidden")) or \
            (params.get("visible", "true").lower() == "false")
        self.setVisible(not hidden)

    def _notify_panel(self):
        p = self.parentWidget()
        while p is not None and not isinstance(p, GlassPanel):
            p = p.parentWidget()
        if isinstance(p, GlassPanel):
            p.request_fit()

    def showEvent(self, e):
        super().showEvent(e)
        self._notify_panel()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._notify_panel()


# ===========================================================================
#  floating top-level container
# ===========================================================================
class GlassPanel(QWidget):
    def __init__(self, spec, api, parent, registry, host_textgroups=None, scope=None):
        super().__init__(parent)
        spec, local = _flatten(spec, scope)
        self.spec = spec
        self.api = api
        self._drag = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # position memory across UI reloads (opt-in via `remember`/`persist` trait)
        self._persist = spec.has("remember") or spec.has("persist")
        self._pos_key = f"{spec.scope}.{spec.component}"
        win = getattr(api, "window", None)
        self._pos_store = getattr(win, "saved_positions", None) if win else None

        props = spec.props or {}
        # stable id so a user-closed panel stays closed across tabs / re-renders
        self.panel_key = f"{spec.scope}.{spec.component}:{props.get('title', '')}"
        bg = props.get("background", "#14181d")
        fg = props.get("color", "#e6e6e6")
        radius = _num(props, "radius", 10)

        self.setStyleSheet(f"""
            GlassPanel {{ {theme.frost(_qss_color(bg))} color:{_qss_color(fg, "#e6e6e6")}; }}
            QPushButton {{ color:{_qss_color(fg, "#e6e6e6")}; }}
            QLabel {{ color:{_qss_color(fg, "#e6e6e6")}; }}
            QLineEdit {{ {theme.input_field()} }}
        """)
        if props.get("opacity"):
            _apply_opacity(self, props["opacity"])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(6)

        sx, sy, sf = _scale_of(spec)

        title = props.get("title")
        if title or spec.has("closable"):
            row = QHBoxLayout()
            lbl = QLabel(title or ""); lbl.setStyleSheet("font-weight:600;padding:2px 0;")
            _scale_font(lbl, sf)
            _apply_title_style(lbl, props.get("title_style"),
                               dict(host_textgroups or {}), sf)
            row.addWidget(lbl, 1)
            if spec.has("closable"):
                x = QPushButton("\u2715"); x.setFixedSize(int(22 * sf), int(22 * sf))
                x.setStyleSheet("text-align:center;padding:0;")
                _scale_font(x, sf)
                x.clicked.connect(self._close_panel); row.addWidget(x, 0)
            outer.addLayout(row)

        tg = dict(host_textgroups or {})
        build_children(spec, outer, api, registry, tg, sf, local)
        _apply_center(outer, spec.center)

        if spec.has("resizable"):
            gr = QHBoxLayout(); gr.addStretch(1); gr.addWidget(QSizeGrip(self))
            outer.addLayout(gr)

        raww = props.get("width")
        w = int(_num(props, "width", 240) * sx)
        h = int(_num(props, "height", 0) * sy)
        if spec.has("resizable"):
            # real resizing: a minimum, then size to content height (or given height)
            self.setMinimumWidth(160)
            self.adjustSize()
            self.resize(w, h if h else max(self.sizeHint().height(), self.height()))
        elif h:
            self.resize(w, h)
        else:
            _track(self, raww, lambda wd, t=raww, s=sx:
                   wd.setFixedWidth(int(_to_int(t, 240) * s)))
        # position: a remembered one wins over the file's x/y so the menu stays
        # put across UI reloads and page reloads.
        saved = None
        if self._persist and self._pos_store is not None:
            saved = self._pos_store.get(self._pos_key)
        if saved is not None:
            self.move(saved[0], saved[1])
        else:
            self.move(_num(props, "x", 30), _num(props, "y", 30))
        if spec.has("pinned") or spec.has("ontop"):
            self.raise_()

    def _close_panel(self):
        self.hide()
        win = getattr(self.api, "window", None)
        closed = getattr(win, "closed_panels", None)
        if closed is not None:
            closed.add(self.panel_key)

    def _save_position(self):
        if self._persist and self._pos_store is not None:
            p = self.pos()
            self._pos_store[self._pos_key] = (p.x(), p.y())

    def request_fit(self):
        # deferred so it runs after the toggled child's geometry settles
        QTimer.singleShot(0, self.fit_to_content)

    def fit_to_content(self):
        lay = self.layout()
        if lay is None:
            return
        target = max(lay.sizeHint().height(), lay.minimumSize().height())
        if target and target != self.height():
            self.resize(self.width(), target)   # keep width, grow/shrink height

    # moveable drag
    def mousePressEvent(self, e):
        if self.spec.has("moveable") and e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None and self.spec.has("moveable"):
            self.move(self.mapToParent(e.position().toPoint()) - self._drag)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._save_position()
        self._drag = None
        super().mouseReleaseEvent(e)


# ===========================================================================
#  fullscreen main screen  (.main { menu.full })
# ===========================================================================
def _event_key_name(ev):
    """Map a Qt key event to the simple name used by input.getHeld/getClick."""
    from PyQt6.QtCore import Qt as _Qt
    special = {
        _Qt.Key.Key_Left: "left", _Qt.Key.Key_Right: "right",
        _Qt.Key.Key_Up: "up", _Qt.Key.Key_Down: "down",
        _Qt.Key.Key_Space: "space", _Qt.Key.Key_Return: "enter",
        _Qt.Key.Key_Enter: "enter", _Qt.Key.Key_Escape: "escape",
        _Qt.Key.Key_Shift: "shift", _Qt.Key.Key_Control: "ctrl",
        _Qt.Key.Key_Tab: "tab", _Qt.Key.Key_Backspace: "backspace",
    }
    k = ev.key()
    if k in special:
        return special[k]
    t = ev.text()
    return t.lower() if t and t.strip() else ""


class FullScreenPanel(QWidget):
    def __init__(self, spec, api, parent, registry, host_textgroups=None, scope=None):
        super().__init__(parent)
        spec, local = _flatten(spec, scope)
        self.spec = spec
        self.api = api
        self._parent = parent
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # accept keyboard focus so key input reaches Qt (not the WebEngine child
        # process, which otherwise swallows keys and breaks input.getHeld/getClick)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._init_visuals(spec, local, api, registry, host_textgroups, parent)

    # ---- keyboard: grab so keys reach Qt even over a WebEngine view ---------
    def _fs_world(self):
        win = getattr(self.api, "window", None)
        return getattr(win, "world", None) if win else None

    def focusInEvent(self, e):
        w = self._fs_world()
        if w is not None and getattr(w, "dynamic", False):
            self.grabKeyboard()          # route ALL keys here, past the WebEngine
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self.releaseKeyboard()           # let the address bar / other fields type
        super().focusOutEvent(e)

    def hideEvent(self, e):
        self.releaseKeyboard()
        super().hideEvent(e)

    def mousePressEvent(self, e):
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        w = self._fs_world()
        if w is not None and not e.isAutoRepeat():
            n = _event_key_name(e)
            if n:
                w.input.key_down(n)
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        w = self._fs_world()
        if w is not None and not e.isAutoRepeat():
            n = _event_key_name(e)
            if n:
                w.input.key_up(n)
        super().keyReleaseEvent(e)

    def _init_visuals(self, spec, local, api, registry, host_textgroups, parent):
        props = spec.props or {}
        bg = props.get("background", "#0d1117")
        fg = props.get("color", "#e6e6e6")
        css = f"FullScreenPanel{{background:{_qss_color(bg, '#0d1117')};"
        img = props.get("image")
        if img:
            css += (f"background-image:url('{_resolve_asset(img)}');"
                    "background-position:center;background-repeat:no-repeat;")
        css += "}"
        self.setStyleSheet(css + f" QLabel{{color:{_qss_color(fg, '#e6e6e6')};}}"
                           " QLineEdit{background:rgba(0,0,0,0.35);color:#e6e6e6;"
                           "border:1px solid rgba(255,255,255,0.18);border-radius:6px;padding:6px 9px;}")
        if props.get("opacity"):
            _apply_opacity(self, props["opacity"])

        sx, sy, sf = _scale_of(spec)

        inner = QWidget()
        ilay = QVBoxLayout(inner)
        ilay.setContentsMargins(0, 0, 0, 0)
        ilay.setSpacing(10)
        title = props.get("title")
        if title:
            t = QLabel(title)
            tf = t.font(); tf.setPointSizeF((tf.pointSizeF() if tf.pointSizeF() > 0 else 10) * 1.8 * sf)
            tf.setBold(True); t.setFont(tf)
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _apply_title_style(t, props.get("title_style"),
                               dict(host_textgroups or {}), sf)
            ilay.addWidget(t)
        tg = dict(host_textgroups or {})
        build_children(spec, ilay, api, registry, tg, sf, local)
        _apply_center(ilay, spec.center)

        grid = QGridLayout(self)
        grid.setContentsMargins(20, 20, 20, 20)
        for r in range(3):
            grid.setRowStretch(r, 1)
        for c in range(3):
            grid.setColumnStretch(c, 1)
        cell = _CELL.get((spec.center or "center").lower(), (1, 1))
        grid.addWidget(inner, cell[0], cell[1], _cell_align(spec.center))

        if parent is not None:
            self.setGeometry(parent.rect())
            parent.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if obj is self._parent and ev.type() == QEvent.Type.Resize:
            self.setGeometry(self._parent.rect())
        return False


# ===========================================================================
def _resolve_bool(ref, scopes):
    r = str(ref).strip()
    low = r.lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off", ""):
        return False
    for sc in scopes:
        if r in sc:
            return bool(sc[r])
    return False


def _apply_grabs(rules, registry, scope=None):
    doc_vars = dict(getattr(rules, "variables", {}) or {})
    if scope:
        doc_vars.update(scope)
    for name, ref in (getattr(rules, "grabs", []) or []):
        if name in registry and _resolve_bool(ref, [doc_vars]):
            registry[name].setVisible(True)

    def walk(spec):
        sv = getattr(spec, "variables", {}) or {}
        for name, ref in (getattr(spec, "grabs", []) or []):
            if name in registry and _resolve_bool(ref, [sv, doc_vars]):
                registry[name].setVisible(True)
        for ch in (getattr(spec, "children", []) or []):
            walk(ch)
    for rule in rules:
        walk(rule)


def render_rules(rules, api, parent, host=None, registry=None, variables=None):
    """Render matching top-level rules. Returns (panels, registry)."""
    global ACTIVE_WORLD
    _SCREEN_BINDINGS.clear()        # widgets from a previous render are gone
    _VAR_BINDINGS.clear()
    _MEDIA_BINDINGS.clear()
    if registry is None:
        registry = {}
    if variables is not None:
        scope = dict(variables)
    else:
        scope = dict(getattr(rules, "variables", {}) or {})
    panels = []
    win = getattr(api, "window", None)
    closed = getattr(win, "closed_panels", set()) if win else set()

    import engine
    world = engine.World()
    world.apply_override_limits(getattr(rules, "override_limits", {}) or {})
    world.vars = variables if variables is not None else scope
    # A "game" (menu.dynamic) treats vcr objects as free-floating, engine-moved
    # widgets. A plain UI menu (full/ui) that merely has an update{} for logic must
    # NOT: its vcr.video/image lay out inside their holders like normal widgets.
    world.dynamic = any(r.has("dynamic") for r in rules if hasattr(r, "has"))
    world.snippets = dict(getattr(rules, "snippets", {}) or {})
    global _RENDER_DYNAMIC
    _RENDER_DYNAMIC = world.dynamic
    prev_world, ACTIVE_WORLD = ACTIVE_WORLD, world   # vcr.* nodes register here

    # *.postEffect profiles aren't visual panels - compile them separately.
    # Each is run once through the normal script machinery with a special
    # "compiling" flag set, so every postEffects.X(...) call inside effect{}
    # appends to a list instead of doing anything live - see engine.py's
    # _kwcall dispatch for the postEffects namespace.
    world.post_profiles = {}
    for rule in rules:
        if rule.component != "postEffect":
            continue
        props = rule.props or {}
        name = str(props.get("post.name", "")).strip()
        if name.startswith('"') and name.endswith('"') and len(name) >= 2:
            name = name[1:-1]
        if not name:
            continue
        cache_raw = str(props.get("post.cache", "true")).strip().lower()
        cache = cache_raw not in ("false", "0", "no")
        try:
            quality = max(0, min(100, int(float(props.get("post.quality", 100)))))
        except (TypeError, ValueError):
            quality = 100
        world._compiling_post = []
        try:
            engine.run_script(getattr(rule, "effect_script", "") or "", world)
        except Exception:
            pass
        effects = world._compiling_post
        world._compiling_post = None
        world.post_profiles[name] = {
            "name": name, "cache": cache, "quality": quality, "effects": effects,
        }

    for rule in rules:
        if rule.component not in ("menu", "panel", "bar", "holder", "main"):
            continue
        if rule.scope not in ("*", "websitename") and host:
            if rule.scope != host and not host.endswith("." + rule.scope):
                continue

        fullscreen = (rule.component == "main"
                      and (rule.mode == "full" or rule.has("full")))
        if fullscreen:
            panel = FullScreenPanel(rule, api, parent, registry, rule.textgroups, scope)
        else:
            if rule.component == "main" and "moveable" not in rule.traits:
                rule.traits = rule.traits + ["moveable"]   # ui main is draggable
            panel = GlassPanel(rule, api, parent, registry, rule.textgroups, scope)
        key = getattr(panel, "panel_key", None)
        if key is not None and key in closed:
            panel.setVisible(False)        # user closed it earlier - keep it closed
        else:
            panel.show()
        panels.append(panel)

    # collect setup/update scripts from the document and every container
    setup = getattr(rules, "setup_script", "") or ""
    upd = getattr(rules, "update_script", "") or ""
    for rule in rules:
        setup += getattr(rule, "setup_script", "") or ""
        upd += getattr(rule, "update_script", "") or ""
    world.setup_script = setup
    world.update_script = upd
    ACTIVE_WORLD = prev_world

    if win is not None:
        if world.active() or getattr(world, "dynamic", False):
            # game objects must live on the full-size panel, not the centered
            # content box, or they'd be clipped. Find the screen-filling panel.
            game_host = None
            for p in panels:
                if isinstance(p, FullScreenPanel):
                    game_host = p
                    break
            if game_host is None and panels:
                game_host = panels[0]
            world.host = game_host
            if world.dynamic:
                # game objects live on the full-size panel so they aren't clipped
                for o in world.objects.values():
                    if o.widget is None:
                        continue
                    if game_host is not None:
                        o.widget.setParent(game_host)
                        o.widget.move(int(o.x), int(o.y))
                    o.widget.show()
                    o.widget.raise_()
            win.world = world
        else:
            win.world = None

    _apply_grabs(rules, registry, scope)
    return panels, registry
