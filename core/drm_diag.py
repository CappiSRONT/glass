"""Glass DRM diagnostic.

Run:  python core\\drm_diag.py     (Windows)
      python3 core/drm_diag.py    (mac/Linux)

It prints your Qt/Chromium versions, whether a Widevine CDM was found and where,
and then runs a REAL Widevine probe (navigator.requestMediaKeySystemAccess +
createMediaKeys on a secure https page). The verdict tells us exactly what's
wrong so we stop guessing.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

print("=" * 60)
print(" GLASS  DRM / WIDEVINE  DIAGNOSTIC")
print("=" * 60)

# --- versions -------------------------------------------------------------
try:
    from PyQt6.QtCore import QT_VERSION_STR
    import PyQt6
    print(f"PyQt6:        installed")
    print(f"Qt version:   {QT_VERSION_STR}")
except Exception as e:
    print("PyQt6 NOT available:", e)
    sys.exit(1)

try:
    from PyQt6.QtWebEngineCore import qWebEngineVersion, qWebEngineChromiumVersion
    print(f"WebEngine:    {qWebEngineVersion()}")
    print(f"Chromium:     {qWebEngineChromiumVersion()}")
except Exception as e:
    print("WebEngine version: (couldn't read)", e)

# --- CDM discovery (importing browser also sets the widevine-path flag) ----
cdm = ""
try:
    import browser                       # sets QTWEBENGINE_CHROMIUM_FLAGS at import
    cdm = getattr(browser, "_wv", "") or ""
except Exception as e:
    print("Note: couldn't import browser module:", e)

print("-" * 60)
print("Widevine CDM path used:", cdm if cdm else "(NONE FOUND)")
if cdm and os.path.isdir(cdm):
    print("  contains manifest.json:",
          os.path.isfile(os.path.join(cdm, "manifest.json")))
    if "Edge" in cdm:
        print("  NOTE: this is Edge's CDM. If Edge is a newer channel than Qt's")
        print("        Chromium, it may not load. Prefer 'Download Widevine now'.")
flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
print("widevine-path in flags:", "yes" if "widevine-path" in flags else "NO")
print("-" * 60)

# --- live EME/Widevine probe ----------------------------------------------
print("Running a live Widevine probe (needs internet; a small window opens)...",
      flush=True)

# Harden against the GPU/sandbox process crashing a standalone script, which
# otherwise kills Python before we can print a verdict. Keep the widevine-path.
_base = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    _base + " --disable-gpu --disable-gpu-sandbox --disable-software-rasterizer "
    "--in-process-gpu --no-sandbox")

try:
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEnginePage
    from PyQt6.QtCore import QUrl, QTimer
except Exception as e:
    print("Can't run probe (missing WebEngine widgets):", e, flush=True)
    sys.exit(0)

PROBE_JS = (
    "(function(){"
    "function t(ks,codec){return navigator.requestMediaKeySystemAccess(ks,"
    "[{initDataTypes:['cenc','webm','keyids'],videoCapabilities:[{contentType:codec}]}])"
    ".then(function(a){return a.createMediaKeys();}).then(function(){return 'ok';})"
    ".catch(function(e){return 'fail:'+e.name;});}"
    "Promise.all(["
    "t('org.w3.clearkey','video/mp4;codecs=\"avc1.42E01E\"'),"
    "t('org.w3.clearkey','video/webm;codecs=\"vp9\"'),"
    "t('com.widevine.alpha','video/mp4;codecs=\"avc1.42E01E\"'),"
    "t('com.widevine.alpha','video/webm;codecs=\"vp9\"')"
    "]).then(function(r){console.log('GLASS_EME2::'+JSON.stringify(r));})"
    ".catch(function(e){console.log('GLASS_EME2::[\"err\"]');});"
    "})();")


def _verdict(payload):
    print("-" * 60, flush=True)
    if not payload:
        print("PROBE RESULT:  no verdict (timeout / no internet / crash).", flush=True)
        print("\nVERDICT: inconclusive.", flush=True)
        print("=" * 60, flush=True)
        return
    import json
    try:
        ck_h264, ck_vp9, wv_h264, wv_vp9 = json.loads(payload)
    except Exception:
        print("PROBE RESULT:", payload, flush=True)
        print("=" * 60, flush=True)
        return
    print(f"ClearKey (H.264): {ck_h264}", flush=True)
    print(f"ClearKey (VP9):   {ck_vp9}", flush=True)
    print(f"Widevine (H.264): {wv_h264}", flush=True)
    print(f"Widevine (VP9):   {wv_vp9}", flush=True)
    print("-" * 60, flush=True)
    eme_alive = ck_h264 == "ok" or ck_vp9 == "ok"
    wv_ok = wv_h264 == "ok" or wv_vp9 == "ok"
    if wv_ok:
        print("VERDICT: Widevine WORKS. If Crunchyroll still spins it wants HARDWARE"
              " DRM (L1); Qt only does software (L3). Not fixable in-app.", flush=True)
    elif not eme_alive:
        print("VERDICT: EME is disabled in this build entirely (even ClearKey failed)."
              " Use 'Open in system browser'.", flush=True)
    elif ck_h264 == "ok" and wv_h264 != "ok":
        print("VERDICT: EME + H.264 work, but the WIDEVINE CDM isn't being loaded by"
              " this Qt build. Likely the --widevine-path form, or Qt 6.11 not"
              " accepting Edge's CDM. This is the case we can still try to fix.", flush=True)
    else:
        print("VERDICT: Widevine not loading; ClearKey partial. Likely CDM not loaded"
              " and/or proprietary codecs. Try Chrome's CDM or system browser.", flush=True)
    print("=" * 60, flush=True)


state = {"done": False}
app = QApplication(sys.argv)


class ProbePage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, msg, line, src):
        if isinstance(msg, str) and msg.startswith("GLASS_EME2::") and not state["done"]:
            state["done"] = True
            _verdict(msg[len("GLASS_EME2::"):])   # print IMMEDIATELY
            QTimer.singleShot(50, app.quit)


view = QWebEngineView()
page = ProbePage()
view.setPage(page)
page.loadFinished.connect(lambda ok: page.runJavaScript(PROBE_JS))
page.load(QUrl("https://www.google.com"))     # a secure (https) origin for EME
view.resize(360, 240)
view.show()
# also run the probe unconditionally after 4s in case loadFinished never fires
def _timeout():
    if not state["done"]:
        state["done"] = True
        _verdict(None)
    app.quit()


QTimer.singleShot(4000, lambda: page.runJavaScript(PROBE_JS))
QTimer.singleShot(20000, _timeout)
try:
    app.exec()
except Exception as e:
    print("probe crashed:", e, flush=True)
if not state["done"]:
    _verdict(None)
