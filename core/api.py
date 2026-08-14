"""
Glass API  (v2)
===============
"Its own API system" - the surface your UI talks to.

Every `action:` name in a .glass file maps to a method on BrowserAPI.
The editor's tutorial reads the docstrings below to explain each action,
so keep them short and accurate when you add your own.

ADD YOUR OWN: define a method here, give it a one-line docstring, then call
it from any menu with   button "X" { action: yourmethod }.
"""

from __future__ import annotations
import inspect
import os
import re
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


class BrowserAPI:
    def __init__(self, window):
        self.window = window

    # ---- dispatch ----------------------------------------------------------
    def call(self, action_name, **kwargs):
        """Invoke an action by name, passing only the kwargs it accepts."""
        fn = getattr(self, action_name, None)
        if fn is None or not callable(fn) or action_name.startswith("_"):
            self.window.log(f"[api] unknown action: {action_name!r}")
            return None
        try:
            sig = inspect.signature(fn)
            accepts = set(sig.parameters)
            if not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                kwargs = {k: v for k, v in kwargs.items() if k in accepts}
            return fn(**kwargs)
        except Exception as e:
            self.window.log(f"[api] error in {action_name}: {e}")
            return None

    @classmethod
    def actions(cls):
        """Return {name: docstring} for every callable action (for the tutorial)."""
        out = {}
        for name, fn in inspect.getmembers(cls, predicate=inspect.isfunction):
            if name.startswith("_") or name in ("call", "actions"):
                continue
            out[name] = (fn.__doc__ or "").strip().split("\n")[0]
        return out

    # ---- holder registry ---------------------------------------------------
    def _registry(self):
        return self.window.active_registry()

    def _targets(self, target):
        return [t for t in re.split(r"[ ,]+", str(target).strip()) if t]

    def toggle(self, target=""):
        """Show/hide one or more named holders (space-separate for several)."""
        for t in self._targets(target):
            w = self._registry().get(t)
            if w is not None:
                w.setVisible(not w.isVisible())
            else:
                self.window.log(f"[api] toggle: no holder named {t!r}")

    def show(self, target=""):
        """Show one or more named holders."""
        for t in self._targets(target):
            w = self._registry().get(t)
            if w is not None:
                w.setVisible(True)

    def hide(self, target=""):
        """Hide one or more named holders."""
        for t in self._targets(target):
            w = self._registry().get(t)
            if w is not None:
                w.setVisible(False)

    # ---- variables (set: / do:) -------------------------------------------
    def run_set(self, stmt=""):
        """Apply a variable assignment like 'Count += 1', then refresh the UI."""
        if self.window.apply_set(stmt):
            self.window.refresh_ui()

    def pkg_call(self, fn=""):
        """Call an imported package function (button uses call: alias.function)."""
        self.window.pkg_call(fn)

    def run_do(self, seq=""):
        """Run a ';'-separated list of commands: show/hide/toggle/set <args>."""
        changed = False
        for cmd in str(seq).split(";"):
            parts = cmd.split()
            if not parts:
                continue
            verb = parts[0].lower()
            args = parts[1:]
            if verb in ("show", "hide", "toggle"):
                for t in args:
                    w = self._registry().get(t)
                    if w is None:
                        continue
                    if verb == "show":
                        w.setVisible(True)
                    elif verb == "hide":
                        w.setVisible(False)
                    else:
                        w.setVisible(not w.isVisible())
            elif verb == "set":
                if self.window.apply_set(" ".join(args)):
                    changed = True
            else:
                self.call(verb)
        if changed:
            self.window.refresh_ui()
        return changed

    def wiki(self):
        """Open the built-in Glass wiki."""
        self.window.load_project("wiki.glass")

    def alert(self, message=""):
        """Show a popup message box."""
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.warning(self.window, "Glass", str(message))

    def getGlass(self, target="", openNew="false"):
        """Load another .glass script from the current project.
        openNew: true opens it in a new tab; false (default) replaces the view."""
        flag = str(openNew).strip().lower() in ("true", "1", "yes", "new")
        self.window.get_glass(target, open_new=flag)

    def cleardata(self):
        """Delete all cookies and clear the cache (privacy panic button)."""
        self.window.clear_data()

    # ---- navigation --------------------------------------------------------
    def navigate(self, url=""):
        """Go to a URL or search term."""
        self.window.navigate(url)

    def reload(self):
        """Reload the current page."""
        self.window.current_view().reload()

    def back(self):
        """Go back in history."""
        self.window.current_view().back()

    def forward(self):
        """Go forward in history."""
        self.window.current_view().forward()

    def stop(self):
        """Stop loading the current page."""
        self.window.current_view().stop()

    def home(self):
        """Go to the home page."""
        self.window.navigate(self.window.home_url)

    def newtab(self, url=""):
        """Open a new tab."""
        self.window.new_tab(url or self.window.home_url)

    def closetab(self):
        """Close the current tab."""
        self.window.close_tab(self.window.tabs.currentIndex())

    # ---- zoom --------------------------------------------------------------
    def zoomin(self):
        """Zoom the page in."""
        v = self.window.current_view(); v.setZoomFactor(v.zoomFactor() + 0.1)

    def zoomout(self):
        """Zoom the page out."""
        v = self.window.current_view(); v.setZoomFactor(max(0.25, v.zoomFactor() - 0.1))

    def zoomreset(self):
        """Reset zoom to 100%."""
        self.window.current_view().setZoomFactor(1.0)

    # ---- transparency: nothing is hidden -----------------------------------
    def viewsource(self):
        """Open the raw HTML source of the current page."""
        self.window.view_source()

    def showlog(self):
        """Toggle the live network/event log."""
        self.window.toggle_network_log()

    def devtools(self):
        """Open Chromium DevTools for the current page."""
        self.window.open_devtools()

    # ---- blocking ----------------------------------------------------------
    def toggleblock(self):
        """Turn ad/tracker blocking on or off."""
        on = self.window.interceptor.toggle()
        self.window.log(f"[block] blocking {'ON' if on else 'OFF'}")
        self.window.refresh_block_label()

    def stripads(self):
        """Remove obvious ad containers from the live page now."""
        self.window.run_js(self.window.interceptor.cosmetic_js())

    # ---- page scripting ----------------------------------------------------
    def js(self, code=""):
        """Run JavaScript in the current page."""
        self.window.run_js(code)

    # ---- app ---------------------------------------------------------------
    def reloadui(self):
        """Reload all .glass UI files from the ui/ folder."""
        self.window.reload_ui()

    def openeditor(self):
        """Open the Glass UI editor (code + live preview + tutorial)."""
        subprocess.Popen([sys.executable, os.path.join(HERE, "editor.py")])

    def quit(self):
        """Close the browser."""
        self.window.close()
