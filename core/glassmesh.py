"""glassmesh - import Wavefront OBJ meshes (e.g. exported from Unity ProBuilder)
and flatten them into the raycaster's 2D grid, transferring MTL materials.

Pure Python, no numpy. Two products per import:
  * the RAW 3D mesh (vertices / faces / materials) for the Unity-style 3D view
  * a FLATTENED grid (+ wall/floor materials) the raycaster renders like a maze

Top-down flatten model (works great for ProBuilder blockouts):
  * vertical faces  (|normal.y| small)  -> WALL cells, carrying the face material
  * up-facing faces (normal.y > 0)      -> FLOOR cells, carrying the face material
  * down-facing faces                    -> ceiling (ignored for now)
  * height collapses to full-height walls; ramps/overhangs flatten

Public API:
  import_obj(path, mesh_id, target_cells=40, up="y") -> MeshData (also stored)
  get(mesh_id) -> MeshData | None
  has(mesh_id) -> bool
  clear()
"""

import os
import math

# module-level registry: mesh_id -> MeshData   (shared by engine + renderer)
MESHES = {}

# wall material chars (floors use a separate set so they never collide)
_WALL_CHARS = "123456789abcdefghijklmnopqrstuvwxyz"
_FLOOR_CHARS = "GHJKLMNPQRSTUVWXYZ"          # avoid I/O (look like 1/0) and wall chars


class MeshData:
    def __init__(self, mesh_id):
        self.id = mesh_id
        self.verts = []           # [(x,y,z), ...]
        self.faces = []           # [([i,j,k,...], matname), ...]   0-based indices
        self.materials = {}       # name -> {"color": (r,g,b) 0..1, "map": path|None}
        # flattened (raycaster) products:
        self.grid = []            # ["11.1", ...] wall grid rows
        self.mats = {}            # char -> {"color": "#rrggbb", "image": path|None}
        self.floormap = None      # ["GG.G", ...] or None
        self.floor_mats = {}      # char -> {"color": ..., "image": ...}
        self.roofmap = None       # ceiling grid or None
        self.roof_mats = {}       # char -> {"color": ..., "image": ...}
        self.cols = 0
        self.rows = 0
        self.cellsize = 64.0      # world units per cell (matches raycaster scale)
        self.bounds = (0.0, 0.0, 0.0, 0.0)   # (minU, minV, maxU, maxV) ground plane
        self.spawn = (1.0, 1.0)   # a walkable start cell (centre of the floor)
        self.up = "y"


# ---------------------------------------------------------------- OBJ / MTL parse

def _parse_mtl(path):
    """Parse a .mtl file -> {name: {"color": (r,g,b), "map": texpath|None}}."""
    mats = {}
    if not path or not os.path.isfile(path):
        return mats
    base = os.path.dirname(path)
    cur = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                key = parts[0].lower()
                if key == "newmtl" and len(parts) > 1:
                    cur = " ".join(parts[1:])
                    mats[cur] = {"color": (0.8, 0.8, 0.8), "map": None}
                elif cur is None:
                    continue
                elif key == "kd" and len(parts) >= 4:      # diffuse color 0..1
                    try:
                        mats[cur]["color"] = (float(parts[1]), float(parts[2]),
                                              float(parts[3]))
                    except ValueError:
                        pass
                elif key in ("map_kd", "map_ka") and len(parts) > 1:
                    tex = parts[-1]                         # last token = filename
                    cand = tex if os.path.isabs(tex) else os.path.join(base, tex)
                    mats[cur]["map"] = cand if os.path.isfile(cand) else tex
    except OSError:
        pass
    return mats


def _parse_obj(path):
    """Parse a .obj -> (verts, faces, materials). Faces are polygons with the
    material name in effect; indices are 0-based into verts."""
    verts = []
    faces = []
    materials = {}
    base = os.path.dirname(path)
    cur_mat = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            key = parts[0].lower()
            if key == "v" and len(parts) >= 4:
                try:
                    verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    verts.append((0.0, 0.0, 0.0))
            elif key == "f" and len(parts) >= 4:
                idx = []
                for tok in parts[1:]:
                    vs = tok.split("/")[0]
                    if not vs:
                        continue
                    try:
                        vi = int(vs)
                    except ValueError:
                        continue
                    vi = (vi - 1) if vi > 0 else (len(verts) + vi)   # 1-based / negative
                    idx.append(vi)
                if len(idx) >= 3:
                    faces.append((idx, cur_mat))
            elif key == "usemtl" and len(parts) > 1:
                cur_mat = " ".join(parts[1:])
            elif key == "mtllib" and len(parts) > 1:
                mtl = " ".join(parts[1:])
                cand = mtl if os.path.isabs(mtl) else os.path.join(base, mtl)
                materials.update(_parse_mtl(cand))
    return verts, faces, materials


# ------------------------------------------------------------------- flatten math

def _face_normal(verts, idx):
    """Newell-ish normal from the first three vertices (robust enough for blockouts)."""
    if len(idx) < 3:
        return (0.0, 1.0, 0.0)
    a, b, c = verts[idx[0]], verts[idx[1]], verts[idx[2]]
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    m = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return (nx / m, ny / m, nz / m)


def _ground_axes(up):
    """Return (iu, iv, iup): vertex-tuple indices for the two ground axes + up."""
    return {"x": (1, 2, 0), "y": (0, 2, 1), "z": (0, 1, 2)}.get(up, (0, 2, 1))


def _raster_line(x0, y0, x1, y1, cols, rows, mark):
    """Grid-DDA a line between two cell-space points, calling mark(cx,cy) on each
    cell it crosses. Cells are clamped to the grid so edge walls aren't lost."""
    dx, dy = x1 - x0, y1 - y0
    steps = int(max(abs(dx), abs(dy))) + 1
    steps = max(1, min(steps * 2, 4096))
    for s in range(steps + 1):
        t = s / steps
        cx = int(x0 + dx * t)
        cy = int(y0 + dy * t)
        cx = 0 if cx < 0 else (cols - 1 if cx >= cols else cx)
        cy = 0 if cy < 0 else (rows - 1 if cy >= rows else cy)
        mark(cx, cy)


def _raster_tri(p0, p1, p2, cols, rows, mark):
    """Mark every cell whose centre is inside the triangle (cell space). Used for
    floor faces. Small triangles that miss all centres still stamp their vertices."""
    minx = max(0, int(math.floor(min(p0[0], p1[0], p2[0]))))
    maxx = min(cols - 1, int(math.ceil(max(p0[0], p1[0], p2[0]))))
    miny = max(0, int(math.floor(min(p0[1], p1[1], p2[1]))))
    maxy = min(rows - 1, int(math.ceil(max(p0[1], p1[1], p2[1]))))

    def edge(ax, ay, bx, by, px, py):
        return (px - ax) * (by - ay) - (py - ay) * (bx - ax)

    area = edge(p0[0], p0[1], p1[0], p1[1], p2[0], p2[1])
    if abs(area) < 1e-9:
        return
    hit = False
    for cy in range(miny, maxy + 1):
        for cx in range(minx, maxx + 1):
            px, py = cx + 0.5, cy + 0.5
            w0 = edge(p1[0], p1[1], p2[0], p2[1], px, py)
            w1 = edge(p2[0], p2[1], p0[0], p0[1], px, py)
            w2 = edge(p0[0], p0[1], p1[0], p1[1], px, py)
            if (w0 >= 0 and w1 >= 0 and w2 >= 0) or (w0 <= 0 and w1 <= 0 and w2 <= 0):
                mark(cx, cy); hit = True
    if not hit:                                   # thin/small tri: stamp its verts
        for p in (p0, p1, p2):
            cx, cy = int(p[0]), int(p[1])
            if 0 <= cx < cols and 0 <= cy < rows:
                mark(cx, cy)


def _hexcol(rgb):
    r, g, b = (max(0, min(255, int(c * 255))) for c in rgb)
    return "#%02x%02x%02x" % (r, g, b)


def flatten(verts, faces, materials, target_cells=40, up="y", cellsize=64.0):
    """Flatten a 3D mesh to a raycaster grid. Returns a dict of grid products."""
    if not verts:
        return None
    iu, iv, iup = _ground_axes(up)
    us = [v[iu] for v in verts]
    vs = [v[iv] for v in verts]
    hs = [v[iup] for v in verts]
    minu, maxu = min(us), max(us)
    minv, maxv = min(vs), max(vs)
    minh, maxh = min(hs), max(hs)
    midh = (minh + maxh) * 0.5
    du = (maxu - minu) or 1.0
    dv = (maxv - minv) or 1.0
    longest = max(du, dv)
    cell_world = longest / max(4, int(target_cells))       # world units per cell
    cols = max(1, int(math.ceil(du / cell_world))) + 2     # +2 = 1-cell wall margin
    rows = max(1, int(math.ceil(dv / cell_world))) + 2

    def to_cell(v):                                        # +1 for the margin
        return ((v[iu] - minu) / cell_world + 1.0, (v[iv] - minv) / cell_world + 1.0)

    wall_grid = [[None] * cols for _ in range(rows)]       # char or None
    floor_grid = [[None] * cols for _ in range(rows)]
    roof_grid = [[None] * cols for _ in range(rows)]
    wall_char = {}                                          # matname -> char
    floor_char = {}
    roof_char = {}
    mats_spec = {}
    floor_spec = {}
    roof_spec = {}

    def wchar(name):
        if name not in wall_char:
            ch = _WALL_CHARS[len(wall_char) % len(_WALL_CHARS)]
            wall_char[name] = ch
            m = materials.get(name, {})
            mats_spec[ch] = {"color": _hexcol(m.get("color", (0.72, 0.72, 0.75))),
                             "image": m.get("map")}
        return wall_char[name]

    def fchar(name):
        if name not in floor_char:
            ch = _FLOOR_CHARS[len(floor_char) % len(_FLOOR_CHARS)]
            floor_char[name] = ch
            m = materials.get(name, {})
            floor_spec[ch] = {"color": _hexcol(m.get("color", (0.4, 0.4, 0.44))),
                              "image": m.get("map")}
        return floor_char[name]

    def rchar(name):
        if name not in roof_char:
            ch = _FLOOR_CHARS[len(roof_char) % len(_FLOOR_CHARS)]
            roof_char[name] = ch
            m = materials.get(name, {})
            roof_spec[ch] = {"color": _hexcol(m.get("color", (0.3, 0.3, 0.34))),
                             "image": m.get("map")}
        return roof_char[name]

    for idx, matname in faces:
        if len(idx) < 3:
            continue
        n = _face_normal(verts, idx)
        pts = [to_cell(verts[i]) for i in idx]
        if abs(n[iup]) < 0.5:                              # vertical -> wall
            ch = wchar(matname)
            for k in range(len(pts)):
                x0, y0 = pts[k]
                x1, y1 = pts[(k + 1) % len(pts)]
                _raster_line(x0, y0, x1, y1, cols, rows,
                             lambda cx, cy, c=ch: wall_grid[cy].__setitem__(cx, c))
        else:                                              # horizontal surface
            meanh = sum(verts[i][iup] for i in idx) / len(idx)
            if meanh > midh + 1e-6:                        # upper -> ceiling / roof
                ch = rchar(matname)
                target = roof_grid
            else:                                          # lower -> walkable floor
                ch = fchar(matname)
                target = floor_grid
            for k in range(1, len(pts) - 1):               # fan triangulate
                _raster_tri(pts[0], pts[k], pts[k + 1], cols, rows,
                            lambda cx, cy, c=ch, t=target: t[cy].__setitem__(cx, c))

    # Build the final grid so INTERIORS stay walkable and thin corridors don't
    # collapse into solid wall lines:
    #   * floor faces define the walkable footprint (floor wins)
    #   * a cell is a wall if it has real wall geometry OR rings the floor,
    #     but never if it is itself floor
    floor_set = set()
    for y in range(rows):
        for x in range(cols):
            if floor_grid[y][x]:
                floor_set.add((x, y))
    # a default wall char (dominant wall material, or a neutral one) for ring cells
    if wall_char:
        default_wall = next(iter(wall_char.values()))
    else:
        default_wall = "1"
        mats_spec.setdefault("1", {"color": "#8a8f98", "image": None})

    final_wall = [["."] * cols for _ in range(rows)]
    for y in range(rows):
        for x in range(cols):
            if (x, y) in floor_set:
                continue                                   # floor wins -> walkable
            wc = wall_grid[y][x]
            is_wall = wc is not None
            if not is_wall:                                # ring: borders floor?
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if (x + dx, y + dy) in floor_set:
                            is_wall = True
                            break
                    if is_wall:
                        break
            if is_wall:
                final_wall[y][x] = wc if wc is not None else default_wall

    grid_rows = ["".join(row) for row in final_wall]
    floor_rows = ["".join(c if c else "." for c in row) for row in floor_grid]
    has_floor = any(c != "." for r in floor_rows for c in r)
    roof_rows = ["".join(c if c else "." for c in row) for row in roof_grid]
    has_roof = any(c != "." for r in roof_rows for c in r)

    # spawn = the roomiest walkable cell (most open floor around it), so the camera
    # starts in an open room rather than jammed in a 1-wide corridor
    if floor_set:
        def openness(p):
            x, y = p
            return sum(1 for dy in range(-2, 3) for dx in range(-2, 3)
                       if (x + dx, y + dy) in floor_set)
        best = max(floor_set, key=openness)
        spawn = (best[0] + 0.5, best[1] + 0.5)
    else:
        spawn = (cols / 2.0, rows / 2.0)

    return {
        "grid": grid_rows,
        "mats": mats_spec,
        "floormap": floor_rows if has_floor else None,
        "floor_mats": floor_spec,
        "roofmap": roof_rows if has_roof else None,
        "roof_mats": roof_spec,
        "cols": cols, "rows": rows, "cellsize": cellsize,
        "bounds": (minu, minv, maxu, maxv),
        "spawn": spawn,
    }


# ------------------------------------------------------------------- public entry

def import_obj(path, mesh_id, target_cells=40, up="y", cellsize=64.0):
    """Parse an OBJ (+MTL) and flatten it into a raycaster grid. Stores and returns
    a MeshData. Raises FileNotFoundError / ValueError on bad input."""
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path)
    verts, faces, materials = _parse_obj(path)
    if not verts or not faces:
        raise ValueError("OBJ has no geometry: %s" % path)
    md = MeshData(mesh_id)
    md.verts = verts
    md.faces = faces
    md.materials = materials
    md.up = up
    flat = flatten(verts, faces, materials, target_cells=target_cells,
                   up=up, cellsize=cellsize)
    if flat:
        md.grid = flat["grid"]
        md.mats = flat["mats"]
        md.floormap = flat["floormap"]
        md.floor_mats = flat["floor_mats"]
        md.roofmap = flat.get("roofmap")
        md.roof_mats = flat.get("roof_mats", {})
        md.cols = flat["cols"]
        md.rows = flat["rows"]
        md.cellsize = flat["cellsize"]
        md.bounds = flat["bounds"]
        md.spawn = flat.get("spawn", (1.0, 1.0))
    MESHES[mesh_id] = md
    return md


def get(mesh_id):
    return MESHES.get(mesh_id)


def has(mesh_id):
    return mesh_id in MESHES


def clear():
    MESHES.clear()


# ------------------------------------------------------- headless collision proxy

_COLL_EMPTY = {".", "0", " ", "", "_"}


class MeshCollider:
    """A minimal maze-like object exposing just what physics/nav need (grid,
    cellsize, _solid), so an imported mesh can collide without a visible raycaster.
    Registered into world.mazes by mesh.createCollider()."""

    def __init__(self, md):
        self.grid = [list(r) for r in md.grid]
        self.cellsize = md.cellsize
        self.mh = len(self.grid)
        self.maze_id = md.id

    def _cell(self, x, y):
        if 0 <= y < len(self.grid):
            row = self.grid[y]
            if 0 <= x < len(row):
                return row[x]
        return "1"                                # out of bounds = solid (closed level)

    def _solid(self, x, y):
        return self._cell(int(x), int(y)) not in _COLL_EMPTY
