"""Glass "Media Player" - a small window for streaming/DRM sites (Crunchyroll,
Netflix, Spotify...) that Glass's own engine can't play.

It uses the Windows WebView2 (Edge Chromium) runtime, which ships with the H.264
codec and Widevine DRM built in. Runs as its OWN process (WebView2 needs its own
loop), launched by Glass, opening as a small resizable window in the bottom-right.

Look & feel is carried over from the user's Glass settings: window background =
their chrome colour, and the page scrollbars use their Glass scrollbar theme.

PRIVACY: uses a SEPARATE, ISOLATED profile inside Glass (core/.drmdata). It never
reads or copies the user's real Edge/Chrome passwords, cookies or history. Because
the profile persists, the user logs into a site ONCE and stays logged in.
Telemetry/sync/crash-reporting are off. 'Clear DRM data' in Settings wipes it.

Usage:  python drmwindow.py <url> [glass_x glass_y glass_w glass_h]
"""

import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, ".drmdata")
ICON = os.path.join(HERE, "assets", "media_icon.ico")


def _harden_env():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.environ["WEBVIEW2_USER_DATA_FOLDER"] = DATA_DIR
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        "--disable-sync --disable-breakpad --disable-crash-reporter "
        "--disable-domain-reliability --no-pings --no-default-browser-check "
        "--disable-features=msEdgeTelemetry,EdgeCollections,EdgeShoppingAssistant,"
        "EdgeDiscoverPage,EdgeSidebar,msWebOOUI,msPdfOOUI,InterestCohort,Translate,"
        "AutofillServerCommunication ")


def _user_theme():
    """Carry over the user's Glass look: chrome colour + scrollbar CSS."""
    bg = "#0c0f12"
    scrollbar_css = ""
    try:
        import prefs
        bg = prefs.load("chrome_bg", bg) or bg
    except Exception:
        pass
    try:
        import theme
        scrollbar_css = theme.web_scrollbar_css() or ""
    except Exception:
        pass
    return bg, scrollbar_css


def _colorref(hexstr, default=0x000000):
    try:
        h = hexstr.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (b << 16) | (g << 8) | r        # Windows COLORREF = 0x00BBGGRR
    except Exception:
        return default


def _style_native(window, bg, border="#2a3340", text="#d7e0ea"):
    """Make the OS window match Glass (dark title bar + Glass colours) and set the
    custom Media Player icon. Best-effort (Win10/11)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import byref, c_int, sizeof
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        # get the real top-level window handle from the native WinForms form
        hwnd = 0
        try:
            hwnd = int(window.native.Handle)
        except Exception:
            hwnd = user32.FindWindowW(None, "Media Player - Glass")
        if not hwnd:
            return False
        # dark title bar + exact Glass colours (Win11 for the colour attrs)
        dwm.DwmSetWindowAttribute(hwnd, 20, byref(c_int(1)), sizeof(c_int))
        for attr, col in ((35, _colorref(bg)), (34, _colorref(border)),
                          (36, _colorref(text))):
            try:
                dwm.DwmSetWindowAttribute(hwnd, attr, byref(c_int(col)), sizeof(c_int))
            except Exception:
                pass
        if not os.path.isfile(ICON):
            return True
        # 1) most reliable: set the WinForms Form.Icon (updates title bar + taskbar)
        set_via_forms = False
        try:
            import clr
            clr.AddReference("System.Drawing")
            from System.Drawing import Icon as _Icon
            window.native.Icon = _Icon(ICON)
            set_via_forms = True
        except Exception:
            pass
        # 2) fallback: WM_SETICON + class icon via ctypes
        try:
            IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
            hicon = user32.LoadImageW(0, ICON, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
            if hicon:
                user32.SendMessageW(hwnd, WM_SETICON, 1, hicon)   # big
                user32.SendMessageW(hwnd, WM_SETICON, 0, hicon)   # small
                try:                                              # class icon (taskbar)
                    setcls = getattr(user32, "SetClassLongPtrW", None) or user32.SetClassLongW
                    setcls(hwnd, -14, hicon)   # GCLP_HICON
                    setcls(hwnd, -34, hicon)   # GCLP_HICONSM
                except Exception:
                    pass
        except Exception:
            pass
        return set_via_forms or True
    except Exception:
        return False


def main():
    args = sys.argv[1:]
    url = args[0] if args else "https://www.crunchyroll.com"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    W, H = 680, 430
    x = y = None
    if len(args) >= 5:
        try:
            gx, gy, gw, gh = (int(float(v)) for v in args[1:5])
            x = max(0, gx + gw - W - 24)          # bottom-right of the Glass window
            y = max(0, gy + gh - H - 24)
        except Exception:
            x = y = None

    _harden_env()
    bg, scrollbar_css = _user_theme()

    try:
        import webview
    except Exception:
        import webbrowser
        webbrowser.open(url)
        print("[drm] pywebview not installed; opened in system browser instead.")
        print("[drm] enable the in-Glass player with:  pip install pywebview")
        return

    class Api:
        def __init__(self):
            self._full = False

        def fullscreen(self):
            try:
                webview.windows[0].toggle_fullscreen()
                self._full = not self._full
            except Exception:
                pass

        def exit_full(self):
            if self._full:
                self.fullscreen()

        def close(self):
            for w in list(getattr(webview, "windows", [])):
                try:
                    w.destroy()
                except Exception:
                    pass

    api = Api()
    kwargs = dict(url=url, width=W, height=H, resizable=True, background_color=bg,
                  js_api=api, confirm_close=False, text_select=True,
                  min_size=(360, 240))
    if x is not None:
        kwargs.update(x=x, y=y)

    window = webview.create_window("Media Player - Glass", **kwargs)

    inject = (
        "(function(){"
        "var s=document.createElement('style');s.textContent=" + json.dumps(scrollbar_css) + ";"
        "document.documentElement.appendChild(s);"
        "document.addEventListener('keydown',function(e){"
        "  if(e.key==='F11'){e.preventDefault();try{window.pywebview.api.fullscreen();}catch(_){ }}"
        "  else if(e.key==='Escape'){try{window.pywebview.api.exit_full();}catch(_){ }}"
        "});"
        "})();")

    def _on_load():
        try:
            window.evaluate_js(inject)
        except Exception:
            pass
        _style_native(window, bg)            # theme title bar + set icon

    try:
        window.events.loaded += _on_load
    except Exception:
        pass

    def _after_start():
        import time
        for _ in range(24):                  # retry until the window handle settles
            try:
                if _style_native(window, bg):
                    break
            except Exception:
                pass
            time.sleep(0.25)

    try:
        webview.start(_after_start, gui="edgechromium",
                      storage_path=DATA_DIR, private_mode=False)
    except Exception as e:
        print("[drm] WebView2 window failed:", e)
        try:
            webview.start(storage_path=DATA_DIR, private_mode=False)
        except Exception:
            import webbrowser
            webbrowser.open(url)


if __name__ == "__main__":
    main()
