"""Glass package system.

A .glasspack is just a zip archive whose single top-level folder is a project
(a project.json plus .glass files, Python modules, and assets). Installing one
drops that folder into the user's projects directory; exporting one zips a
project folder back up.

.glass files may pull in real Python with:

    import helper                  # loads helper.py from the project folder
    import mc from "mclauncher.py" # loads a named file under an alias

Imported modules expose functions that buttons call via  call: alias.function .
Each function receives a PackageContext giving it the live UI variables, the
browser API, and helpers to run work off the UI thread.

SECURITY: an installed package can run arbitrary Python on this machine, exactly
like a Unity asset or an npm dependency. Only install .glasspack files you trust.
Imports are restricted to files inside the package's own folder (no path
traversal, no system paths).
"""

import importlib.util
import os
import zipfile


# ---------------------------------------------------------------------------
#  importing a package's Python
# ---------------------------------------------------------------------------
def load_imports(project_dir, imports, logger=None):
    """Load [(alias, filename)] Python modules from project_dir.
    Returns {alias: module}. Files must live directly inside project_dir."""
    mods = {}
    if not project_dir:
        return mods
    for alias, filename in (imports or []):
        safe = os.path.basename(str(filename))          # no path traversal
        if not safe.endswith(".py"):
            safe += ".py"
        path = os.path.join(project_dir, safe)
        if not os.path.isfile(path):
            if logger:
                logger(f"[import] not found: {safe}")
            continue
        try:
            modname = f"glasspack_{alias}_{abs(hash(path)) & 0xffffff}"
            spec = importlib.util.spec_from_file_location(modname, path)
            mod = importlib.util.module_from_spec(spec)
            # let the module import sibling files from its own folder
            import sys
            if project_dir not in sys.path:
                sys.path.insert(0, project_dir)
            spec.loader.exec_module(mod)
            mods[alias] = mod
            if logger:
                logger(f"[import] loaded {alias} from {safe}")
        except Exception as e:
            if logger:
                logger(f"[import] error loading {safe}: {e}")
    return mods


class PackageContext:
    """Handed to every imported function called from a button.

    ctx.get(name) / ctx.set(name, value)  read & write the live UI variables
        (any {name} text/labels update automatically a moment later).
    ctx.api      the browser action API (navigate, getGlass, toggle, ...).
    ctx.project_dir  the folder the package lives in (for reading its files).
    ctx.log(msg) write to the Glass log.
    ctx.thread(fn)   run fn on a background thread (for network / downloads).
    ctx.run_later(fn, ms)  run fn on the UI thread after a delay.
    """

    def __init__(self, window, get_vars, api, project_dir):
        self._window = window
        self._get_vars = get_vars
        self.api = api
        self.project_dir = project_dir

    @property
    def vars(self):
        try:
            return self._get_vars() or {}
        except Exception:
            return {}

    def get(self, name, default=None):
        return self.vars.get(name, default)

    def set(self, name, value):
        v = self.vars
        if v is not None:
            v[name] = value

    def set_many(self, **pairs):
        v = self.vars
        if v is not None:
            v.update(pairs)

    def log(self, msg):
        try:
            self._window.log(f"[pkg] {msg}")
        except Exception:
            pass

    def thread(self, fn):
        import threading
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        return t

    def run_later(self, fn, delay_ms=0):
        try:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(int(delay_ms), fn)
        except Exception:
            fn()


# ---------------------------------------------------------------------------
#  install / export
# ---------------------------------------------------------------------------
def _safe_members(zf):
    """Yield archive members, rejecting absolute paths and .. traversal."""
    for m in zf.namelist():
        norm = os.path.normpath(m)
        if norm.startswith(("/", "\\")) or norm.split(os.sep)[0] == "..":
            raise ValueError(f"unsafe path in package: {m}")
        yield m


def pack_project_name(pack_path):
    """Return the top-level folder name inside a .glasspack (the project name)."""
    with zipfile.ZipFile(pack_path) as zf:
        for m in zf.namelist():
            top = m.replace("\\", "/").split("/")[0]
            if top:
                return top
    return ""


def install_pack(pack_path, projects_root):
    """Extract a .glasspack into projects_root. Returns the new project dir."""
    os.makedirs(projects_root, exist_ok=True)
    with zipfile.ZipFile(pack_path) as zf:
        members = list(_safe_members(zf))
        zf.extractall(projects_root, members)
    name = pack_project_name(pack_path)
    return os.path.join(projects_root, name) if name else projects_root


def export_pack(project_dir, out_path):
    """Zip a project folder (including the folder itself) into out_path."""
    project_dir = os.path.abspath(project_dir)
    base = os.path.basename(project_dir.rstrip("/\\"))
    parent = os.path.dirname(project_dir)
    if not out_path.endswith(".glasspack"):
        out_path += ".glasspack"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith((".pyc", ".glass_ready")) or f == ".editor_seen":
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, parent)   # keep the folder name
                zf.write(full, rel)
    return out_path
