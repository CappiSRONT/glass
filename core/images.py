"""Glass icons - the ONE place to set the app icons.

Change the two paths below to use your own images (any size; PNG recommended).
They are always downscaled to ICON_SIZE for the title bar / taskbar, so even a
big 100x100 (or larger) image is shrunk to a crisp small icon automatically.

    BROWSER_ICON = "assets/glass_icon.png"
    EDITOR_ICON  = "assets/editor_icon.png"

Paths are relative to the Glass folder (or absolute).
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- set your icons here -------------------------------------------------
BROWSER_ICON = "assets/glass_icon.png"
EDITOR_ICON = "assets/editor_icon.png"
ICON_SIZE = 32          # everything is downscaled to this many pixels

# ---- per-button icons (settable; any size, always downscaled) ------------
BUTTON_ICON_SIZE = 16   # pixel-art icons render 1:1 (nearest-neighbor) = crisp
BUTTON_ICONS = {
    # browser chrome
    "back": "assets/icons/back.png",
    "forward": "assets/icons/forward.png",
    "reload": "assets/icons/reload.png",
    "source": "assets/icons/source.png",
    "log": "assets/icons/log.png",
    "ui": "assets/icons/ui.png",
    "settings": "assets/icons/settings.png",
    "edit": "assets/icons/edit.png",
    "history": "assets/icons/history.png",
    # editor toolbar
    "new": "assets/icons/new.png",
    "open": "assets/icons/open.png",
    "save": "assets/icons/save.png",
    "saveas": "assets/icons/saveas.png",
    "sprites": "assets/icons/sprites.png",
    "project": "assets/icons/project.png",
    "edits": "assets/icons/edits.png",
    "tutorial": "assets/icons/tutorial.png",
    "assistant": "assets/icons/assistant.png",
    # sprite-editor tools
    "tool_pencil": "assets/icons/tool_pencil.png",
    "tool_eraser": "assets/icons/tool_eraser.png",
    "tool_fill": "assets/icons/tool_fill.png",
    "tool_pick": "assets/icons/tool_pick.png",
}
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------


def _abs(rel):
    return rel if os.path.isabs(rel) else os.path.join(HERE, rel)


def load_pixmap(which="browser", size=None):
    """Return a downscaled QPixmap for the icon (always shrunk to `size`)."""
    from PyQt6.QtGui import QPixmap
    from PyQt6.QtCore import Qt
    size = int(size or ICON_SIZE)
    rel = BROWSER_ICON if which == "browser" else EDITOR_ICON
    pm = QPixmap(_abs(rel))
    if pm.isNull():
        pm = _default_pixmap(which, 256)
    return pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)


def load_icon(which="browser", size=None):
    """A QIcon with several downscaled sizes so the title bar AND the Windows
    taskbar both render crisply (all shrunk from the source image)."""
    from PyQt6.QtGui import QIcon
    if size is not None:                       # explicit single size if asked
        return QIcon(load_pixmap(which, size))
    icon = QIcon()
    for s in (16, 24, 32, 48, 64):
        icon.addPixmap(load_pixmap(which, s))
    return icon


def button_icon(name, size=None):
    """A downscaled QIcon for a built-in button, or an empty icon if unset/missing
    (so the button keeps its text label)."""
    from PyQt6.QtGui import QIcon, QPixmap
    from PyQt6.QtCore import Qt
    size = int(size or BUTTON_ICON_SIZE)
    rel = BUTTON_ICONS.get(name)
    if rel:
        pm = QPixmap(_abs(rel))
        if not pm.isNull():
            return QIcon(pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.FastTransformation))  # crisp pixels
    return QIcon()


def _default_pixmap(which, S=256):
    """Drawn fallback if the configured file is missing."""
    from PyQt6.QtGui import (QPixmap, QPainter, QColor, QLinearGradient, QBrush,
                             QPen, QFont, QPolygonF)
    from PyQt6.QtCore import Qt, QRectF, QPointF
    pm = QPixmap(S, S)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c1, c2 = ("#0e1419", "#0a1a16") if which == "browser" else ("#141019", "#1a1426")
    g = QLinearGradient(0, 0, S, S); g.setColorAt(0, QColor(c1)); g.setColorAt(1, QColor(c2))
    p.setBrush(QBrush(g)); p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(S * 0.06, S * 0.06, S * 0.88, S * 0.88), S * 0.18, S * 0.18)
    if which == "browser":
        pane = QPolygonF([QPointF(S * 0.34, S * 0.24), QPointF(S * 0.74, S * 0.30),
                          QPointF(S * 0.66, S * 0.76), QPointF(S * 0.26, S * 0.70)])
        gr = QLinearGradient(S * 0.3, S * 0.2, S * 0.7, S * 0.8)
        gr.setColorAt(0, QColor(108, 240, 154, 150)); gr.setColorAt(1, QColor(108, 240, 154, 40))
        p.setBrush(QBrush(gr)); p.setPen(QPen(QColor(108, 240, 154, 220), S * 0.012))
        p.drawPolygon(pane)
    else:
        f = QFont("DejaVu Sans"); f.setPointSizeF(S * 0.30); f.setBold(True); p.setFont(f)
        p.setPen(QColor("#c792ea"))
        p.drawText(QRectF(0, 0, S, S), Qt.AlignmentFlag.AlignCenter, "</>")
    p.end()
    return pm
