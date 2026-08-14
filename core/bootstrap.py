"""
Dependency bootstrap
====================
On first launch - or any time a required package is missing - this installs the
Glass dependencies with pip, so `python browser.py` just works on a fresh
machine. It is safe to call repeatedly: when everything is already importable it
does nothing and returns immediately.

The browser needs PyQt6 + PyQt6-WebEngine; the editor only needs PyQt6, so each
asks for just what it uses (passing the right group below).
"""

from __future__ import annotations
import importlib
import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS = os.path.join(HERE, "requirements.txt")

# (pip spec, module used to detect whether it is already installed)
QT = ("PyQt6>=6.6", "PyQt6.QtWidgets")
WEBENGINE = ("PyQt6-WebEngine>=6.6", "PyQt6.QtWebEngineWidgets")

BROWSER_DEPS = [QT, WEBENGINE]   # the full browser
EDITOR_DEPS = [QT]               # the standalone editor (no Chromium needed)

# optional: enables audio.playSound quality (bit-crush) and hertz (resample).
# never blocks startup - audio still works (speed/volume) without it.
MINIAUDIO = ("miniaudio", "miniaudio")
# optional: the separate WebView2 window for DRM sites (Windows Edge engine).
PYWEBVIEW = ("pywebview", "webview")
OPTIONAL_DEPS = [MINIAUDIO, PYWEBVIEW]


def ensure_optional(deps=None, verbose=True):
    """Best-effort install of optional packages. Never fails the launch."""
    deps = deps or OPTIONAL_DEPS
    missing = _missing(deps)
    if not missing:
        return
    specs = [s for s, _ in missing]
    if verbose:
        print(f"[glass] Installing optional support ({', '.join(specs)})\u2026",
              flush=True)
    try:
        _pip_install(specs)
        importlib.invalidate_caches()
    except Exception:
        pass


def _missing(deps):
    out = []
    for spec, mod in deps:
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append((spec, mod))
    return out


def _run_pip(args):
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *args])
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def _pip_install(specs):
    # plain install first; then fall back to flags some environments require
    if _run_pip(specs):
        return True
    if _run_pip(["--break-system-packages", *specs]):   # PEP 668 systems
        return True
    if _run_pip(["--user", *specs]):                     # locked-down systems
        return True
    return False


def ensure_dependencies(deps=None, verbose=True):
    """Install any missing packages from `deps`. Returns True if all present."""
    deps = deps or BROWSER_DEPS
    missing = _missing(deps)
    if not missing:
        return True

    # make sure pip itself exists
    if importlib.util.find_spec("pip") is None:
        try:
            subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
        except (subprocess.CalledProcessError, OSError):
            if verbose:
                print("[glass] pip is unavailable. Install Python from python.org "
                      "(it includes pip) and try again.", file=sys.stderr)
            return False

    specs = [s for s, _ in missing]
    if verbose:
        line = "=" * 64
        print(line)
        print(" Glass setup - installing dependencies (first run only)")
        print("   " + "\n   ".join(specs))
        print(" This can take a minute. A window will open when it's done.")
        print(line, flush=True)

    _pip_install(specs)
    importlib.invalidate_caches()

    still = _missing(deps)
    if still:
        if verbose:
            names = ", ".join(s for s, _ in still)
            print(f"[glass] Could not install: {names}", file=sys.stderr)
            print("[glass] Install manually, then relaunch:", file=sys.stderr)
            print("        python -m pip install -r requirements.txt", file=sys.stderr)
        return False
    if verbose:
        print("[glass] Dependencies ready.", flush=True)
    return True
