"""A small MS-Paint-style sprite editor for Glass UI element textures."""

from PyQt6.QtWidgets import (
    QDialog, QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QLabel,
    QScrollArea, QColorDialog, QButtonGroup, QSpinBox, QCheckBox)
from PyQt6.QtGui import QImage, QPainter, QColor, QPen
from PyQt6.QtCore import Qt, QSize

import spritestore

PALETTE = ["#000000", "#ffffff", "#e7edf3", "#8b95a1", "#6cf09a", "#3fbf8c",
           "#7a3cff", "#ffd23c", "#ff5d5d", "#3ca7ff", "#10141b", "#1c2530"]


class PixelCanvas(QWidget):
    def __init__(self, img, scale=10):
        super().__init__()
        self.img = img
        self.scale = scale
        self.brush = 1
        self.tool = "pencil"
        self.color = QColor("#6cf09a")
        self.setFixedSize(img.width() * scale, img.height() * scale)
        self.setMouseTracking(True)

    # ---- model -----------------------------------------------------------
    def set_image(self, img):
        self.img = img
        self.setFixedSize(img.width() * self.scale, img.height() * self.scale)
        self.update()

    def set_scale(self, scale):
        self.scale = max(2, min(40, int(scale)))
        self.setFixedSize(self.img.width() * self.scale, self.img.height() * self.scale)
        self.update()

    def _pix(self, pos):
        x = int(pos.x() // self.scale); y = int(pos.y() // self.scale)
        if 0 <= x < self.img.width() and 0 <= y < self.img.height():
            return x, y
        return None

    def _stamp(self, x, y, color):
        n = max(1, self.brush); off = (n - 1) // 2
        for dx in range(n):
            for dy in range(n):
                px, py = x - off + dx, y - off + dy
                if 0 <= px < self.img.width() and 0 <= py < self.img.height():
                    self.img.setPixelColor(px, py, color)

    def _apply(self, x, y):
        if self.tool == "pencil":
            self._stamp(x, y, self.color)
        elif self.tool == "eraser":
            self._stamp(x, y, QColor(0, 0, 0, 0))
        elif self.tool == "pick":
            self.color = QColor(self.img.pixelColor(x, y))
        elif self.tool == "fill":
            self._flood(x, y, self.color)
        self.update()

    def _flood(self, x, y, new):
        target = self.img.pixelColor(x, y)
        if target == new:
            return
        w, h = self.img.width(), self.img.height()
        stack = [(x, y)]
        seen = set()
        while stack:
            cx, cy = stack.pop()
            if (cx, cy) in seen or not (0 <= cx < w and 0 <= cy < h):
                continue
            seen.add((cx, cy))
            if self.img.pixelColor(cx, cy) != target:
                continue
            self.img.setPixelColor(cx, cy, new)
            stack += [(cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)]

    # ---- events ----------------------------------------------------------
    def mousePressEvent(self, e):
        p = self._pix(e.position())
        if p:
            self._apply(*p)

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton and self.tool in ("pencil", "eraser"):
            p = self._pix(e.position())
            if p:
                self._apply(*p)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_scale(self.scale + (2 if e.angleDelta().y() > 0 else -2))
            e.accept()
        else:
            e.ignore()

    def paintEvent(self, _):
        p = QPainter(self)
        s = self.scale
        # transparency checker
        for y in range(self.img.height()):
            for x in range(self.img.width()):
                base = QColor("#202833") if (x + y) % 2 == 0 else QColor("#161c24")
                p.fillRect(x * s, y * s, s, s, base)
                c = self.img.pixelColor(x, y)
                if c.alpha() > 0:
                    p.fillRect(x * s, y * s, s, s, c)
        # grid
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        for x in range(self.img.width() + 1):
            p.drawLine(x * s, 0, x * s, self.img.height() * s)
        for y in range(self.img.height() + 1):
            p.drawLine(0, y * s, self.img.width() * s, y * s)
        p.end()


class SpriteEditorDialog(QDialog):
    def __init__(self, parent, project_dir, name, kind, elem_w, elem_h, existing=None):
        super().__init__(parent)
        self.project_dir = project_dir
        self.name = name
        self.kind = kind
        self.result_rel = None
        self.setWindowTitle(f"Sprite \u2013 {name}")
        self.setStyleSheet(
            "QDialog{background:#0d1117;} QLabel{color:#d7e0ea;}"
            "QPushButton{background:#161b20;color:#e6eef7;border:1px solid #28384a;"
            "border-radius:6px;padding:6px 10px;} QPushButton:hover{background:#243248;}"
            "QPushButton:checked{background:#1e7e4f;border-color:#2a9c63;}")

        rw, rh = spritestore.sprite_resolution(elem_w, elem_h)
        if existing is not None and not existing.isNull():
            img = existing.convertToFormat(QImage.Format.Format_ARGB32)
        else:
            img = spritestore.default_glass_image(elem_w, elem_h).scaled(
                rw, rh, Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        disp = max(5, min(18, 460 // max(rw, rh)))
        self.canvas = PixelCanvas(img, scale=disp)

        root = QHBoxLayout(self); root.setContentsMargins(14, 14, 14, 14); root.setSpacing(12)
        scroll = QScrollArea(); scroll.setWidget(self.canvas)
        scroll.setStyleSheet("QScrollArea{background:#0a0e13;border:1px solid #1c2530;}")
        scroll.setMinimumSize(QSize(380, 320))
        root.addWidget(scroll, 1)

        side = QVBoxLayout(); side.setSpacing(8)
        side.addWidget(QLabel(f"<b>{name}</b>  ({rw}\u00d7{rh})"))
        side.addWidget(QLabel("Texture stretches to the element,\nso it auto-scales when resized."))

        tools = QHBoxLayout(); self._tg = QButtonGroup(self)
        import images
        from PyQt6.QtCore import QSize as _QSize
        _ticons = {"pencil": "tool_pencil", "eraser": "tool_eraser",
                   "fill": "tool_fill", "pick": "tool_pick"}
        _tfallback = {"pencil": "\u270e", "eraser": "\u232b", "fill": "\u25a3", "pick": "\u25c9"}
        for key in ("pencil", "eraser", "fill", "pick"):
            b = QPushButton(); b.setCheckable(True); b.setFixedWidth(46)
            ic = images.button_icon(_ticons[key], 22)
            if not ic.isNull():
                b.setIcon(ic); b.setIconSize(_QSize(22, 22)); b.setToolTip(key)
            else:
                b.setText(_tfallback[key])
            b.clicked.connect(lambda _c, k=key: self._set_tool(k))
            b._tool = key
            if key == "pencil":
                b.setChecked(True)
            self._tg.addButton(b); tools.addWidget(b)
        side.addLayout(tools)

        # brush size + zoom controls
        bz = QHBoxLayout(); bz.setSpacing(6)
        bz.addWidget(QLabel("Brush"))
        self.brush_spin = QSpinBox(); self.brush_spin.setRange(1, 8); self.brush_spin.setValue(1)
        self.brush_spin.setFixedWidth(48)
        self.brush_spin.valueChanged.connect(lambda v: setattr(self.canvas, "brush", v))
        bz.addWidget(self.brush_spin)
        bz.addSpacing(10)
        bz.addWidget(QLabel("Zoom"))
        zout = QPushButton("\u2212"); zout.setFixedWidth(32)
        zout.clicked.connect(lambda: self.canvas.set_scale(self.canvas.scale - 2))
        zin = QPushButton("+"); zin.setFixedWidth(32)
        zin.clicked.connect(lambda: self.canvas.set_scale(self.canvas.scale + 2))
        bz.addWidget(zout); bz.addWidget(zin); bz.addStretch(1)
        side.addLayout(bz)
        side.addWidget(QLabel("(Ctrl + scroll also zooms)"))

        self.swatch = QLabel(); self.swatch.setFixedHeight(22)
        self._paint_swatch()
        side.addWidget(self.swatch)
        grid = QHBoxLayout(); grid.setSpacing(3); col = 0; rowbox = None
        gridwrap = QVBoxLayout()
        for i, hexc in enumerate(PALETTE):
            if i % 6 == 0:
                rowbox = QHBoxLayout(); rowbox.setSpacing(3); gridwrap.addLayout(rowbox)
            sw = QPushButton(); sw.setFixedSize(26, 22)
            sw.setStyleSheet(f"background:{hexc};border:1px solid #28384a;border-radius:4px;")
            sw.clicked.connect(lambda _c, h=hexc: self._set_color(QColor(h)))
            rowbox.addWidget(sw)
        side.addLayout(gridwrap)
        custom = QPushButton("Custom colour\u2026"); custom.clicked.connect(self._pick_custom)
        side.addWidget(custom)

        side.addStretch(1)
        self.sharp_cb = QCheckBox("Sharp (pixel-perfect \u2013 not blurry when scaled)")
        self.sharp_cb.setChecked(True)
        self.sharp_cb.setStyleSheet("color:#c7d2dc;")
        side.addWidget(self.sharp_cb)
        glass = QPushButton("Reset canvas to Glass texture")
        glass.clicked.connect(self._reset_canvas); side.addWidget(glass)
        row = QHBoxLayout()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject); row.addWidget(cancel)
        save = QPushButton("Save sprite")
        save.setStyleSheet("QPushButton{background:#1e7e4f;border-color:#2a9c63;}")
        save.clicked.connect(self._save); row.addWidget(save)
        side.addLayout(row)
        self._elem = (elem_w, elem_h)
        root.addLayout(side)

    def _set_tool(self, k):
        self.canvas.tool = k

    def _set_color(self, c):
        self.canvas.color = c; self.canvas.tool = "pencil"; self._paint_swatch()
        for b in self._tg.buttons():
            b.setChecked(getattr(b, "_tool", None) == "pencil")

    def _paint_swatch(self):
        self.swatch.setStyleSheet(
            f"background:{self.canvas.color.name()};border:1px solid #28384a;border-radius:4px;")

    def _pick_custom(self):
        c = QColorDialog.getColor(self.canvas.color, self, "Pick colour")
        if c.isValid():
            self._set_color(c)

    def _reset_canvas(self):
        rw, rh = self.canvas.img.width(), self.canvas.img.height()
        img = spritestore.default_glass_image(*self._elem).scaled(
            rw, rh, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.canvas.set_image(img)

    def _save(self):
        img = self.canvas.img
        if self.sharp_cb.isChecked():
            # Sprites are stretched to the element via CSS border-image (smooth). To
            # keep pixels crisp, pre-upscale with nearest-neighbour so the hard edges
            # survive the stretch. Blurry = save at native small size (smooth stretch).
            longest = max(img.width(), img.height()) or 1
            factor = max(1, 256 // longest)
            if factor > 1:
                img = img.scaled(img.width() * factor, img.height() * factor,
                                 Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.FastTransformation)
        self.result_rel = spritestore.save_image(img, self.project_dir, self.name)
        self.accept()
