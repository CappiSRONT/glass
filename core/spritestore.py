"""Sprite storage and .glass source editing for the sprite creator.

Sprites are PNGs saved under <project>/sprites/. A sprite is attached to a UI
element by a `sprite: "sprites/<name>.png"` property in the .glass source, so it
is persistent, visible, and the renderer just reads it (stretching it to the
element, which makes it auto-scale when the element is resized).
"""

import os
import re

# elements that can carry a sprite
NAMED_KINDS = ("button", "text", "label", "holder", "image", "input", "panel")


def sprites_dir(project_dir):
    d = os.path.join(project_dir, "sprites")
    os.makedirs(d, exist_ok=True)
    return d


def sprite_rel(name):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name).strip("._- ") or "sprite"
    return f"sprites/{safe}.png"


def sprite_abs(project_dir, name):
    return os.path.join(project_dir, sprite_rel(name).replace("/", os.sep))


def save_image(qimage, project_dir, name):
    sprites_dir(project_dir)
    path = sprite_abs(project_dir, name)
    qimage.save(path, "PNG")
    return sprite_rel(name)


def load_image(project_dir, name):
    from PyQt6.QtGui import QImage
    path = sprite_abs(project_dir, name)
    if os.path.exists(path):
        img = QImage(path)
        if not img.isNull():
            return img
    return None


def default_glass_image(w, h):
    """Bake the frosted-glass button texture into an editable QImage."""
    import theme
    from PyQt6.QtGui import QImage, QPainter, QColor, QLinearGradient, QPen
    from PyQt6.QtCore import Qt, QRectF
    w = max(8, int(w)); h = max(8, int(h))
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, 0, h)
    g.setColorAt(0.0, QColor(255, 255, 255, 18))
    g.setColorAt(0.12, QColor(16, 20, 27, 255))
    g.setColorAt(1.0, QColor(16, 20, 27, 255))
    p.setBrush(g); p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 6, 6)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 40), 1))
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 6, 6)
    p.setPen(QPen(QColor(255, 255, 255, 28), 1))
    p.drawLine(6, 1, w - 6, 1)             # lit top edge
    p.end()
    return img



# ---- scanning + editing the .glass source (position based) ----------------
# Match any element keyword at the start of a line, followed by a string,
# a "{" block or a "(" param group - covers:  text "x" {..} ,
# button { {style} "label" } {props} ,  holder ( .. ) { .. }
_DECL_RE = re.compile(
    r'^[ \t]*(?P<kind>' + "|".join(NAMED_KINDS) + r')\b[ \t]+(?=["{(])',
    re.MULTILINE)


def _extent(text, pos):
    """End offset of the element declaration that starts at `pos`
    (spans all its balanced {}/() groups; ends at the first newline
    that is not inside a group or string)."""
    depth = 0
    instr = False
    i = pos
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            instr = not instr
        elif not instr:
            if c in "{(":
                depth += 1
            elif c in ")}":
                depth -= 1
            elif c == "\n" and depth <= 0:
                return i
        i += 1
    return n


def _last_brace_group(text, start, end):
    """(open_idx, close_idx) of the LAST top-level {..} group in [start,end)."""
    depth = 0
    instr = False
    last = None
    open_at = -1
    i = start
    while i < end:
        c = text[i]
        if c == '"':
            instr = not instr
        elif not instr:
            if c == "{":
                if depth == 0:
                    open_at = i
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and open_at >= 0:
                    last = (open_at, i)
        i += 1
    return last


def scan_elements(text):
    """[{kind, name, line, pos, has_sprite}] for every element declaration."""
    out = []
    for m in _DECL_RE.finditer(text):
        pos = m.start()
        kind = m.group("kind")
        end = _extent(text, pos)
        seg = text[pos:end]
        q = re.search(r'"([^"]*)"', seg)        # label = first quoted string
        line = text.count("\n", 0, pos)
        name = q.group(1).strip() if q and q.group(1).strip() else f"{kind}_{line + 1}"
        has = bool(re.search(r'\bsprite\s*:', seg))
        out.append({"kind": kind, "name": name, "line": line,
                    "pos": pos, "has_sprite": has})
    return out


def _element_at(text, pos):
    for e in scan_elements(text):
        if e["pos"] == pos:
            return e
    # tolerate small drift: nearest declaration at/just before pos
    cands = [e for e in scan_elements(text) if e["pos"] <= pos]
    return cands[-1] if cands else None


def element_name_at(text, pos):
    e = _element_at(text, pos)
    return e["name"] if e else "sprite"


def element_size_at(text, pos, default=(200, 48)):
    end = _extent(text, pos)
    seg = text[pos:end]
    s = re.search(r'size\s*:\s*(\d+)\s*[xX]\s*(\d+)', seg)
    if s:
        return (int(s.group(1)), int(s.group(2)))
    w = re.search(r'\bwidth\s*:\s*(\d+)', seg)
    h = re.search(r'\bheight\s*:\s*(\d+)', seg)
    return (int(w.group(1)) if w else default[0],
            int(h.group(1)) if h else default[1])


def sprite_resolution(w, h, maxside=64, minside=8):
    """Pixel-art resolution preserving aspect, capped for comfortable editing."""
    w = max(1, int(w)); h = max(1, int(h))
    scale = min(1.0, maxside / max(w, h))
    return (max(minside, round(w * scale)), max(minside, round(h * scale)))


def set_sprite_at(text, pos, rel):
    """Insert/update `sprite: "rel"` in the element's last {..} block."""
    end = _extent(text, pos)
    grp = _last_brace_group(text, pos, end)
    if not grp:
        # no body -> append one
        return text[:end] + ' { sprite: "%s" }' % rel + text[end:]
    b0, b1 = grp
    body = text[b0:b1 + 1]
    if re.search(r'\bsprite\s*:', body):
        new = re.sub(r'sprite\s*:\s*"[^"]*"', 'sprite: "%s"' % rel, body, count=1)
    else:
        inner = body[1:].lstrip()
        sep = "" if inner.startswith("}") else ", "
        new = '{ sprite: "%s"%s%s' % (rel, sep, inner)
    return text[:b0] + new + text[b1 + 1:]


def remove_sprite_at(text, pos):
    end = _extent(text, pos)
    grp = _last_brace_group(text, pos, end)
    if not grp:
        return text
    b0, b1 = grp
    body = text[b0:b1 + 1]
    new = re.sub(r'\s*sprite\s*:\s*"[^"]*"\s*,?', "", body, count=1)
    new = re.sub(r'\{\s*,', "{", new)
    return text[:b0] + new + text[b1 + 1:]
