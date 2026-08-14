"""
Glass - a transparent, no-AI, no-ads, fully scriptable browser shell.
=====================================================================
- Real rendering via Chromium (Qt WebEngine) - opens real sites.
- Every request is interceptable and logged. Nothing is hidden.
- No telemetry, no AI, no built-in tracking.
- UI (menus/bars/panels) is defined in editable .glass files and
  driven by your own API (api.py). Edit, save, hit Reload UI.

Run:  python browser.py
"""

from __future__ import annotations
import os
import sys
import re
import base64

# Ensure the GUI dependencies exist before we import them. On a fresh machine
# (or if something was uninstalled) this pip-installs them, then continues.
from bootstrap import ensure_dependencies, BROWSER_DEPS
ensure_dependencies(BROWSER_DEPS)

# ---- Chromium memory tuning (must be set before QtWebEngine initializes) ----
def _lib_names():
    if sys.platform.startswith("win"):
        return ["widevinecdm.dll"]
    if sys.platform == "darwin":
        return ["libwidevinecdm.dylib"]
    return ["libwidevinecdm.so"]


def _lib_in(path):
    """Given a file or directory, return the widevine library FILE path inside."""
    import glob as _glob
    names = _lib_names()
    if os.path.isfile(path) and os.path.basename(path) in names:
        return path
    if os.path.isdir(path):
        for nm in names:
            hits = _glob.glob(os.path.join(path, "**", nm), recursive=True)
            if hits:
                hits.sort()
                return hits[-1]
    return ""


def _pkg_root(libfile):
    """Given the CDM library file, return the folder Chromium/Qt expects on
    --widevine-path (the one containing manifest.json), falling back to the
    library's own folder."""
    if not libfile:
        return ""
    d = os.path.dirname(libfile)
    probe = d
    for _ in range(4):
        if os.path.isfile(os.path.join(probe, "manifest.json")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return d


def _find_widevine_dir():
    """Locate the Widevine CDM and return the folder to pass on --widevine-path.
    Order: manual override -> our downloaded (version-matched) copy -> Chrome ->
    Edge. Chrome/our download track current stable Chromium; Edge may be a newer
    channel whose CDM won't load in Qt's Chromium."""
    import glob as _glob
    # 1) manual override (env or prefs) - a file OR a folder
    manual = os.environ.get("GLASS_WIDEVINE_PATH", "")
    if not manual:
        try:
            import prefs as _p
            manual = _p.load("widevine_path", "") or ""
        except Exception:
            manual = ""
    if manual:
        lib = _lib_in(manual)
        if lib:
            return _pkg_root(lib)
    # 2) a CDM downloaded by the launcher (widevine_setup.py) - version matched
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "widevine")
    lib = _lib_in(local)
    if lib:
        return _pkg_root(lib)
    # 3) installed browsers - Chrome first (matches stable Chromium), Edge last
    if sys.platform.startswith("win"):
        env = os.environ
        chrome = [
            os.path.join(env.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data", "WidevineCdm"),
            os.path.join(env.get("PROGRAMFILES", r"C:\Program Files"), "Google", "Chrome", "Application"),
            os.path.join(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Google", "Chrome", "Application"),
        ]
        edge = [
            os.path.join(env.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data", "WidevineCdm"),
            os.path.join(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Microsoft", "Edge", "Application"),
            os.path.join(env.get("PROGRAMFILES", r"C:\Program Files"), "Microsoft", "Edge", "Application"),
        ]
        bases = chrome + edge
    elif sys.platform == "darwin":
        bases = ["/Applications/Google Chrome.app",
                 os.path.expanduser("~/Library/Application Support/Google/Chrome/WidevineCdm")]
    else:
        bases = [os.path.expanduser("~/.config/google-chrome/WidevineCdm"),
                 "/opt/google/chrome", "/usr/lib/chromium", "/usr/lib/chromium-browser"]
    names = _lib_names()
    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        for nm in names:
            hits = _glob.glob(os.path.join(base, "**", nm), recursive=True)
            if hits:
                hits.sort()
                return _pkg_root(hits[-1])
    return ""


def _low_memory():
    try:
        import prefs as _p
        return bool(_p.load("low_memory", True))   # default ON - Glass is lightweight
    except Exception:
        return True


_wv = _find_widevine_dir()
_LOWMEM = _low_memory()
_flags = (
    "--process-per-site "                       # share one renderer per site
    "--disk-cache-size=33554432 "               # 32 MB on-disk cache
    "--disable-domain-reliability "
    "--disable-sync "
    "--disable-breakpad "
    "--disable-crash-reporter "
    "--disable-client-side-phishing-detection "
    "--no-pings --no-default-browser-check --no-first-run "
    "--disable-features=MediaRouter,Translate,OptimizationHints,InterestCohort,"
    "InterestFeedContentSuggestions,AutofillServerCommunication,SafeBrowsing,"
    "NetworkPrediction,Prefetch,PreconnectToSearch,CalculateNativeWinOcclusion "
)
if _LOWMEM:
    # aggressive memory savings. Trade-off: web (in-page) video may be lower
    # quality/software-decoded. Local vcr.video is unaffected (uses QtMultimedia).
    _flags += (
        "--renderer-process-limit=2 "
        "--enable-low-end-device-mode "         # smaller caches, tighter tile/GC limits
        "--js-flags=--max-old-space-size=256 "  # cap each renderer's JS heap
        "--disable-gpu-compositing "            # helps on virtualized/weak GPUs
        "--disable-gpu-shader-disk-cache "
    )
else:
    _flags += "--renderer-process-limit=3 --js-flags=--max-old-space-size=512 "
if _wv:
    _flags += f'--widevine-path="{_wv}" '
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", _flags)

from PyQt6.QtCore import QUrl, Qt, QFileSystemWatcher, QEvent, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut, QAction
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
    QPushButton, QTabWidget, QLabel, QPlainTextEdit, QDockWidget, QToolBar,
    QDialog, QColorDialog, QCheckBox,
)
from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from PyQt6.QtWebEngineWidgets import QWebEngineView

import dsl
import renderer
from adblock import Interceptor
from api import BrowserAPI
import glassnet
import project
import tabbudget
import glasspack
import images
import prefs
import vault
import theme
import history

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
PROJECTS_DIR = os.path.join(HERE, "projects")

# Launch onto a clean local start page so your .glass main menu is the landing
# screen. DuckDuckGo is used only as the search engine for typed queries below.
_START_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
 html,body{height:100%;margin:0;background:#0f1419;color:#1c2630;
   font-family:'Segoe UI',Arial,sans-serif;overflow:hidden;}
 .wrap{height:100%;display:flex;align-items:center;justify-content:center;
   flex-direction:column;gap:12px;user-select:none;}
 .logo{font-size:72px;font-weight:700;letter-spacing:3px;color:#202b35;}
 .sub{font-size:14px;color:#202b35;}
</style></head><body><div class="wrap">
 <div class="logo">Glass</div>
 <div class="sub">Your main menu is loaded. Type above to search, or use the panel.</div>
</div></body></html>"""
HOME = "data:text/html;base64," + base64.b64encode(_START_HTML.encode()).decode()
SEARCH_URL = "https://duckduckgo.com/?kae=d&q="   # full DDG honors kae=d (dark); /html/ does not


def _key_name(event):
    """Turn a Qt key event into a simple name for input.GetHeld/GetClick."""
    k = event.key()
    special = {
        Qt.Key.Key_Space: "space", Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right",
        Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down", Qt.Key.Key_Return: "enter",
        Qt.Key.Key_Enter: "enter", Qt.Key.Key_Escape: "escape",
        Qt.Key.Key_Shift: "shift", Qt.Key.Key_Control: "ctrl",
        Qt.Key.Key_Tab: "tab", Qt.Key.Key_Backspace: "backspace",
    }
    if k in special:
        return special[k]
    t = event.text()
    if t and t.strip():
        return t.lower()
    return ""


class SavePasswordDialog(QDialog):
    """Asks whether to save a login Glass just saw submitted (like Chrome/Google)."""
    def __init__(self, window, host, username):
        super().__init__(window)
        self.result_choice = None
        self.setWindowTitle("Save login?")
        self.setMinimumWidth(380)
        self.setStyleSheet(
            "QDialog{background:#0d1117;} QLabel{color:#d7e0ea;}"
            "QPushButton{background:#1a2330;color:#e6eef7;border:1px solid #28384a;"
            "border-radius:6px;padding:7px 12px;} QPushButton:hover{background:#243248;}")
        lay = QVBoxLayout(self); lay.setContentsMargins(16, 16, 16, 16); lay.setSpacing(8)
        head = QLabel(f"Save your login for {host}?")
        head.setStyleSheet("font-size:15px;font-weight:600;color:#6cf09a;")
        lay.addWidget(head)
        lay.addWidget(QLabel(f"Username: {username or '(none detected)'}"))
        lay.addWidget(QLabel("Stored locally and encrypted - never sent online."))
        row = QHBoxLayout(); row.addStretch(1)
        never = QPushButton("Never for this site"); never.clicked.connect(self._never)
        notnow = QPushButton("Not now"); notnow.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setStyleSheet("QPushButton{background:#1e7e4f;border-color:#2a9c63;}")
        save.clicked.connect(self._save)
        row.addWidget(never); row.addWidget(notnow); row.addWidget(save)
        lay.addLayout(row)

    def _save(self): self.result_choice = "save"; self.accept()
    def _never(self): self.result_choice = "never"; self.accept()


class FillPasswordDialog(QDialog):
    """Offers to paste a saved login into the page."""
    def __init__(self, window, host, username):
        super().__init__(window)
        self.setWindowTitle("Use saved login?")
        self.setMinimumWidth(360)
        self.setStyleSheet(
            "QDialog{background:#0d1117;} QLabel{color:#d7e0ea;}"
            "QPushButton{background:#1a2330;color:#e6eef7;border:1px solid #28384a;"
            "border-radius:6px;padding:7px 12px;} QPushButton:hover{background:#243248;}")
        lay = QVBoxLayout(self); lay.setContentsMargins(16, 16, 16, 16); lay.setSpacing(8)
        head = QLabel(f"Fill your saved login for {host}?")
        head.setStyleSheet("font-size:15px;font-weight:600;color:#6cf09a;")
        lay.addWidget(head)
        lay.addWidget(QLabel(f"Username: {username or '(saved)'}"))
        row = QHBoxLayout(); row.addStretch(1)
        no = QPushButton("No thanks"); no.clicked.connect(self.reject)
        yes = QPushButton("Fill it in")
        yes.setStyleSheet("QPushButton{background:#1e7e4f;border-color:#2a9c63;}")
        yes.clicked.connect(self.accept)
        row.addWidget(no); row.addWidget(yes); lay.addLayout(row)


class SavedDataDialog(QDialog):
    """Settings > Saved data: list/reveal/delete locally stored logins."""
    def __init__(self, window):
        super().__init__(window)
        self.setWindowTitle("Saved data")
        self.setMinimumSize(460, 360)
        self.setStyleSheet(
            "QDialog{background:#0d1117;} QLabel{color:#d7e0ea;}"
            "QListWidget{background:#11161c;color:#d7e0ea;border:1px solid #1c2530;}"
            "QPushButton{background:#1a2330;color:#e6eef7;border:1px solid #28384a;"
            "border-radius:6px;padding:6px 11px;} QPushButton:hover{background:#243248;}")
        from PyQt6.QtWidgets import QListWidget
        lay = QVBoxLayout(self); lay.setContentsMargins(16, 16, 16, 16); lay.setSpacing(10)
        head = QLabel("Saved logins (local only)")
        head.setStyleSheet("font-size:16px;font-weight:600;color:#6cf09a;")
        lay.addWidget(head)
        if sys.platform.startswith("win"):
            enc_note = ("These are stored on this PC, encrypted with your Windows "
                        "account's own OS-level key (DPAPI) - unreadable on another "
                        "account or machine. Nothing here is uploaded anywhere.")
        else:
            enc_note = ("These are stored on this PC only - nothing is uploaded "
                        "anywhere - but on this OS it's light obfuscation, NOT real "
                        "encryption. Don't treat this as a hardened password manager.")
        note_lbl = QLabel(enc_note)
        note_lbl.setWordWrap(True)
        lay.addWidget(note_lbl)
        self.list = QListWidget(); lay.addWidget(self.list, 1)
        self._refresh()
        row = QHBoxLayout()
        rev = QPushButton("Reveal password"); rev.clicked.connect(self._reveal); row.addWidget(rev)
        dele = QPushButton("Delete"); dele.clicked.connect(self._delete); row.addWidget(dele)
        clr = QPushButton("Clear all"); clr.clicked.connect(self._clear); row.addWidget(clr)
        row.addStretch(1)
        close = QPushButton("Close"); close.clicked.connect(self.accept); row.addWidget(close)
        lay.addLayout(row)

    def _refresh(self):
        self.list.clear()
        self._hosts = []
        for host, user in vault.list_logins():
            self._hosts.append(host)
            self.list.addItem(f"{host}    -    {user or '(no username)'}")
        if not self._hosts:
            self.list.addItem("(nothing saved yet)")

    def _selected_host(self):
        i = self.list.currentRow()
        return self._hosts[i] if 0 <= i < len(self._hosts) else None

    def _reveal(self):
        host = self._selected_host()
        if not host:
            return
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.information(self, host, f"Password: {vault.reveal(host)}")

    def _delete(self):
        host = self._selected_host()
        if host:
            vault.delete_login(host); self._refresh()

    def _clear(self):
        from PyQt6.QtWidgets import QMessageBox
        if QMessageBox.question(self, "Glass", "Delete ALL saved logins?") \
                == QMessageBox.StandardButton.Yes:
            vault.clear_all(); self._refresh()


class RestoreSessionDialog(QDialog):
    """Zen-style 'welcome back' prompt offering to reopen last session's tabs."""
    def __init__(self, window, items):
        super().__init__(window)
        from PyQt6.QtWidgets import QListWidget
        self.setWindowTitle("Welcome back")
        self.setMinimumWidth(440)
        self.setStyleSheet(
            "QDialog{background:#0d1117;}QLabel{color:#d7e0ea;}"
            "QListWidget{background:#11161c;color:#d7e0ea;border:1px solid #1c2530;}"
            "QPushButton{background:#1a2330;color:#e6eef7;border:1px solid #28384a;"
            "border-radius:6px;padding:7px 12px;}QPushButton:hover{background:#243248;}")
        lay = QVBoxLayout(self); lay.setContentsMargins(16, 16, 16, 16); lay.setSpacing(10)
        head = QLabel(f"Restore your last {len(items)} tab" + ("s" if len(items) != 1 else "") + "?")
        head.setStyleSheet("font-size:16px;font-weight:600;color:#6cf09a;")
        lay.addWidget(head)
        lst = QListWidget()
        for it in items[:30]:
            lst.addItem(it.get("title") or it.get("url", ""))
        lay.addWidget(lst, 1)
        row = QHBoxLayout(); row.addStretch(1)
        no = QPushButton("Start fresh"); no.clicked.connect(self.reject); row.addWidget(no)
        yes = QPushButton("Restore")
        yes.setStyleSheet("QPushButton{background:#1e7e4f;border-color:#2a9c63;}")
        yes.clicked.connect(self.accept); row.addWidget(yes)
        lay.addLayout(row)


class HistoryDialog(QDialog):
    """Searchable browsing history; click an entry to open it in a new tab."""
    def __init__(self, window):
        super().__init__(window)
        from PyQt6.QtWidgets import QListWidget, QListWidgetItem
        self._window = window
        self.setWindowTitle("History")
        self.setMinimumSize(560, 460)
        self.setStyleSheet(
            "QDialog{background:#0d1117;}QLabel{color:#d7e0ea;}"
            "QLineEdit{background:#11161c;color:#d7e0ea;border:1px solid #1c2530;"
            "padding:6px;border-radius:5px;}"
            "QListWidget{background:#11161c;color:#d7e0ea;border:1px solid #1c2530;}"
            "QListWidget::item{padding:4px;}"
            "QPushButton{background:#1a2330;color:#e6eef7;border:1px solid #28384a;"
            "border-radius:6px;padding:6px 11px;}QPushButton:hover{background:#243248;}")
        lay = QVBoxLayout(self); lay.setContentsMargins(16, 16, 16, 16); lay.setSpacing(10)
        head = QLabel("History"); head.setStyleSheet("font-size:16px;font-weight:600;color:#6cf09a;")
        lay.addWidget(head)
        self.search = QLineEdit(); self.search.setPlaceholderText("Search history\u2026")
        self.search.textChanged.connect(self._refresh)
        lay.addWidget(self.search)
        self.list = QListWidget(); self.list.itemActivated.connect(self._open)
        self.list.itemDoubleClicked.connect(self._open)
        lay.addWidget(self.list, 1)
        self._items = []
        self._refresh()
        row = QHBoxLayout()
        opn = QPushButton("Open"); opn.clicked.connect(lambda: self._open(self.list.currentItem()))
        row.addWidget(opn)
        clr = QPushButton("Clear history"); clr.clicked.connect(self._clear); row.addWidget(clr)
        row.addStretch(1)
        close = QPushButton("Close"); close.clicked.connect(self.accept); row.addWidget(close)
        lay.addLayout(row)

    def _refresh(self):
        import time as _t
        self.list.clear()
        self._items = history.search_history(self.search.text(), limit=500)
        now = _t.time()
        for h in self._items:
            ago = now - h.get("time", now)
            when = ("just now" if ago < 60 else f"{int(ago // 60)}m ago" if ago < 3600
                    else f"{int(ago // 3600)}h ago" if ago < 86400
                    else f"{int(ago // 86400)}d ago")
            title = h.get("title") or h.get("url", "")
            self.list.addItem(f"{title}    \u2014  {when}")
        if not self._items:
            self.list.addItem("(no history)")

    def _open(self, item):
        i = self.list.currentRow()
        if 0 <= i < len(self._items):
            self._window.new_tab(self._items[i]["url"])
            self.accept()

    def _clear(self):
        from PyQt6.QtWidgets import QMessageBox
        if QMessageBox.question(self, "Glass", "Clear all browsing history?") \
                == QMessageBox.StandardButton.Yes:
            history.clear_history(); self._refresh()


class SettingsDialog(QDialog):
    """Adjust the look of Glass's own chrome (top bar + search bar)."""

    DEFAULTS = {"chrome_bg": "#0c0f12", "address_bg": "#0a0d10"}

    def __init__(self, window):
        super().__init__(window)
        self.setWindowTitle("Glass settings")
        self.setMinimumWidth(420)
        self.setStyleSheet(
            "QDialog{background:#0d1117;} QLabel{color:#d7e0ea;}"
            "QLineEdit{background:#11161c;color:#d7e0ea;border:1px solid #1c2530;"
            "padding:6px;border-radius:5px;}"
            "QPushButton{background:#1a2330;color:#e6eef7;border:1px solid #28384a;"
            "border-radius:6px;padding:7px 12px;} QPushButton:hover{background:#243248;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16); lay.setSpacing(10)
        head = QLabel("Appearance")
        head.setStyleSheet("font-size:16px;font-weight:600;color:#6cf09a;")
        lay.addWidget(head)
        self._rows = {}
        lay.addLayout(self._color_row("Top bar / tab bar colour", "chrome_bg"))
        lay.addLayout(self._color_row("Search bar colour", "address_bg"))
        lay.addSpacing(6)
        data_btn = QPushButton("Saved data \u2013 view saved logins\u2026")
        data_btn.clicked.connect(lambda: SavedDataDialog(window).exec())
        lay.addWidget(data_btn)
        hist_btn = QPushButton("History \u2013 view & search\u2026")
        hist_btn.clicked.connect(lambda: HistoryDialog(window).exec())
        lay.addWidget(hist_btn)

        # ---- DRM / Widevine ------------------------------------------------
        lay.addSpacing(10)
        drm_head = QLabel("Streaming video (DRM)")
        drm_head.setStyleSheet("font-size:16px;font-weight:600;color:#6cf09a;")
        lay.addWidget(drm_head)
        note = QLabel("For Crunchyroll / Netflix etc. Glass downloads Widevine "
                      "automatically on first launch; you can also point it at the "
                      "widevinecdm.dll from an installed Chrome/Edge. Takes effect "
                      "after you restart Glass.")
        note.setWordWrap(True); note.setStyleSheet("color:#8b95a1;")
        lay.addWidget(note)
        wv_row = QHBoxLayout()
        self._wv_edit = QLineEdit(str(prefs.load("widevine_path", "")))
        self._wv_edit.setPlaceholderText("path to widevinecdm.dll (leave blank to auto-detect)")
        wv_browse = QPushButton("Browse\u2026")
        wv_browse.clicked.connect(self._pick_widevine)
        wv_auto = QPushButton("Auto-detect")
        wv_auto.clicked.connect(self._auto_widevine)
        wv_row.addWidget(self._wv_edit, 1); wv_row.addWidget(wv_browse); wv_row.addWidget(wv_auto)
        lay.addLayout(wv_row)
        wv_dl = QPushButton("Download Widevine now (from Google) \u2013 then restart Glass")
        wv_dl.clicked.connect(self._download_widevine)
        lay.addWidget(wv_dl)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Streaming / DRM sites:"))
        from PyQt6.QtWidgets import QComboBox
        self._drm_mode = QComboBox()
        self._drm_mode.addItem("Ask me (show a banner)", "ask")
        self._drm_mode.addItem("Open in Glass's Media Player (small window, bottom-right)", "window")
        self._drm_mode.addItem("Open in my system browser", "browser")
        self._drm_mode.addItem("Do nothing", "off")
        cur = str(prefs.load("drm_mode", "ask"))
        idx = max(0, self._drm_mode.findData(cur))
        self._drm_mode.setCurrentIndex(idx)
        mode_row.addWidget(self._drm_mode, 1)
        lay.addLayout(mode_row)

        hint = QLabel("The Media Player is a separate Edge/WebView2 window with its own "
                      "isolated, private profile \u2013 it can't see your real Edge data, "
                      "and telemetry/sync are off. Needed because this engine can't "
                      "play Widevine video itself.")
        hint.setWordWrap(True); hint.setStyleSheet("color:#8b95a1;font-size:11px;")
        lay.addWidget(hint)
        clr = QPushButton("Clear Media Player data (logins/cookies)")
        clr.clicked.connect(self._clear_drm_data)
        lay.addWidget(clr)

        # ---- performance ---------------------------------------------------
        lay.addSpacing(10)
        perf_head = QLabel("Performance")
        perf_head.setStyleSheet("font-size:16px;font-weight:600;color:#6cf09a;")
        lay.addWidget(perf_head)
        self._low_mem = QCheckBox("Low memory mode (recommended) \u2013 uses much less RAM")
        self._low_mem.setChecked(bool(prefs.load("low_memory", True)))
        self._low_mem.setStyleSheet("color:#c7d2dc;")
        lay.addWidget(self._low_mem)
        mhint = QLabel("Caps the web engine's memory (smaller caches, tighter JS heap, "
                       "fewer live tabs, lighter GPU use). May slightly reduce in-page "
                       "video quality. Takes effect after restarting Glass.")
        mhint.setWordWrap(True); mhint.setStyleSheet("color:#8b95a1;font-size:11px;")
        lay.addWidget(mhint)

        lay.addSpacing(6)
        brow = QHBoxLayout(); brow.addStretch(1)
        reset = QPushButton("Reset"); reset.clicked.connect(self._reset); brow.addWidget(reset)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject); brow.addWidget(cancel)
        save = QPushButton("Save")
        save.setStyleSheet("QPushButton{background:#1e7e4f;border-color:#2a9c63;}"
                           "QPushButton:hover{background:#249160;}")
        save.clicked.connect(self._save); brow.addWidget(save)
        lay.addLayout(brow)

    def _color_row(self, label, key):
        row = QHBoxLayout()
        row.addWidget(QLabel(label), 1)
        swatch = QLabel(); swatch.setFixedSize(26, 26)
        val = str(prefs.load(key, self.DEFAULTS[key]))
        edit = QLineEdit(val)
        pick = QPushButton("Pick\u2026")
        def paint():
            swatch.setStyleSheet(f"background:{edit.text().strip()};"
                                 "border:1px solid #28384a;border-radius:4px;")
        def choose():
            from PyQt6.QtGui import QColor
            c = QColorDialog.getColor(QColor(edit.text().strip()), self, label)
            if c.isValid():
                edit.setText(c.name()); paint()
        edit.textChanged.connect(paint); pick.clicked.connect(choose); paint()
        row.addWidget(swatch); row.addWidget(edit); row.addWidget(pick)
        self._rows[key] = edit
        return row

    def _pick_widevine(self):
        from PyQt6.QtWidgets import QFileDialog
        start = self._wv_edit.text().strip() or os.environ.get("PROGRAMFILES", "")
        lib = "widevinecdm.dll" if sys.platform.startswith("win") else (
              "libwidevinecdm.dylib" if sys.platform == "darwin" else "libwidevinecdm.so")
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {lib}", start, f"Widevine CDM ({lib});;All files (*)")
        if path:
            self._wv_edit.setText(path)

    def _auto_widevine(self):
        from PyQt6.QtWidgets import QMessageBox
        found = _find_widevine_dir()
        if found:
            self._wv_edit.setText(found)
        else:
            QMessageBox.information(self, "Auto-detect",
                                   "Couldn't find a Widevine module automatically. "
                                   "Install Chrome or Edge, or use 'Download Widevine now'.")

    def _download_widevine(self):
        from PyQt6.QtWidgets import QMessageBox
        from PyQt6.QtWidgets import QApplication
        try:
            import widevine_setup
        except Exception as e:
            QMessageBox.warning(self, "Widevine", f"Setup module missing: {e}")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok = widevine_setup.install(force=True, prefer_browser=False)
        except Exception as e:
            ok = False
            self._glass_window_log(f"[widevine] {e}") if hasattr(self, "_glass_window_log") else None
        finally:
            QApplication.restoreOverrideCursor()
        if ok:
            resolved = _find_widevine_dir()
            if resolved:
                self._wv_edit.setText(resolved)
            QMessageBox.information(self, "Widevine",
                                   "Downloaded. Restart Glass for DRM video to take effect.")
        else:
            QMessageBox.warning(self, "Widevine",
                                "Couldn't download Widevine (no internet, or blocked). "
                                "You can still use an installed Chrome/Edge, or the "
                                "'Open in system browser' button on DRM sites.")

    def _reset(self):
        for key, edit in self._rows.items():
            edit.setText(self.DEFAULTS[key])

    def _save(self):
        for key, edit in self._rows.items():
            v = edit.text().strip()
            if v:
                prefs.save(key, v)
        wv = self._wv_edit.text().strip()
        prev = str(prefs.load("widevine_path", ""))
        if wv:
            prefs.save("widevine_path", wv)
        else:
            prefs.delete("widevine_path")
        prefs.save("drm_mode", self._drm_mode.currentData())
        prev_lm = bool(prefs.load("low_memory", True))
        new_lm = bool(self._low_mem.isChecked())
        prefs.save("low_memory", new_lm)
        if new_lm != prev_lm:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Restart needed",
                                   "Low memory mode changes take effect the next time "
                                   "you start Glass.")

    def _clear_drm_data(self):
        from PyQt6.QtWidgets import QMessageBox
        import shutil
        d = os.path.join(HERE, ".drmdata")
        try:
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
            QMessageBox.information(self, "Media Player",
                                   "Cleared. Close any open Media Player window first "
                                   "if it doesn't fully clear.")
        except Exception as e:
            QMessageBox.warning(self, "Media Player", f"Couldn't clear: {e}")
        if wv != prev:                      # remind that DRM path needs a restart
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Restart needed",
                                   "The Widevine/DRM path changes take effect the next "
                                   "time you start Glass.")
        self.accept()


class GlassPage(QWebEnginePage):
    """Web page that listens for login-form submits (via a console-message
    bridge) so Glass can offer to save the credentials locally."""
    def __init__(self, profile, view, window):
        super().__init__(profile, view)
        self._glass_window = window

    def javaScriptConsoleMessage(self, level, message, line, source):
        if isinstance(message, str) and message.startswith("GLASS_CRED::"):
            try:
                import json
                d = json.loads(message[len("GLASS_CRED::"):])
                host = self.url().host()
                self._glass_window.offer_save_login(host, d.get("u", ""), d.get("p", ""))
            except Exception:
                pass
            return                       # swallow our own message
        if isinstance(message, str) and message.startswith("GLASS_DRM::"):
            try:
                self._glass_window._on_drm_status(self.url().host(),
                                                  message[len("GLASS_DRM::"):])
            except Exception:
                pass
            return


class GlassWebView(QWebEngineView):
    """A web view that opens link-target / popup requests as new Glass tabs,
    so right-click 'Open link in new tab' and target=_blank links work."""
    def __init__(self, window):
        super().__init__()
        self._glass_window = window

    def createWindow(self, _type):
        try:
            tab = self._glass_window.new_tab()
            return tab.view
        except Exception:
            return None


class Tab(QWidget):
    """A web view plus its own overlay of .glass panels.

    A tab can be *suspended*: its web view (and the Chromium renderer behind it)
    is freed and replaced by a tiny placeholder, dropping its memory to almost
    nothing. It reloads from `saved_url` when resumed."""
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.panels = []
        self.holders = {}
        self.ui_doc = None        # per-tab .glass doc (None = use the global ui/ UI)
        self.ui_vars = None       # per-tab live variables for ui_doc
        self.imports = {}         # {alias: python module} loaded for this doc
        self.suspended = False
        self.saved_url = ""       # url to restore when resumed
        self.title = "New Tab"
        self.placeholder = None
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        if view is not None:
            self.lay.addWidget(view)

    def clear_panels(self):
        for p in self.panels:
            p.setParent(None)
            p.deleteLater()
        self.panels = []


class DownloadsBar(QWidget):
    """A slim bottom bar showing active downloads with progress, like Chrome/Firefox."""
    def __init__(self, window):
        super().__init__(window)
        self.setStyleSheet(
            "DownloadsBar{background:#0c1117;border-top:1px solid #1c2530;}"
            "QLabel{color:#d7e0ea;}"
            "QProgressBar{background:#11161c;border:1px solid #1c2530;border-radius:4px;"
            "height:10px;text-align:center;color:transparent;}"
            "QProgressBar::chunk{background:#6cf09a;border-radius:3px;}"
            "QPushButton{background:transparent;color:#8b95a1;border:0;padding:2px 6px;}"
            "QPushButton:hover{color:#e7edf3;}")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(8, 4, 8, 4); self._lay.setSpacing(3)
        self.hide()

    def add(self, item):
        from PyQt6.QtWidgets import QProgressBar
        row = QWidget(); rl = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(8)
        name = QLabel(item.downloadFileName() if hasattr(item, "downloadFileName") else "file")
        name.setMinimumWidth(180)
        bar = QProgressBar(); bar.setRange(0, 100); bar.setValue(0); bar.setFixedWidth(220)
        status = QLabel("starting\u2026"); status.setMinimumWidth(120)
        close = QPushButton("\u2715")
        rl.addWidget(name); rl.addWidget(bar); rl.addWidget(status); rl.addStretch(1); rl.addWidget(close)
        self._lay.addWidget(row); self.show()

        def fmt(n):
            for unit in ("B", "KB", "MB", "GB"):
                if n < 1024:
                    return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
                n /= 1024
            return f"{n:.1f} TB"

        def update():
            try:
                got = item.receivedBytes(); tot = item.totalBytes()
            except Exception:
                got, tot = 0, 0
            if tot > 0:
                pct = int(got * 100 / tot)
                bar.setValue(pct)
                status.setText(f"{fmt(got)} / {fmt(tot)}  ({pct}%)")
            else:
                bar.setRange(0, 0)              # indeterminate
                status.setText(fmt(got))

        def finished():
            bar.setRange(0, 100); bar.setValue(100)
            status.setText("\u2713 done")
            close.setText("Open")
            try:
                close.clicked.disconnect()
            except Exception:
                pass
            close.clicked.connect(lambda: self._reveal(item))

        def remove():
            row.setParent(None); row.deleteLater()
            if self._lay.count() == 0:
                self.hide()

        close.clicked.connect(remove)
        for sig in ("receivedBytesChanged", "totalBytesChanged"):
            try:
                getattr(item, sig).connect(update)
            except Exception:
                pass
        try:
            item.isFinishedChanged.connect(finished)
        except Exception:
            try:
                item.finished.connect(finished)
            except Exception:
                pass
        update()

    def _reveal(self, item):
        import subprocess
        try:
            path = os.path.join(item.downloadDirectory(), item.downloadFileName())
            if sys.platform.startswith("win"):
                os.startfile(os.path.dirname(path))   # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", os.path.dirname(path)])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            pass


class ZoomToast(QLabel):
    """A small fading overlay that shows the current zoom level."""
    def __init__(self, window):
        super().__init__(window)
        self.setStyleSheet(
            "background:rgba(10,12,16,0.92);color:#e7edf3;border:1px solid #2a3a4d;"
            "border-radius:8px;padding:8px 14px;font-size:15px;font-weight:600;")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hide()
        self._timer = QTimer(self); self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_zoom(self, pct):
        self.setText(f"{pct}%")
        self.adjustSize()
        p = self.parent()
        self.move((p.width() - self.width()) // 2, 70)
        self.show(); self.raise_()
        self._timer.start(1100)


STREAMING_HOSTS = ("crunchyroll.com", "netflix.com", "disneyplus.com", "hulu.com",
                   "max.com", "hbomax.com", "primevideo.com", "spotify.com",
                   "funimation.com", "vrv.co", "peacocktv.com", "paramountplus.com")

# Instantiate a Widevine CDM; if this fails, protected video can't play.
_DRM_CHECK_JS = (
    "(function(){try{"
    "if(!navigator.requestMediaKeySystemAccess){console.log('GLASS_DRM::none');return;}"
    "navigator.requestMediaKeySystemAccess('com.widevine.alpha',[{initDataTypes:['cenc'],"
    "videoCapabilities:[{contentType:'video/mp4;codecs=\"avc1.42E01E\"',"
    "robustness:'SW_SECURE_CRYPTO'}]}])"
    ".then(function(a){return a.createMediaKeys();})"
    ".then(function(){console.log('GLASS_DRM::ok');})"
    ".catch(function(){console.log('GLASS_DRM::fail');});"
    "}catch(e){console.log('GLASS_DRM::fail');}})();")


class NoticeBar(QWidget):
    """A dismissible banner under the chrome for important notices
    (e.g. 'Widevine DRM unavailable')."""
    def __init__(self, window):
        super().__init__(window)
        self.setStyleSheet(
            "NoticeBar{background:#2a2410;border-bottom:1px solid #5a4a1c;}"
            "QLabel{color:#f0e6c0;}"
            "QPushButton{background:#3a3216;color:#ffe9a8;border:1px solid #6a5a24;"
            "border-radius:6px;padding:5px 10px;}QPushButton:hover{background:#4a3f1e;}")
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(12, 6, 8, 6); self._lay.setSpacing(8)
        self._label = QLabel(""); self._label.setWordWrap(True)
        self._lay.addWidget(self._label, 1)
        self._btns = []
        self.hide()

    def show_message(self, text, actions=None):
        self._label.setText("\u26a0  " + text)
        for b in self._btns:
            b.setParent(None); b.deleteLater()
        self._btns = []
        for label, cb in (actions or []):
            b = QPushButton(label)
            b.clicked.connect(lambda _c, f=cb: f())
            self._lay.insertWidget(self._lay.count() - 0, b)
            self._btns.append(b)
        x = QPushButton("\u2715"); x.setFixedWidth(30)
        x.clicked.connect(self.hide)
        self._lay.addWidget(x); self._btns.append(x)
        self.show()


class ChromeBar(QWidget):
    """The top bar doubles as the window's title bar: drag to move, double-click
    to maximize (Brave-style, since the OS title bar is hidden)."""
    def __init__(self, window):
        super().__init__()
        self._win = window

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            wh = self._win.windowHandle()
            if wh is not None:
                wh.startSystemMove()
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        self._win._toggle_max()
        super().mouseDoubleClickEvent(e)


class _ResizeHandle(QWidget):
    """Thin invisible border strip that lets a frameless window be resized
    natively (keeps OS snapping/edge-resize)."""
    def __init__(self, window, edge, cursor):
        super().__init__(window)
        self._win = window
        self._edge = edge
        self.setCursor(cursor)
        self.setStyleSheet("background:transparent;")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            wh = self._win.windowHandle()
            if wh is not None:
                wh.startSystemResize(self._edge)


class GlassWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.home_url = HOME
        self._session_ready = False
        self._was_max = False
        self.setWindowTitle("Glass")
        # Frameless: no OS title bar - Glass's own bar is the title bar (Brave-style)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(1200, 800)

        # ---- private, telemetry-free profile ------------------------------
        self.profile = QWebEngineProfile("glass-profile", self)
        # A clean, current Chrome UA. The old UA ended in "Glass", which some
        # streaming sites reject (degraded player / endless spinner).
        self.profile.setHttpUserAgent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        )
        self.interceptor = Interceptor(self)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self._install_web_scripts()
        try:                                  # make "Save link/image as" work
            self.profile.downloadRequested.connect(self._on_download)
        except Exception:
            pass
        # small on-disk cache instead of a large in-memory one
        try:
            self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
            self.profile.setHttpCacheMaximumSize(32 * 1024 * 1024)   # 32 MB
        except Exception:
            pass
        # --- privacy: no spell-check dictionary downloads ---
        try:
            self.profile.setSpellCheckEnabled(False)
        except Exception:
            pass
        # --- privacy: block third-party (cross-site tracking) cookies ---
        try:
            store = self.profile.cookieStore()

            def _cookie_filter(req):
                # keep first-party cookies (logins etc.), drop cross-site ones
                if getattr(req, "thirdParty", False):
                    return False
                return True
            store.setCookieFilter(_cookie_filter)
        except Exception:
            pass

        self.api = BrowserAPI(self)
        self.ui_rules = dsl.Document()
        self.variables = {}         # live variable values (mutated by set:/do:)
        self.base_ui_rules = dsl.Document()   # the ui/ folder UI (fallback for tabs)
        self.base_variables = {}
        self.saved_positions = {}   # remembered menu positions (for `remember` trait)
        self.closed_panels = set()  # panels the user closed - stay closed across tabs
        self.server = None          # GlassServer when hosting via /serv
        self.current_project_dir = None   # set when a project folder is opened
        self.max_live_tabs = 2 if _LOWMEM else 4   # renderers kept alive; rest suspended
        self._live_order = []       # live tabs, least-recently-used first

        os.makedirs(PROJECTS_DIR, exist_ok=True)
        renderer.ASSET_DIRS = [PROJECTS_DIR, UI_DIR, HERE]

        self._build_chrome()
        self._build_logdock()

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        new_btn = QPushButton("+")
        new_btn.setFixedSize(26, 22)
        new_btn.setToolTip("New tab (Ctrl+T)")
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,0.08);color:#e6eef7;border:none;"
            "font-size:15px;font-weight:600;border-radius:4px;}"
            "QPushButton:hover{background:rgba(255,255,255,0.20);}")
        new_btn.clicked.connect(lambda: self.new_tab(self.home_url))
        # wrap with a little padding so it isn't clipped at the window edge
        corner = QWidget()
        ch = QHBoxLayout(corner)
        ch.setContentsMargins(4, 2, 8, 2)
        ch.setSpacing(0)
        ch.addWidget(new_btn)
        self.tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        self.apply_chrome_theme()

        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self.chrome)
        self.notice = NoticeBar(self)
        cl.addWidget(self.notice)
        cl.addWidget(self.tabs, 1)
        self.downloads_bar = DownloadsBar(self)
        cl.addWidget(self.downloads_bar)
        self.setCentralWidget(central)
        self._zoom_toast = ZoomToast(self)
        QApplication.instance().installEventFilter(self)
        self._init_resize_handles()

        self._build_shortcuts()
        self._setup_ui_watcher()

        # ---- game runtime: a ~60fps frame loop driving the active world ----
        self.world = None
        self._cursor_hidden = False
        self._last_frame = None
        self._next_audio_id = 2001
        import audioctl
        self.audio = audioctl.AudioController(self)
        # ---- package system: live {Var} refresh for non-game UIs -----------
        self._ui_var_timer = QTimer(self)
        self._ui_var_timer.setInterval(140)
        self._ui_var_timer.timeout.connect(self._tick_ui_vars)
        self._ui_var_timer.start()
        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._frame)
        self._frame_timer.start(0)      # uncapped - see RENDER_INTERVAL_MS in renderer.py
        QApplication.instance().installEventFilter(self)   # capture keys for input.*

        self.new_tab(self.home_url)
        self.load_ui()

    # ---- game frame loop ---------------------------------------------------
    def _frame(self):
        w = self.world
        if w is None:
            if getattr(self, "_cursor_hidden", False):
                self._release_cursor()      # world ended - give the cursor back
            return
        import time as _t
        now = _t.perf_counter()
        dt = 0.016 if self._last_frame is None else (now - self._last_frame)
        self._last_frame = now
        try:
            w.begin_frame(dt)
            w.run_setup_once()
            w.maybe_fit()
            self._apply_cursor(w)           # BEFORE update, so mouse.dx/dy are fresh
            w.run_update()
            w.tick_timers()                 # fire any elapsed after{} blocks
            renderer.spawn_pending(w, getattr(w, "host", None))
            w.resolve_collisions()
            renderer.refresh_var_bindings(w.vars)
            renderer.refresh_media_bindings(w.vars)
            if getattr(w, "dynamic", False):
                for obj in list(w.objects.values()):
                    renderer.apply_transform(obj)
            w.end_frame()
        except Exception as e:
            self.log(f"[engine] frame error: {e}")

    def _release_cursor(self):
        """Undo any cursor hiding/override (called when the game ends)."""
        from PyQt6.QtWidgets import QApplication
        while getattr(self, "_cursor_hidden", False):
            QApplication.restoreOverrideCursor()
            self._cursor_hidden = False

    def _apply_cursor(self, w):
        """Honor cursor.lock / cursor.hide / cursor.confine and feed mouse-look
        deltas (mouse.dx / mouse.dy) back to scripts."""
        cs = getattr(w, "cursor", None)
        if cs is None:
            return
        from PyQt6.QtGui import QCursor
        from PyQt6.QtWidgets import QApplication
        host = w.host or self
        want_hidden = bool(cs["hide"] or cs["lock"])

        # reliable global hide via an override cursor, toggled only on change
        if want_hidden and not getattr(self, "_cursor_hidden", False):
            QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)
            self._cursor_hidden = True
        elif not want_hidden and getattr(self, "_cursor_hidden", False):
            self._release_cursor()

        # only manipulate the pointer while our window is the active one, so we
        # never fight the user's mouse when they've tabbed away
        active = self.isActiveWindow()
        try:
            if cs["lock"]:
                center = host.mapToGlobal(host.rect().center())
                pos = QCursor.pos()
                if active:
                    w.mouse["dx"] = float(pos.x() - center.x())
                    w.mouse["dy"] = float(pos.y() - center.y())
                    QCursor.setPos(center)      # recenter for endless look
                # local mouse position = centre of host while locked
                w.mouse["x"] = float(host.rect().center().x())
                w.mouse["y"] = float(host.rect().center().y())
            elif cs["confine"] and active:
                tl = host.mapToGlobal(host.rect().topLeft())
                br = host.mapToGlobal(host.rect().bottomRight())
                pos = QCursor.pos()
                x = min(max(pos.x(), tl.x()), br.x())
                y = min(max(pos.y(), tl.y()), br.y())
                if x != pos.x() or y != pos.y():
                    QCursor.setPos(x, y)
        except Exception:
            pass

    def eventFilter(self, obj, event):
        et = event.type()
        if et in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and self.world is not None:
            name = _key_name(event)
            if name:
                if et == QEvent.Type.KeyPress and not event.isAutoRepeat():
                    self.world.input.key_down(name)
                elif et == QEvent.Type.KeyRelease and not event.isAutoRepeat():
                    self.world.input.key_up(name)
        elif et == QEvent.Type.MouseMove and self.world is not None:
            w = self.world
            if not w.cursor.get("lock"):          # locked mode uses centre-delta
                try:
                    host = w.host or self
                    gp = event.globalPosition().toPoint()
                    lp = host.mapFromGlobal(gp)
                    nx, ny = float(lp.x()), float(lp.y())
                    w.mouse["dx"] += nx - w.mouse["x"]
                    w.mouse["dy"] += ny - w.mouse["y"]
                    w.mouse["x"], w.mouse["y"] = nx, ny
                except Exception:
                    pass
        elif et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease) \
                and self.world is not None:
            try:
                if event.button() == Qt.MouseButton.LeftButton:
                    self.world.mouse["down"] = (et == QEvent.Type.MouseButtonPress)
            except Exception:
                pass
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_resize_handles()
        # keep screen.width / screen.height current and re-stretch bound widgets
        try:
            renderer.set_screen(*self._ui_area())
        except Exception:
            pass

    # ---- top chrome --------------------------------------------------------
    def _build_chrome(self):
        self.chrome = ChromeBar(self)
        self.chrome.setStyleSheet(
            "background:#0c0f12; color:#dfe6ee; border-bottom:1px solid #1c2228;"
        )
        h = QHBoxLayout(self.chrome)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        def nav_btn(icon, slot, w=34, tip="", text=""):
            from PyQt6.QtCore import QSize
            b = QPushButton()
            b.setFixedWidth(w)
            b.setStyleSheet(theme.button())
            ic = images.button_icon(icon)
            if not ic.isNull():
                b.setIcon(ic); b.setIconSize(QSize(16, 16))
                b.setToolTip(tip or icon)
            else:
                b.setText(text or icon)
            b.clicked.connect(slot)
            return b

        h.addWidget(nav_btn("back", lambda: self.current_view().back(), tip="Back"))
        h.addWidget(nav_btn("forward", lambda: self.current_view().forward(), tip="Forward"))
        h.addWidget(nav_btn("reload", lambda: self.current_view().reload(), tip="Reload"))

        self.address = QLineEdit()
        self.address.setStyleSheet(
            "background:#0a0d10;color:#eaf2fb;border:1px solid #232a31;"
            "border-radius:8px;padding:7px 10px;"
        )
        self.address.setPlaceholderText("Search or enter address - nothing about this is logged externally")
        self.address.returnPressed.connect(lambda: self.navigate(self.address.text()))
        h.addWidget(self.address, 1)

        self.block_label = QPushButton()
        self.block_label.setStyleSheet(
            "QPushButton{background:#10231a;color:#6cf09a;border:1px solid #1f4a35;"
            "border-radius:6px;padding:5px 9px;}QPushButton:hover{background:#16321f;}"
        )
        self.block_label.clicked.connect(self.api.toggleblock)
        h.addWidget(self.block_label)
        self.refresh_block_label()

        h.addWidget(nav_btn("source", self.view_source, 40, tip="View source", text="</>"))
        h.addWidget(nav_btn("log", self.toggle_network_log, 40, tip="Network log"))
        h.addWidget(nav_btn("ui", self.reload_ui, 40, tip="Reload UI"))
        h.addWidget(nav_btn("settings", self.open_settings, 34, tip="Settings"))
        h.addWidget(nav_btn("edit", self.open_editor, 40, tip="Open editor"))

        # ---- window controls (frameless: we draw our own) ----
        def win_btn(text, slot, hover):
            b = QPushButton(text); b.setFixedSize(38, 26)
            b.setStyleSheet(
                "QPushButton{background:transparent;color:#cfd8e2;border:0;"
                "font-size:13px;border-radius:5px;}"
                f"QPushButton:hover{{background:{hover};color:#ffffff;}}")
            b.clicked.connect(slot)
            return b
        h.addSpacing(4)
        h.addWidget(win_btn("\u2013", self.showMinimized, "#243248"))
        self._max_btn = win_btn("\u25a1", self._toggle_max, "#243248")
        h.addWidget(self._max_btn)
        h.addWidget(win_btn("\u2715", self.close, "#c0392b"))

    def _toggle_max(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _on_fullscreen(self, request):
        """A web page (YouTube/Crunchyroll video) asked to go fullscreen:
        accept it, hide our chrome + resize borders, and fill the screen."""
        try:
            request.accept()
        except Exception:
            return
        on = request.toggleOn() if hasattr(request, "toggleOn") else not self.isFullScreen()
        if on:
            self._was_max = self.isMaximized()
            self.chrome.hide()
            self.downloads_bar.hide()
            self._set_handles_visible(False)
            self.showFullScreen()
        else:
            self.showNormal()
            if self._was_max:
                self.showMaximized()
            self.chrome.show()
            self._set_handles_visible(True)

    # ---- frameless window resize handles -----------------------------------
    def _init_resize_handles(self):
        from PyQt6.QtGui import QCursor
        E = Qt.Edge
        C = Qt.CursorShape
        specs = [
            (E.TopEdge, C.SizeVerCursor), (E.BottomEdge, C.SizeVerCursor),
            (E.LeftEdge, C.SizeHorCursor), (E.RightEdge, C.SizeHorCursor),
            (E.TopEdge | E.LeftEdge, C.SizeFDiagCursor),
            (E.BottomEdge | E.RightEdge, C.SizeFDiagCursor),
            (E.TopEdge | E.RightEdge, C.SizeBDiagCursor),
            (E.BottomEdge | E.LeftEdge, C.SizeBDiagCursor),
        ]
        self._handles = []
        for edge, cur in specs:
            hnd = _ResizeHandle(self, edge, QCursor(cur))
            hnd.show()
            self._handles.append((hnd, edge))
        self._position_resize_handles()

    def _position_resize_handles(self):
        if not getattr(self, "_handles", None):
            return
        T = 6
        w, h = self.width(), self.height()
        E = Qt.Edge
        geom = {
            E.TopEdge: (T, 0, w - 2 * T, T),
            E.BottomEdge: (T, h - T, w - 2 * T, T),
            E.LeftEdge: (0, T, T, h - 2 * T),
            E.RightEdge: (w - T, T, T, h - 2 * T),
            E.TopEdge | E.LeftEdge: (0, 0, T, T),
            E.BottomEdge | E.RightEdge: (w - T, h - T, T, T),
            E.TopEdge | E.RightEdge: (w - T, 0, T, T),
            E.BottomEdge | E.LeftEdge: (0, h - T, T, T),
        }
        for hnd, edge in self._handles:
            hnd.setGeometry(*geom[edge])
            hnd.raise_()

    def _set_handles_visible(self, vis):
        for hnd, _ in getattr(self, "_handles", []):
            hnd.setVisible(vis)

    def open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.apply_chrome_theme()

    def apply_chrome_theme(self):
        """Frosted-glass top bar, tab bar and search bar (overridable in settings)."""
        import theme
        cbg = prefs.load("chrome_bg", "")
        abg = prefs.load("address_bg", "")
        chrome_css = (f"background:{cbg};" if cbg else
                      f"background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                      f"stop:0 {theme.SHEEN}, stop:0.5 {theme.BASE}, stop:1 {theme.BASE});")
        self.chrome.setObjectName("chrome")
        self.chrome.setStyleSheet(
            f"QWidget#chrome{{{chrome_css}border-bottom:1px solid {theme.EDGE};}}")
        self.address.setStyleSheet(theme.input_field(abg or None))
        self.tabs.setStyleSheet(
            f"QTabWidget::pane{{border:0;background:{theme.BASE};}}"
            f"QTabBar{{background:transparent;}}"
            f"QTabBar::tab{{background:transparent;color:{theme.MUTED};"
            f"padding:7px 14px;border:0;border-bottom:2px solid transparent;}}"
            f"QTabBar::tab:selected{{color:{theme.TEXT};"
            f"border-bottom:2px solid {theme.ACCENT};}}"
            f"QTabBar::tab:hover{{color:{theme.TEXT};}}")

    def refresh_block_label(self):
        on = self.interceptor.enabled
        n = self.interceptor.blocked_count
        self.block_label.setText(("\u25cf blocking " if on else "\u25cb off ") + f"({n})")

    # ---- network log -------------------------------------------------------
    def _build_logdock(self):
        self.logview = QPlainTextEdit()
        self.logview.setReadOnly(True)
        self.logview.setStyleSheet("background:#07090b;color:#9fb3c8;font-family:Consolas,monospace;")
        self.logdock = QDockWidget("Network / Event Log (everything, in the open)", self)
        self.logdock.setWidget(self.logview)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.logdock)
        self.logdock.hide()

    def log(self, msg):
        self.logview.appendPlainText(msg)
        self.refresh_block_label()

    def toggle_network_log(self):
        self.logdock.setVisible(not self.logdock.isVisible())

    # ---- tabs / views ------------------------------------------------------
    def _install_web_scripts(self):
        """Inject the hatched scrollbar CSS into every page AND into shadow roots
        (YouTube etc. render scrollable areas inside shadow DOM, which a plain
        <style> in <head> can't reach)."""
        try:
            from PyQt6.QtWebEngineCore import QWebEngineScript
            css = theme.web_scrollbar_css().replace("\\", "\\\\").replace("`", "\\`")
            src = (
                "(function(){var CSS=`" + css + "`;"
                "var sheet=null;try{sheet=new CSSStyleSheet();sheet.replaceSync(CSS);}catch(e){}"
                "var seen=new WeakSet();"
                "function apply(root){if(seen.has(root))return;seen.add(root);try{"
                "  if(sheet&&'adoptedStyleSheets' in root){"
                "    root.adoptedStyleSheets=[].concat(root.adoptedStyleSheets,sheet);return;}"
                "}catch(e){}"
                "  try{var h=root.head||root;"
                "    var s=document.createElement('style');s.setAttribute('data-glass-sb','1');"
                "    s.textContent=CSS;h.appendChild(s);}catch(e){}}"
                "function walk(node){apply(node);try{var els=node.querySelectorAll?"
                "node.querySelectorAll('*'):[];for(var i=0;i<els.length;i++){"
                "if(els[i].shadowRoot)walk(els[i].shadowRoot);}}catch(e){}}"
                "function run(){try{walk(document);}catch(e){}}"
                "run();document.addEventListener('DOMContentLoaded',run);"
                # debounce: coalesce bursts of mutations into one sweep, and only
                # sweep for NEW shadow roots (seen-set skips already-styled ones).
                "var t=null;function sched(){if(t)return;t=setTimeout(function(){"
                "t=null;run();},600);}"
                "try{var mo=new MutationObserver(function(muts){"
                "for(var i=0;i<muts.length;i++){var a=muts[i].addedNodes;"
                "for(var j=0;j<a.length;j++){if(a[j].nodeType===1&&a[j].shadowRoot){sched();return;}}}"
                "});mo.observe(document.documentElement,{childList:true,subtree:true});}catch(e){}"
                # a few slow sweeps to catch late shadow roots, then stop.
                "var c=0,iv=setInterval(function(){run();if(++c>6)clearInterval(iv);},2500);})();")
            sc = QWebEngineScript()
            sc.setName("glass-scrollbar")
            sc.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
            sc.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
            sc.setRunsOnSubFrames(True)
            sc.setSourceCode(src)
            self.profile.scripts().insert(sc)
            self._web_sb_js = src
        except Exception:
            self._web_sb_js = ""

    def _on_download(self, item):
        """Ask where to save (like a normal browser), then show progress."""
        try:
            from PyQt6.QtCore import QStandardPaths
            from PyQt6.QtWidgets import QFileDialog
            dldir = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DownloadLocation) or os.path.join(HERE, "downloads")
            suggested = (item.downloadFileName() if hasattr(item, "downloadFileName") else "") or "download"
            path, _ = QFileDialog.getSaveFileName(self, "Save file as",
                                                  os.path.join(dldir, suggested))
            if not path:
                item.cancel()
                return
            item.setDownloadDirectory(os.path.dirname(path))
            item.setDownloadFileName(os.path.basename(path))
            item.accept()
            self.downloads_bar.add(item)
            self.log(f"[download] {os.path.basename(path)} -> {os.path.dirname(path)}")
        except Exception as e:
            self.log(f"[download] failed: {e}")

    # ---- zoom (Ctrl+scroll), shown in a toast and saved per-site ----------
    def eventFilter(self, obj, event):
        try:
            if event.type() == QEvent.Type.Wheel and \
                    (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                v = self.current_view()
                if v is not None:
                    self._zoom_step(v, 1 if event.angleDelta().y() > 0 else -1)
                    return True
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def _zoom_domain(self, view):
        try:
            return view.url().host()
        except Exception:
            return ""

    def _zoom_step(self, view, direction):
        cur = view.zoomFactor()
        new = max(0.3, min(3.0, round((cur + direction * 0.1) * 10) / 10))
        view.setZoomFactor(new)
        self._zoom_toast.show_zoom(int(round(new * 100)))
        d = self._zoom_domain(view)
        if d:
            z = prefs.load("zoom_levels", {})
            if not isinstance(z, dict):
                z = {}
            if abs(new - 1.0) < 0.001:
                z.pop(d, None)              # default zoom -> don't store
            else:
                z[d] = new
            prefs.save("zoom_levels", z)

    def _apply_saved_zoom(self, view):
        d = self._zoom_domain(view)
        if not d:
            return
        z = prefs.load("zoom_levels", {})
        if isinstance(z, dict) and d in z:
            try:
                view.setZoomFactor(float(z[d]))
            except Exception:
                pass

    def _on_drm_status(self, host, status):
        """A streaming site reported its Widevine/DRM capability."""
        self.log(f"[drm] {host}: {status}")
        if status in ("fail", "none"):
            self.notice.show_message(
                "This build's engine can't play DRM video. Open it in Glass's "
                "separate Media Player (a small isolated Edge/WebView2 window) instead?",
                [("Open Media Player", lambda: self._open_drm_window(
                    self.current_view().url().toString() if self.current_view() else "")),
                 ("Always do this", self._enable_drm_window),
                 ("System browser", self._open_system_browser),
                 ("Why?", self._drm_help)])

    def _open_drm_window(self, url):
        """Launch the isolated 'Media Player - Glass' window (WebView2) in the
        bottom-right, themed from the user's Glass settings. Separate process."""
        if not url:
            v = self.current_view()
            url = v.url().toString() if v is not None else ""
        args = [sys.executable, os.path.join(HERE, "drmwindow.py"), url]
        try:
            g = self.frameGeometry()
            tl = self.mapToGlobal(self.rect().topLeft())
            args += [str(tl.x()), str(tl.y()), str(g.width()), str(g.height())]
        except Exception:
            pass
        try:
            import subprocess
            subprocess.Popen(args)
        except Exception as e:
            self.log(f"[drm] couldn't open Media Player: {e}")
            self._open_system_browser()

    def _enable_drm_window(self):
        try:
            prefs.save("drm_mode", "window")
        except Exception:
            pass
        self.notice.hide()
        self._open_drm_window("")

    def _open_system_browser(self):
        import webbrowser
        v = self.current_view()
        if v is not None:
            try:
                webbrowser.open(v.url().toString())
            except Exception as e:
                self.log(f"[drm] open in system browser failed: {e}")

    def _retry_no_adblock(self):
        try:
            self.interceptor.enabled = False
            self.refresh_block_label()
        except Exception:
            pass
        self.notice.hide()
        v = self.current_view()
        if v is not None:
            v.reload()

    def _drm_help(self):
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.information(
            self, "About DRM video",
            "Sites like Crunchyroll and Netflix encrypt video with Widevine DRM. "
            "Glass is built on the Qt WebEngine browser engine, which - unlike "
            "Chrome or Brave - does not ship Widevine, so DRM video is not "
            "guaranteed to work.\n\n"
            "Glass tries to borrow the Widevine module from an installed Chrome or "
            "Edge, but that can still fail if their module is a newer version than "
            "this engine understands. That's most likely what's happening here.\n\n"
            "Your options:\n"
            "\u2022 Glass tries to download Widevine automatically on first launch, "
            "and also borrows it from an installed Chrome/Edge. If it's still not "
            "working, open Settings \u2192 'Streaming video (DRM)' and use 'Download "
            "Widevine now', or Browse to a widevinecdm.dll, then restart.\n"
            "\u2022 Use 'Open in system browser' to watch it in Chrome/Edge, where "
            "DRM always works.\n\n"
            "Note: some Python/Qt WebEngine builds ship with Widevine disabled; in "
            "that case no CDM will load and the system-browser option is the fix.\n\n"
            "Non-DRM video (YouTube and most embeds) is unaffected.")

    def _make_view(self):
        view = GlassWebView(self)
        page = GlassPage(self.profile, view, self)
        view.setPage(page)
        self._harden_view(view, page)
        view.urlChanged.connect(self._on_url_changed)
        view.loadFinished.connect(lambda ok, v=view: self._on_load_finished(v))
        view.titleChanged.connect(self._on_title_changed)
        return view

    def _harden_view(self, view, page):
        # privacy-oriented engine settings
        try:
            s = view.settings()
            A = s.WebAttribute
            # don't let WebRTC reveal your real/local IP addresses
            s.setAttribute(A.WebRTCPublicInterfacesOnly, True)
            # let sites' copy/paste menu items work (e.g. YouTube "Copy video URL")
            s.setAttribute(A.JavascriptCanAccessClipboard, True)
            s.setAttribute(A.JavascriptCanPaste, True)
            # let video players (YouTube, Crunchyroll) go fullscreen
            try:
                s.setAttribute(A.FullScreenSupportEnabled, True)
            except Exception:
                pass
            # don't auto-load geolocation on insecure origins
            try:
                s.setAttribute(A.AllowGeolocationOnInsecureOrigins, False)
            except Exception:
                pass
        except Exception:
            pass
        try:
            page.fullScreenRequested.connect(self._on_fullscreen)
        except Exception:
            pass
        # deny camera / mic / location / notification grabs by default
        try:
            page.featurePermissionRequested.connect(
                lambda origin, feature, pg=page: self._deny_feature(pg, origin, feature))
        except Exception:
            pass

    def _deny_feature(self, page, origin, feature):
        try:
            page.setFeaturePermission(
                origin, feature,
                QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)
            self.log(f"[privacy] denied {feature} for {origin.host()}")
        except Exception:
            pass

    def _collect_session(self):
        tabs = []
        for i in range(self.tabs.count()):
            t = self.tabs.widget(i)
            if not isinstance(t, Tab):
                continue
            url = t.saved_url or ""
            if not url and t.view is not None:
                try:
                    url = t.view.url().toString()
                except Exception:
                    url = ""
            if url.startswith(("http://", "https://")):
                tabs.append({"url": url, "title": getattr(t, "title", "") or url})
        return tabs

    def _save_session(self):
        if not getattr(self, "_session_ready", False):
            return                    # don't overwrite last session before we offer restore
        try:
            history.save_session(self._collect_session())
        except Exception:
            pass

    def _offer_restore(self):
        prev = []
        try:
            prev = history.load_session()
        except Exception:
            prev = []
        self._session_ready = True    # from now on, saving overwrites the old session
        if prev:
            dlg = RestoreSessionDialog(self, prev)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                for item in prev:
                    self.new_tab(item.get("url", ""))
        self._save_session()

    def open_history(self):
        HistoryDialog(self).exec()

    def new_tab(self, url=""):
        view = self._make_view()
        tab = Tab(view)
        tab.audio_id = self._next_audio_id
        self._next_audio_id += 1
        idx = self.tabs.addTab(tab, "New Tab")
        self._live_order.append(tab)
        self.tabs.setCurrentIndex(idx)
        if url:
            view.setUrl(QUrl(url))
        self._enforce_tab_budget()
        return tab

    def close_tab(self, index):
        if self.tabs.count() <= 1:
            self.close()
            return
        w = self.tabs.widget(index)
        if w in self._live_order:
            self._live_order.remove(w)
        self.tabs.removeTab(index)
        w.deleteLater()
        self._save_session()

    # ---- background-tab discarding (memory) --------------------------------
    def _touch_tab(self, tab):
        if tab in self._live_order:
            self._live_order.remove(tab)
        self._live_order.append(tab)

    def _suspend_tab(self, tab):
        if tab is None or tab.suspended or tab.view is None:
            return
        v = tab.view
        tab.saved_url = v.url().toString() or tab.saved_url
        try:
            v.stop()
        except Exception:
            pass
        tab.lay.removeWidget(v)
        v.setParent(None)
        v.deleteLater()
        tab.view = None
        tab.suspended = True
        tab.clear_panels()          # the .glass overlay is rebuilt on resume
        ph = QLabel("This tab is asleep to save memory.\nSelect it to reload.")
        ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph.setStyleSheet("color:#7f8c9b;background:#0b0f14;font-size:13px;")
        tab.placeholder = ph
        tab.lay.addWidget(ph)
        if tab in self._live_order:
            self._live_order.remove(tab)
        i = self.tabs.indexOf(tab)
        if i >= 0:
            self.tabs.setTabToolTip(i, "asleep (click to reload)")
        self.log(f"[mem] suspended tab: {tab.title}")

    def _resume_tab(self, tab):
        if tab is None or not tab.suspended:
            return
        if tab.placeholder is not None:
            tab.placeholder.setParent(None)
            tab.placeholder.deleteLater()
            tab.placeholder = None
        v = self._make_view()
        tab.view = v
        tab.lay.addWidget(v)
        tab.suspended = False
        self._touch_tab(tab)
        i = self.tabs.indexOf(tab)
        if i >= 0:
            self.tabs.setTabToolTip(i, "")
        v.setUrl(QUrl(tab.saved_url or self.home_url))

    def _enforce_tab_budget(self):
        current = self.current_tab()
        live = [t for t in self._live_order if not t.suspended]
        for victim in tabbudget.victims_to_suspend(live, current, self.max_live_tabs):
            self._suspend_tab(victim)

    def current_tab(self):
        return self.tabs.currentWidget()

    def current_view(self):
        t = self.current_tab()
        return t.view if t else None

    def navigate(self, text):
        text = (text or "").strip()
        if not text:
            return

        # ---- .glass loading via the address bar ---------------------------
        low = text.lower()
        m = re.match(r'^(?:glass://)?([\w.\-]+):(\d+)/(.+)$', text, re.I)
        if m:
            host, port, fname = m.groups()
            return self.load_remote(host, int(port), fname)
        if low.endswith(".glass/my"):
            return self.load_glass(text[:-3])            # single file: drop "/my"
        if low.endswith(".glass/serv"):
            return self.serve_current(text[:-5])
        if low.endswith("/my") and "." not in text[:-3]:
            return self.open_project(text[:-3])          # project folder: drop "/my"
        if low.endswith("/serv") and "." not in text[:-5]:
            self.open_project(text[:-5])
            return self.serve_current(None)

        if "." in text and " " not in text and not text.startswith("javascript:"):
            url = text if "://" in text else "https://" + text
        else:
            url = SEARCH_URL + QUrl.toPercentEncoding(text).data().decode()
        # any DuckDuckGo page -> force its dark theme
        if "duckduckgo.com" in url.lower() and "kae=" not in url:
            url += ("&" if "?" in url else "?") + "kae=d"
        self.current_view().setUrl(QUrl(url))

    # ---- resolving .glass files (project-aware) ---------------------------
    def _resolve_glass(self, name, scoped=False):
        if not name.lower().endswith(".glass"):
            name += ".glass"
        name = os.path.basename(name)
        cands = []
        if self.current_project_dir:
            cands.append(os.path.join(self.current_project_dir, name))
        if not scoped:
            cands.append(os.path.join(PROJECTS_DIR, name))
        for c in cands:
            if os.path.exists(c):
                return c
        return None

    def _set_ui_doc(self, doc, label, open_new=False):
        if open_new:
            self.new_tab("")                 # creates and switches to a fresh tab
        tab = self.current_tab()
        if tab is not None:
            tab.ui_doc = doc
            tab.ui_vars = dict(getattr(doc, "variables", {}) or {})
            tab.imports = glasspack.load_imports(
                self.current_project_dir, getattr(doc, "imports", []), logger=self.log)
        self.log(f"[glass] loaded {label}{' (new tab)' if open_new else ''}: "
                 f"{len(doc)} component(s), {len(getattr(doc, 'variables', {}))} variable(s)")
        v = self.current_view()
        if v:
            v.setUrl(QUrl(HOME))     # clean backdrop; the .glass UI is the focus
        self._render_ui()

    def _load_glass_path(self, path, label, open_new=False):
        try:
            doc = dsl.parse_file(path)
        except dsl.DSLError as e:
            self.log(f"[glass] parse error: {e}")
            return
        except Exception as e:
            self.log(f"[glass] couldn't load {label}: {e}")
            return
        self._set_ui_doc(doc, label, open_new=open_new)

    def load_glass(self, filename):
        """Load a single .glass file (current project first, then projects/)."""
        path = self._resolve_glass(filename)
        if not path:
            self.log(f"[glass] not found: {os.path.basename(filename)}")
            return
        self._load_glass_path(path, os.path.relpath(path, HERE))

    # kept for older callers (wiki, etc.) - resolves like load_glass
    def load_project(self, filename):
        self.load_glass(filename)

    def open_project(self, name):
        """Open a project folder: set it current and load its entry script."""
        name = os.path.basename(name.strip("/\\"))
        d = os.path.join(PROJECTS_DIR, name)
        if not project.is_project(d):
            self.log(f"[project] not found: projects/{name}/ (no {project.MANIFEST})")
            return
        man = project.refresh_manifest(d)
        self.current_project_dir = d
        renderer.ASSET_DIRS = [d, PROJECTS_DIR, UI_DIR, HERE]   # project assets first
        entry = man.get("entry", "home.glass")
        self.log(f"[project] opened {name} (entry: {entry})")
        self._load_glass_path(os.path.join(d, entry), f"{name}/{entry}")

    def get_glass(self, name, open_new=False):
        """getGlass action: load a sibling script from the CURRENT project only.
        With open_new=True the script opens in a new tab instead of replacing
        the current one."""
        if not name:
            return
        path = self._resolve_glass(name, scoped=bool(self.current_project_dir))
        if not path:
            where = (os.path.basename(self.current_project_dir)
                     if self.current_project_dir else "projects")
            self.log(f"[getGlass] '{name}' not found in {where}")
            return
        self._load_glass_path(path, os.path.relpath(path, HERE), open_new=open_new)

    def serve_current(self, filename):
        """Host the current project folder (or projects/) on the network."""
        directory = self.current_project_dir or PROJECTS_DIR
        if self.server is None or self.server.directory != directory:
            if self.server is not None:
                self.server.stop()
            self.server = glassnet.GlassServer(directory)
        host, port = self.server.start()
        self.log(f"[serv] hosting {os.path.basename(directory)} at  {host}:{port}")
        if filename:
            self.log(f"[serv] others open:  {host}:{port}/{os.path.basename(filename)}")
            self.load_glass(filename)

    def load_remote(self, host, port, filename):
        self.log(f"[glass] fetching {host}:{port}/{filename} ...")
        try:
            text = glassnet.fetch(host, port, filename)
            doc = dsl.parse(text)
        except dsl.DSLError as e:
            self.log(f"[glass] remote parse error: {e}")
            return
        except Exception as e:
            self.log(f"[glass] fetch failed: {e}")
            return
        self._set_ui_doc(doc, f"{host}:{port}/{filename}")

    def run_js(self, code):
        if code:
            self.current_view().page().runJavaScript(code)

    def clear_data(self):
        """Wipe cookies and the on-disk cache - a privacy panic button."""
        try:
            self.profile.cookieStore().deleteAllCookies()
            self.profile.clearHttpCache()
            self.log("[privacy] cleared all cookies and cache")
        except Exception as e:
            self.log(f"[privacy] clear failed: {e}")

    # ---- transparency tools ------------------------------------------------
    def view_source(self):
        v = self.current_view()

        def show(html):
            win = QMainWindow(self)
            win.setWindowTitle("Source: " + v.url().toString())
            win.resize(900, 700)
            te = QPlainTextEdit()
            te.setReadOnly(True)
            te.setPlainText(html)
            te.setStyleSheet("background:#0a0d10;color:#cfe3f5;font-family:Consolas,monospace;")
            win.setCentralWidget(te)
            win.show()
            self._source_win = win
        v.page().toHtml(show)

    def open_devtools(self):
        v = self.current_view()
        dev = QWebEngineView()
        v.page().setDevToolsPage(dev.page())
        win = QMainWindow(self)
        win.setWindowTitle("DevTools")
        win.resize(900, 600)
        win.setCentralWidget(dev)
        win.show()
        self._dev_win = win

    # ---- .glass UI ---------------------------------------------------------
    def load_ui(self):
        doc = dsl.Document()
        if os.path.isdir(UI_DIR):
            for name in sorted(os.listdir(UI_DIR)):
                if name.endswith(".glass"):
                    try:
                        d = dsl.parse_file(os.path.join(UI_DIR, name))
                        doc.extend(d)
                        doc.variables.update(getattr(d, "variables", {}))
                        doc.grabs.extend(getattr(d, "grabs", []))
                        if getattr(d, "center", None) and not doc.center:
                            doc.center = d.center
                    except dsl.DSLError as e:
                        self.log(f"[ui] parse error in {name}: {e}")
                    except Exception as e:
                        self.log(f"[ui] couldn't load {name}: {e}")
        self.ui_rules = doc
        self.variables = dict(getattr(doc, "variables", {}) or {})
        self.base_ui_rules = doc
        self.base_variables = dict(getattr(doc, "variables", {}) or {})
        tab = self.current_tab()
        if tab is not None:
            tab.ui_doc = None        # return this tab to the base ui/ UI
            tab.ui_vars = None
        self._render_ui()

    def reload_ui(self):
        self.log("[ui] reloading .glass files")
        self.closed_panels.clear()     # a reload brings closed panels back
        self.load_ui()

    def open_editor(self):
        import subprocess
        subprocess.Popen([sys.executable, os.path.join(HERE, "editor.py")])
        self.log("[ui] launched editor")

    def _is_home_url(self, url):
        u = (url or "").strip()
        return (u == "" or u == HOME or u.startswith("data:text/html")
                or u.startswith("about:") or u == "about:blank")

    def _active_ui(self):
        """The (doc, vars) for the current tab: per-tab if set, else base ui/."""
        tab = self.current_tab()
        if tab is not None and tab.ui_doc is not None:
            if tab.ui_vars is None:
                tab.ui_vars = dict(getattr(tab.ui_doc, "variables", {}) or {})
            return tab.ui_doc, tab.ui_vars
        return self.base_ui_rules, self.base_variables

    def _active_vars(self):
        tab = self.current_tab()
        if tab is not None and tab.ui_doc is not None:
            if tab.ui_vars is None:
                tab.ui_vars = dict(getattr(tab.ui_doc, "variables", {}) or {})
            return tab.ui_vars
        return self.base_variables

    def _ui_area(self):
        """Size of the area .glass panels render into (the current tab)."""
        tab = self.current_tab()
        if tab and tab.width() > 1 and tab.height() > 1:
            return tab.width(), tab.height()
        return max(1, self.width()), max(1, self.height())

    def _render_ui(self):
        tab = self.current_tab()
        if not tab:
            return
        renderer.set_screen(*self._ui_area())     # screen.width / screen.height
        tab.clear_panels()
        v = self.current_view()
        host = v.url().host() if v else None
        doc, varstore = self._active_ui()
        tab.panels, tab.holders = renderer.render_rules(
            doc, self.api, tab, host, registry={}, variables=varstore)
        if self.world is not None:                 # let scripts reach audio.*
            self.world.audio = self.audio
        # the full-screen .main is the home screen: only show it on the start
        # page, never covering an actual website.
        on_home = self._is_home_url(v.url().toString() if v else "")
        game_panel = None
        for p in tab.panels:
            if isinstance(p, renderer.FullScreenPanel):
                p.setVisible(on_home)
                if on_home:
                    game_panel = p
            p.raise_()
        # a live game needs keyboard focus, or the WebEngine view eats the keys
        if game_panel is not None and self.world is not None and \
                getattr(self.world, "dynamic", False):
            game_panel.setFocus(Qt.FocusReason.OtherFocusReason)

    def refresh_ui(self):
        """Re-render the current UI keeping live variable values."""
        self._render_ui()

    def _tick_ui_vars(self):
        """Keep {Var} text live for non-game UIs (e.g. package launchers)."""
        if self.world is None:
            try:
                renderer.refresh_var_bindings(self._active_vars())
            except Exception:
                pass

    def _make_pkg_ctx(self):
        return glasspack.PackageContext(
            self, self._active_vars, self.api, self.current_project_dir)

    def pkg_call(self, fn=""):
        """Run an imported package function from a button: call: alias.function"""
        tab = self.current_tab()
        mods = getattr(tab, "imports", {}) if tab is not None else {}
        alias, _, func = str(fn).partition(".")
        mod = mods.get(alias)
        if mod is None:
            self.log(f"[pkg] no import named {alias!r} (did you 'import {alias}'?)")
            return
        target = getattr(mod, func or "main", None)
        if not callable(target):
            self.log(f"[pkg] {fn!r} is not a function")
            return
        try:
            target(self._make_pkg_ctx())
        except Exception as e:
            self.log(f"[pkg] error in {fn}: {e}")
        try:
            renderer.refresh_var_bindings(self._active_vars())
        except Exception:
            pass

    def apply_set(self, stmt):
        """Apply a 'Var = / += / -= / *= /= value' assignment to live variables."""
        m = re.match(r"\s*([A-Za-z_]\w*)\s*(\+=|-=|\*=|/=|=)\s*(.+)$", stmt or "")
        if not m:
            return False
        store = self._active_vars()
        name, op, rhs = m.group(1), m.group(2), m.group(3).strip()
        low = rhs.lower()
        if op == "=" and low in ("true", "false"):
            store[name] = (low == "true")
            return True
        if len(rhs) >= 2 and rhs[0] in "\"'" and rhs[-1] == rhs[0]:
            text = rhs[1:-1]                    # string literal
            if op == "+=":
                store[name] = str(store.get(name, "")) + text
            else:
                store[name] = text
            return True
        if isinstance(store.get(name), str):     # current value is a string
            store[name] = (str(store.get(name, "")) if op == "+=" else "") + rhs
            return True

        def num(x):
            if isinstance(x, bool):
                return 1.0 if x else 0.0
            try:
                return float(x)
            except (ValueError, TypeError):
                return 0.0
        cur = num(store.get(name, 0))
        val = dsl.eval_number(rhs, store)
        if op == "=":
            new = val
        elif op == "+=":
            new = cur + val
        elif op == "-=":
            new = cur - val
        elif op == "*=":
            new = cur * val
        elif op == "/=":
            new = cur / val if val else cur
        else:
            return False
        if float(new).is_integer():
            new = int(new)
        store[name] = new
        return True

    def active_registry(self):
        t = self.current_tab()
        return t.holders if t else {}

    def _setup_ui_watcher(self):
        self.watcher = QFileSystemWatcher(self)
        if os.path.isdir(UI_DIR):
            self.watcher.addPath(UI_DIR)
            for name in os.listdir(UI_DIR):
                if name.endswith(".glass"):
                    self.watcher.addPath(os.path.join(UI_DIR, name))
        self.watcher.directoryChanged.connect(lambda _p: self.reload_ui())
        self.watcher.fileChanged.connect(lambda _p: self.reload_ui())

    # ---- signal handlers ---------------------------------------------------
    def _on_url_changed(self, qurl):
        if self.sender() is self.current_view():
            self.address.setText(qurl.toString())
        self._save_session()

    def _on_load_finished(self, view):
        # inject cosmetic ad-hiding into every page
        view.page().runJavaScript(self.interceptor.cosmetic_js())
        view.page().runJavaScript(self.interceptor.youtube_js())
        if getattr(self, "_web_sb_js", ""):
            view.page().runJavaScript(self._web_sb_js)
        self._inject_credentials(view)
        self._apply_saved_zoom(view)
        try:
            u = view.url().toString()
            if u.startswith(("http://", "https://")):
                history.add_history(u, view.title())
            host = view.url().host()
            if any(s in host for s in STREAMING_HOSTS):
                mode = prefs.load("drm_mode", "ask")   # ask | window | browser | off
                if mode == "window":
                    self._open_drm_window(view.url().toString())
                    self.notice.show_message(
                        f"Opened {host} in Glass's Media Player (isolated, "
                        f"private profile).", [("OK", self.notice.hide)])
                elif mode == "browser":
                    self._open_system_browser()
                    self.notice.show_message(
                        f"Opened {host} in your system browser.",
                        [("OK", self.notice.hide)])
                elif mode == "off":
                    pass
                else:
                    view.page().runJavaScript(_DRM_CHECK_JS)
        except Exception:
            pass
        if view is self.current_view():
            self._render_ui()

    # ---- saved logins ------------------------------------------------------
    _CAPTURE_JS = (
        "(function(){if(window.__glassCred)return;window.__glassCred=1;"
        "document.addEventListener('submit',function(e){try{"
        "var f=e.target;var pw=f.querySelector&&f.querySelector('input[type=password]');"
        "if(!pw||!pw.value)return;var u='';"
        "var ins=f.querySelectorAll('input[type=text],input[type=email],input:not([type])');"
        "for(var i=0;i<ins.length;i++){if(ins[i].value){u=ins[i].value;}}"
        "console.log('GLASS_CRED::'+JSON.stringify({u:u,p:pw.value}));"
        "}catch(err){}},true);})();")

    def _inject_credentials(self, view):
        try:
            view.page().runJavaScript(self._CAPTURE_JS)      # capture on submit
        except Exception:
            return
        host = ""
        try:
            host = view.url().host()
        except Exception:
            pass
        if not host or not vault.has_login(host):
            return
        # only offer to fill if the page actually has a password field
        def _check(has_pw, v=view, h=host):
            if has_pw:
                self.offer_fill_login(v, h)
        try:
            view.page().runJavaScript(
                "!!document.querySelector('input[type=password]')", _check)
        except Exception:
            pass

    def offer_save_login(self, host, username, password):
        if not host or not password:
            return
        existing = vault.get_login(host)
        if existing and existing.get("password") == password \
                and existing.get("username") == username:
            return                                   # already saved, unchanged
        dlg = SavePasswordDialog(self, host, username)
        choice = dlg.exec()
        if dlg.result_choice == "save":
            vault.save_login(host, username, password)
            self.log(f"[logins] saved login for {host}")
        elif dlg.result_choice == "never":
            vault.set_never(host)
            self.log(f"[logins] won't ask again for {host}")

    def offer_fill_login(self, view, host):
        creds = vault.get_login(host)
        if not creds:
            return
        dlg = FillPasswordDialog(self, host, creds["username"])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            import json as _j
            u = _j.dumps(creds["username"]); p = _j.dumps(creds["password"])
            fill = (
                "(function(u,p){var pw=document.querySelector('input[type=password]');"
                "if(pw){pw.value=p;pw.dispatchEvent(new Event('input',{bubbles:true}));}"
                "var us=document.querySelectorAll('input[type=text],input[type=email],input:not([type])');"
                "if(us.length){us[0].value=u;us[0].dispatchEvent(new Event('input',{bubbles:true}));}"
                f"}})({u},{p});")
            try:
                view.page().runJavaScript(fill)
                self.log(f"[logins] filled login for {host}")
            except Exception:
                pass

    def _on_title_changed(self, title):
        idx = self.tabs.currentIndex()
        if idx >= 0 and self.sender() is self.current_view():
            text = (title or "Tab")[:24]
            self.tabs.setTabText(idx, text)
            tab = self.current_tab()
            if tab is not None:
                tab.title = text

    def _on_tab_changed(self, _idx):
        tab = self.current_tab()
        if tab is not None:
            if tab.suspended:
                self._resume_tab(tab)       # reloads the page
            else:
                self._touch_tab(tab)
            self._enforce_tab_budget()
        v = self.current_view()
        if v:
            self.address.setText(v.url().toString())
        self._render_ui()

    # ---- shortcuts ---------------------------------------------------------
    def _build_shortcuts(self):
        def sc(seq, fn):
            QShortcut(QKeySequence(seq), self, activated=fn)
        sc("Ctrl+L", self.toggle_network_log)
        sc("Ctrl+U", self.view_source)
        sc("Ctrl+R", lambda: self.current_view().reload())
        sc("Ctrl+T", lambda: self.new_tab(self.home_url))
        sc("Ctrl+W", lambda: self.close_tab(self.tabs.currentIndex()))
        sc("F12", self.open_devtools)
        sc("Ctrl+Shift+U", self.reload_ui)
        sc("Ctrl+E", self.open_editor)
        sc("Ctrl+Shift+Delete", self.clear_data)


READY_FLAG = os.path.join(HERE, ".glass_ready")


def _signal_ready():
    """Tell the launcher the window is up so it can close its console."""
    try:
        with open(READY_FLAG, "w", encoding="utf-8") as f:
            f.write("ok")
    except OSError:
        pass


def _set_app_id(app_id):
    """On Windows, give the process its own taskbar identity so it shows our
    window icon instead of the generic Python icon."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass


def main():
    _set_app_id("Anthropic.Glass.Browser")
    app = QApplication(sys.argv)
    app.setApplicationName("Glass")
    app.setWindowIcon(images.load_icon("browser"))
    import theme
    theme.apply_theme(app)
    win = GlassWindow()
    win.setWindowTitle("Glass")
    win.setWindowIcon(images.load_icon("browser"))
    win.show()
    QTimer.singleShot(500, win._offer_restore)
    # once the window has shown and the event loop is running, signal the launcher
    QTimer.singleShot(400, _signal_ready)

    def _cleanup():
        try:
            os.remove(READY_FLAG)
        except OSError:
            pass
    app.aboutToQuit.connect(_cleanup)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
