"""Glass Snapshot - packages a screenshot of your screen + your .glass code +
version info into ONE folder/zip that you can upload to Claude, so it can see
exactly what's on your screen and help.

PRIVACY: this only SAVES a bundle on your PC. It sends nothing anywhere. YOU
choose whether to upload it. Review the images first - a full-screen shot may
show whatever else is open, so close anything private before running it.

Run:  glass_snapshot.bat        (or)  python core/glass_snapshot.py
"""

import os
import sys
import glob
import json
import shutil
import zipfile
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))     # .../glass/core
ROOT = os.path.dirname(HERE)                           # .../glass
OUT = os.path.join(ROOT, "glass_snapshot")
ZIP = os.path.join(ROOT, "glass_snapshot.zip")


def _versions():
    lines = []
    lines.append("Glass snapshot  " + datetime.datetime.now().isoformat(timespec="seconds"))
    lines.append("platform: " + sys.platform + "  python: " + sys.version.split()[0])
    try:
        from PyQt6.QtCore import QT_VERSION_STR
        lines.append("Qt: " + QT_VERSION_STR)
    except Exception as e:
        lines.append("Qt: (n/a) " + str(e))
    try:
        from PyQt6.QtWebEngineCore import qWebEngineVersion, qWebEngineChromiumVersion
        lines.append("WebEngine: " + qWebEngineVersion() + "  Chromium: " + qWebEngineChromiumVersion())
    except Exception:
        lines.append("WebEngine: (not installed)")
    for mod in ("miniaudio", "webview"):
        try:
            __import__(mod)
            lines.append(mod + ": installed")
        except Exception:
            lines.append(mod + ": not installed")
    return "\n".join(lines)


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QGuiApplication
    app = QApplication.instance() or QApplication([])

    # 1) screenshot of every monitor
    shots = 0
    try:
        for i, screen in enumerate(QGuiApplication.screens()):
            pm = screen.grabWindow(0)
            if not pm.isNull():
                pm.save(os.path.join(OUT, f"screen_{i + 1}.png"), "PNG")
                shots += 1
    except Exception as e:
        with open(os.path.join(OUT, "screenshot_error.txt"), "w") as f:
            f.write(str(e))

    # 2) your .glass sources
    srcdir = os.path.join(OUT, "glass_sources")
    os.makedirs(srcdir, exist_ok=True)
    for f in glob.glob(os.path.join(ROOT, "**", "*.glass"), recursive=True):
        try:
            rel = os.path.relpath(f, ROOT).replace(os.sep, "__")
            shutil.copy(f, os.path.join(srcdir, rel))
        except Exception:
            pass

    # 3) settings + versions (no logins/cookies - just prefs)
    try:
        prefs_path = os.path.join(HERE, "prefs.json")
        if os.path.isfile(prefs_path):
            data = json.load(open(prefs_path))
            data.pop("widevine_path", None)      # harmless, but trim machine paths
            with open(os.path.join(OUT, "settings.json"), "w") as f:
                json.dump(data, f, indent=2)
    except Exception:
        pass
    with open(os.path.join(OUT, "report.txt"), "w", encoding="utf-8") as f:
        f.write(_versions())

    # 4) zip it up for easy upload
    try:
        with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
            for dp, _dn, fns in os.walk(OUT):
                for fn in fns:
                    full = os.path.join(dp, fn)
                    z.write(full, os.path.relpath(full, ROOT))
    except Exception as e:
        print("[snapshot] couldn't zip:", e)

    print("=" * 58)
    print(" GLASS SNAPSHOT READY")
    print("=" * 58)
    print(f" Captured {shots} screen(s) + your .glass code + versions.")
    print(f" Folder:  {OUT}")
    print(f" Zip:     {ZIP}")
    print()
    print(" Review the screenshots first (close anything private), then")
    print(" upload glass_snapshot.zip to Claude in the chat.")
    print(" Nothing was sent anywhere - this only saved files on your PC.")
    print("=" * 58)


if __name__ == "__main__":
    main()
