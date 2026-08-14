"""Fetches the Widevine CDM from Google's public CDN (the same source Chrome
uses) into core/widevine/ so DRM video (Crunchyroll, Netflix, Spotify...) can
decrypt. Qt WebEngine does not ship Widevine, so without this it just spins.

- Nothing is bundled or redistributed: the file is downloaded from Google to
  THIS machine on first run, exactly like a browser would.
- Skips instantly if a CDM is already present (local, or Chrome/Edge).
- Fully non-fatal: if there's no internet or the build can't use it, Glass still
  launches and falls back to the 'Open in system browser' option.
"""

import os
import sys
import ssl
import json
import shutil
import hashlib
import zipfile
import tempfile
import platform
import datetime
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "widevine")            # Glass points --widevine-path here
VERSIONS_URL = "https://dl.google.com/widevine-cdm/versions.txt"
LOG_PATH = os.path.join(HERE, "widevine_fetch_log.json")   # human-readable audit trail


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _log(record):
    """Append one transparency record - what CDM Glass is using this run, where
    it came from, and its exact hash - so nothing about this ever happens
    invisibly. Readable any time as plain JSON, independent of the console
    output that disappears once the launcher window closes."""
    record["timestamp"] = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        log = json.load(open(LOG_PATH, "r", encoding="utf-8")) if os.path.isfile(LOG_PATH) else []
    except Exception:
        log = []
    log.append(record)
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2)
    except Exception:
        pass
    print(f"[widevine] {record.get('source','?')}: {record.get('detail','')}")
    if record.get("library_sha256"):
        print(f"[widevine]   library file : {record.get('library_path')}")
        print(f"[widevine]   sha256        : {record['library_sha256']}")
    print(f"[widevine]   full log at   : {LOG_PATH}")
    return record


def _platform():
    """Return (download-tag, platform-subdir, library-filename)."""
    m = platform.machine().lower()
    if sys.platform.startswith("win"):
        if "arm" in m:
            return "win-arm64", "win_arm64", "widevinecdm.dll"
        if m in ("amd64", "x86_64", "x64"):
            return "win-x64", "win_x64", "widevinecdm.dll"
        return "win-ia32", "win_x86", "widevinecdm.dll"
    if sys.platform == "darwin":
        if "arm" in m:
            return "mac-arm64", "mac_arm64", "libwidevinecdm.dylib"
        return "mac-x64", "mac_x64", "libwidevinecdm.dylib"
    return "linux-x64", "linux_x64", "libwidevinecdm.so"


def already_installed():
    """True if we've already placed a CDM under core/widevine/."""
    _, platdir, lib = _platform()
    return (os.path.isfile(os.path.join(DEST, "manifest.json")) and
            os.path.isfile(os.path.join(DEST, "_platform_specific", platdir, lib)))


def _browser_cdm_exists():
    """Path to an installed Chrome/Edge CDM Glass can borrow, or None."""
    env = os.environ
    if sys.platform.startswith("win"):
        roots = [os.path.join(env.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data", "WidevineCdm"),
                 os.path.join(env.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data", "WidevineCdm")]
        needle = "widevinecdm.dll"
    elif sys.platform == "darwin":
        roots = [os.path.expanduser("~/Library/Application Support/Google/Chrome/WidevineCdm")]
        needle = "libwidevinecdm.dylib"
    else:
        roots = [os.path.expanduser("~/.config/google-chrome/WidevineCdm"), "/opt/google/chrome"]
        needle = "libwidevinecdm.so"
    for root in roots:
        if root and os.path.isdir(root):
            for dp, _dn, fns in os.walk(root):
                if needle in fns:
                    return os.path.join(dp, needle)
    return None


def _latest_version(ctx):
    with urllib.request.urlopen(VERSIONS_URL, timeout=20, context=ctx) as r:
        vers = [ln.strip() for ln in r.read().decode("utf-8", "replace").splitlines()
                if ln.strip() and ln[0].isdigit()]

    def key(v):
        try:
            return tuple(int(x) for x in v.split("."))
        except Exception:
            return (0,)
    vers.sort(key=key)
    if not vers:
        raise RuntimeError("no versions listed")
    return vers[-1]


def install(force=False, prefer_browser=True):
    """Ensure a Widevine CDM is available. Returns True if one is present after.
    Every path through this function ends by logging exactly which file is in
    use, where it came from, and its sha256 - see LOG_PATH."""
    if already_installed() and not force:
        _, platdir, lib = _platform()
        lib_path = os.path.join(DEST, "_platform_specific", platdir, lib)
        _log({"source": "already_installed", "detail": "existing local CDM, no network request made",
              "library_path": lib_path, "library_sha256": _sha256_file(lib_path)})
        return True
    if prefer_browser and not force:
        browser_lib = _browser_cdm_exists()
        if browser_lib:
            _log({"source": "browser_cdm", "detail": "borrowed from an installed Chrome/Edge, no download",
                  "library_path": browser_lib, "library_sha256": _sha256_file(browser_lib)})
            return True

    tag, platdir, lib = _platform()
    try:
        ctx = ssl.create_default_context()
    except Exception:
        ctx = None
    try:
        ver = _latest_version(ctx)
    except Exception as e:
        _log({"source": "unavailable", "detail": f"couldn't reach dl.google.com/widevine-cdm/versions.txt ({e})"})
        return already_installed()

    url = f"https://dl.google.com/widevine-cdm/{ver}-{tag}.zip"
    tmp = os.path.join(tempfile.gettempdir(), f"glass_wv_{ver}_{tag}.zip")
    print(f"[widevine] downloading Widevine {ver} for {tag} from {url} ...")
    try:
        with urllib.request.urlopen(url, timeout=90, context=ctx) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:
        _log({"source": "download_failed", "detail": f"GET {url} failed ({e})"})
        return already_installed()

    zip_size = os.path.getsize(tmp)
    zip_sha256 = _sha256_file(tmp)
    print(f"[widevine] fetched {zip_size} bytes, sha256={zip_sha256}")

    try:
        libdir = os.path.join(DEST, "_platform_specific", platdir)
        os.makedirs(libdir, exist_ok=True)
        with zipfile.ZipFile(tmp) as z:
            for name in z.namelist():
                base = os.path.basename(name)
                if not base:
                    continue
                data = z.read(name)
                if base in (lib, "widevinecdm.sig", "libwidevinecdm.dylib", "libwidevinecdm.so"):
                    out = os.path.join(libdir, base)
                else:                                   # manifest.json, LICENSE.txt
                    out = os.path.join(DEST, base)
                with open(out, "wb") as f:
                    f.write(data)
    except Exception as e:
        _log({"source": "extract_failed", "detail": f"couldn't unpack {url} ({e})",
              "zip_sha256": zip_sha256, "zip_size_bytes": zip_size})
        return already_installed()
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass

    if already_installed():
        lib_path = os.path.join(DEST, "_platform_specific", platdir, lib)
        _log({"source": "downloaded", "detail": f"Widevine {ver} for {tag}",
              "download_url": url, "versions_url": VERSIONS_URL,
              "zip_sha256": zip_sha256, "zip_size_bytes": zip_size,
              "library_path": lib_path, "library_sha256": _sha256_file(lib_path)})
        return True
    _log({"source": "install_incomplete", "detail": "zip layout unexpected after extract",
          "download_url": url, "zip_sha256": zip_sha256})
    return False


if __name__ == "__main__":
    force = "--force" in sys.argv
    ok = install(force=force)
    sys.exit(0 if ok else 0)     # never fail the launcher
