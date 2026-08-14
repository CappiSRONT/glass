"""mesh3d - a Unity-style 3D scene view for inspecting imported meshes.

A pure-Python / Qt software 3D renderer (no numpy, no OpenGL): perspective
projection, painter's-algorithm depth sort, backface culling, flat directional
shading with the mesh's own materials, a ground grid and an XYZ axis gizmo.

Controls (like Unity's Scene view):
  * left-drag        orbit
  * right/mid-drag   pan
  * wheel            zoom
  * F                frame / reset
  * W                toggle wireframe

Meant for low-poly ProBuilder blockouts - it stays smooth into the low
thousands of triangles.
"""

import math
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QPainter, QColor, QPolygonF, QPen, QBrush


class MeshView3D(QWidget):
    def __init__(self, mesh_data=None, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # camera (orbit)
        self.yaw = math.radians(35)
        self.pitch = math.radians(28)
        self.dist = 12.0
        self.target = [0.0, 0.0, 0.0]
        self.fov = math.radians(55)
        self.wireframe = False
        self.show_grid = True
        # mesh
        self.verts = []          # [(x,y,z)]
        self.faces = []          # [(idx_list, QColor)]
        self._radius = 1.0
        # light direction (world) - a soft key from upper-front
        self._light = self._norm((0.4, 0.8, 0.5))
        self._last = None
        self._pan_btn = False
        if mesh_data is not None:
            self.set_mesh(mesh_data)

    # ---------------------------------------------------------------- mesh setup

    def set_mesh(self, md):
        """Load a glassmesh.MeshData: store verts + per-face material colours and
        frame the camera on the bounding sphere."""
        self.verts = list(md.verts)
        self.faces = []
        mats = getattr(md, "materials", {}) or {}
        for idx, matname in md.faces:
            m = mats.get(matname, {})
            col = m.get("color", (0.72, 0.72, 0.75))
            qc = QColor(max(0, min(255, int(col[0] * 255))),
                        max(0, min(255, int(col[1] * 255))),
                        max(0, min(255, int(col[2] * 255))))
            self.faces.append((idx, qc))
        self._frame()
        self.update()

    def _frame(self):
        if not self.verts:
            self.target = [0.0, 0.0, 0.0]; self._radius = 1.0; self.dist = 6.0
            return
        xs = [v[0] for v in self.verts]
        ys = [v[1] for v in self.verts]
        zs = [v[2] for v in self.verts]
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        cz = (min(zs) + max(zs)) / 2.0
        self.target = [cx, cy, cz]
        self._radius = max(1e-3, 0.5 * math.sqrt((max(xs) - min(xs)) ** 2 +
                                                 (max(ys) - min(ys)) ** 2 +
                                                 (max(zs) - min(zs)) ** 2))
        self.dist = self._radius / max(0.2, math.sin(self.fov / 2)) * 1.1

    # ------------------------------------------------------------------ 3D maths

    @staticmethod
    def _norm(v):
        m = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
        return (v[0] / m, v[1] / m, v[2] / m)

    def _basis(self):
        """Camera position + view basis (right, up, forward) from orbit angles."""
        cp = math.cos(self.pitch); sp = math.sin(self.pitch)
        cy = math.cos(self.yaw); sy = math.sin(self.yaw)
        fwd = (-cp * cy, -sp, -cp * sy)                # from camera toward target
        cam = (self.target[0] - fwd[0] * self.dist,
               self.target[1] - fwd[1] * self.dist,
               self.target[2] - fwd[2] * self.dist)
        right = self._norm((fwd[2], 0.0, -fwd[0]))     # cross(fwd, worldUp)
        up = (right[1] * fwd[2] - right[2] * fwd[1],
              right[2] * fwd[0] - right[0] * fwd[2],
              right[0] * fwd[1] - right[1] * fwd[0])
        return cam, right, up, fwd

    def _project_all(self, cam, right, up, fwd, W, H):
        """Project every vertex to screen; return list of (sx, sy, camz) or None
        (behind camera). camz is depth along the view direction."""
        f = (H / 2.0) / math.tan(self.fov / 2.0)
        out = []
        for (x, y, z) in self.verts:
            dx, dy, dz = x - cam[0], y - cam[1], z - cam[2]
            cz = dx * fwd[0] + dy * fwd[1] + dz * fwd[2]     # depth
            if cz <= 0.05:
                out.append(None); continue
            cxs = dx * right[0] + dy * right[1] + dz * right[2]
            cys = dx * up[0] + dy * up[1] + dz * up[2]
            sx = W / 2.0 + (cxs / cz) * f
            sy = H / 2.0 - (cys / cz) * f
            out.append((sx, sy, cz))
        return out

    # -------------------------------------------------------------------- render

    def paintEvent(self, _e):
        W, H = self.width(), self.height()
        if W < 2 or H < 2:
            return
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        qp.fillRect(self.rect(), QColor("#20242b"))       # Unity-ish scene bg
        cam, right, up, fwd = self._basis()

        if self.show_grid:
            self._draw_grid(qp, cam, right, up, fwd, W, H)

        if not self.verts:
            qp.setPen(QColor("#5b6472"))
            qp.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                        "No mesh loaded.\nOpen an .obj to view it in 3D.")
            self._draw_gizmo(qp, right, up, fwd, W, H)
            return

        proj = self._project_all(cam, right, up, fwd, W, H)
        drawn = []
        for idx, qc in self.faces:
            pts = [proj[i] for i in idx if 0 <= i < len(proj)]
            if len(pts) < 3 or any(p is None for p in pts):
                continue
            # world normal + centroid for shading, culling and depth sort
            a, b, c = self.verts[idx[0]], self.verts[idx[1]], self.verts[idx[2]]
            n = self._norm(((b[1]-a[1])*(c[2]-a[2]) - (b[2]-a[2])*(c[1]-a[1]),
                            (b[2]-a[2])*(c[0]-a[0]) - (b[0]-a[0])*(c[2]-a[2]),
                            (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])))
            facing = n[0]*fwd[0] + n[1]*fwd[1] + n[2]*fwd[2]
            if not self.wireframe and facing > 0.02:      # backface cull
                continue
            depth = sum(p[2] for p in pts) / len(pts)
            drawn.append((depth, pts, qc, n))
        drawn.sort(key=lambda t: -t[0])                    # far -> near
        for depth, pts, qc, n in drawn:
            poly = QPolygonF([QPointF(p[0], p[1]) for p in pts])
            if self.wireframe:
                qp.setPen(QPen(QColor("#8fb8ff"), 1))
                qp.setBrush(Qt.BrushStyle.NoBrush)
                qp.drawPolygon(poly)
            else:
                lit = abs(n[0]*self._light[0] + n[1]*self._light[1] +
                          n[2]*self._light[2])
                sh = 0.30 + 0.70 * lit                     # ambient + diffuse
                col = QColor(int(qc.red()*sh), int(qc.green()*sh), int(qc.blue()*sh))
                qp.setPen(QPen(QColor(0, 0, 0, 60), 1))
                qp.setBrush(QBrush(col))
                qp.drawPolygon(poly)
        self._draw_gizmo(qp, right, up, fwd, W, H)

    def _draw_grid(self, qp, cam, right, up, fwd, W, H):
        """A ground grid on the y=0 plane for spatial reference."""
        f = (H / 2.0) / math.tan(self.fov / 2.0)
        r = max(4.0, self._radius * 2.0)
        step = max(0.5, r / 10.0)
        n = int(r / step)

        def proj(x, z):
            dx, dy, dz = x - cam[0], -cam[1], z - cam[2]
            cz = dx*fwd[0] + dy*fwd[1] + dz*fwd[2]
            if cz <= 0.05:
                return None
            cxs = dx*right[0] + dy*right[1] + dz*right[2]
            cys = dx*up[0] + dy*up[1] + dz*up[2]
            return (W/2.0 + (cxs/cz)*f, H/2.0 - (cys/cz)*f)

        cx, cz0 = self.target[0], self.target[2]
        qp.setPen(QPen(QColor("#2c333d"), 1))
        for i in range(-n, n + 1):
            a = proj(cx - r, cz0 + i*step); b = proj(cx + r, cz0 + i*step)
            if a and b:
                qp.drawLine(QPointF(*a), QPointF(*b))
            a = proj(cx + i*step, cz0 - r); b = proj(cx + i*step, cz0 + r)
            if a and b:
                qp.drawLine(QPointF(*a), QPointF(*b))

    def _draw_gizmo(self, qp, right, up, fwd, W, H):
        """Small XYZ axis indicator in the corner (Unity-style)."""
        ox, oy, L = W - 42, H - 42, 22
        axes = [((1, 0, 0), "#ff5b6b", "X"), ((0, 1, 0), "#7dd66b", "Y"),
                ((0, 0, 1), "#5b8bff", "Z")]
        for v, col, name in axes:
            sx = v[0]*right[0] + v[1]*right[1] + v[2]*right[2]
            sy = v[0]*up[0] + v[1]*up[1] + v[2]*up[2]
            qp.setPen(QPen(QColor(col), 2))
            qp.drawLine(QPointF(ox, oy), QPointF(ox + sx*L, oy - sy*L))

    # ------------------------------------------------------------------ controls

    def mousePressEvent(self, e):
        self._last = e.position()
        self._pan_btn = e.button() in (Qt.MouseButton.RightButton,
                                       Qt.MouseButton.MiddleButton)

    def mouseMoveEvent(self, e):
        if self._last is None:
            return
        p = e.position()
        dx = p.x() - self._last.x(); dy = p.y() - self._last.y()
        self._last = p
        btns = e.buttons()
        if btns & (Qt.MouseButton.RightButton | Qt.MouseButton.MiddleButton):
            _, right, up, _ = self._basis()
            s = self.dist * 0.0016
            for k in range(3):
                self.target[k] -= (right[k]*dx - up[k]*dy) * s
        elif btns & Qt.MouseButton.LeftButton:
            self.yaw += dx * 0.01
            self.pitch = max(-1.55, min(1.55, self.pitch + dy * 0.01))
        self.update()

    def mouseReleaseEvent(self, _e):
        self._last = None

    def wheelEvent(self, e):
        d = e.angleDelta().y() / 120.0
        self.dist = max(self._radius * 0.2, min(self._radius * 40,
                                                self.dist * (0.9 ** d)))
        self.update()

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key.Key_F:
            self._frame()
        elif k == Qt.Key.Key_W:
            self.wireframe = not self.wireframe
        elif k == Qt.Key.Key_G:
            self.show_grid = not self.show_grid
        else:
            super().keyPressEvent(e); return
        self.update()
