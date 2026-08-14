"""
Glass auto-updater
===================
Checks GitHub for a newer TAGGED RELEASE, and if the user agrees, downloads
and applies it. Called early from run.bat/run.sh - before dependency checks,
so an update that changes requirements.txt is picked up by the pip install
step that runs right after this.

Never blocks launching Glass: any failure here (offline, GitHub rate-limited,
a malformed release, whatever) is caught and just falls through to launching
whatever version is already installed. An update check is a nice-to-have,
not a dependency.

The one thing this is careful about above everything else: an update must
NEVER touch the user's own data. See PRESERVE below - it's deliberately
conservative (e.g. ALL of core/projects/ is preserved, not just the parts
that look like "your" content vs "demo" content), because the cost of
silently deleting somebody's in-progress game is enormous and the cost of
an update not refreshing a demo project is nothing.
"""

from __future__ import annotations
import os
import sys
import json
import shutil
import zipfile
import tempfile
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))   # .../Glass/core
ROOT = os.path.dirname(HERE)                          # .../Glass
VERSION_PATH = os.path.join(ROOT, "VERSION")
SKIP_MARKER = os.path.join(HERE, ".glass_update_skip")   # remembers "user said no to THIS tag"

REPO = "CappiSRONT/glass"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
USER_AGENT = "glass-updater"
TIMEOUT_CHECK = 8       # seconds - the version check must never make launching feel stuck
TIMEOUT_DOWNLOAD = 120

# Paths (relative to ROOT) an update must never overwrite or delete, no matter
# what the downloaded release archive happens to contain at that same path.
# Deliberately conservative - see module docstring.
PRESERVE = [
    "core/projects",             # your games/UI - Game/home.glass and everything else
    "core/prefs.json",
    "core/history.json",
    "core/session.json",
    "core/saved_data.json",      # the login vault
    "core/widevine",             # already-fetched DRM component - no need to re-download
    "core/widevine_fetch_log.json",
    "core/.glass_insight_ok",
    ".venv",                     # machine-specific; also never shipped in a release archive
]


def _local_version() -> str:
    try:
        with open(VERSION_PATH, "r", encoding="utf-8") as f:
            return f.read().strip() or "v0.0.0"
    except OSError:
        return "v0.0.0"           # no VERSION file yet -> treat as "older than anything tagged"


def _latest_release():
    """(tag, zipball_url) for the newest GitHub release, or (None, None) on
    any failure at all - offline, rate-limited, no releases yet, whatever.
    Never raises."""
    try:
        req = urllib.request.Request(
            API_LATEST,
            headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT_CHECK) as r:
            data = json.load(r)
        tag = data.get("tag_name")
        zip_url = data.get("zipball_url")
        if not tag or not zip_url:
            return None, None
        return tag, zip_url
    except Exception:
        return None, None


def _already_skipped(tag: str) -> bool:
    try:
        with open(SKIP_MARKER, "r", encoding="utf-8") as f:
            return f.read().strip() == tag
    except OSError:
        return False


def _remember_skip(tag: str) -> None:
    try:
        with open(SKIP_MARKER, "w", encoding="utf-8") as f:
            f.write(tag)
    except OSError:
        pass


def _preserve_abs_paths():
    return [os.path.normpath(os.path.join(ROOT, p)) for p in PRESERVE]


def _copy_tree_preserving(src_root: str, dst_root: str) -> None:
    """Copy every file from src_root into dst_root, except anything under a
    PRESERVE path. Preserved DIRECTORIES are pruned from the walk entirely
    (never even descended into), not just skipped file-by-file, so nothing
    under core/projects/ or .venv is ever touched regardless of what the
    release archive contains there."""
    preserved = _preserve_abs_paths()
    for dirpath, dirnames, filenames in os.walk(src_root):
        rel = os.path.relpath(dirpath, src_root)
        dst_dir = os.path.normpath(os.path.join(dst_root, rel)) if rel != "." else dst_root
        dirnames[:] = [d for d in dirnames
                       if os.path.normpath(os.path.join(dst_dir, d)) not in preserved]
        os.makedirs(dst_dir, exist_ok=True)
        for fn in filenames:
            dst_file = os.path.normpath(os.path.join(dst_dir, fn))
            if dst_file in preserved:
                continue
            shutil.copy2(os.path.join(dirpath, fn), dst_file)


def _find_extracted_root(tmpdir: str) -> str | None:
    """GitHub's zipball extracts into one top-level folder named like
    owner-repo-<shortsha>/ - find it rather than assuming an exact name,
    since the short SHA suffix changes every release."""
    owner, name = REPO.split("/")
    prefix = f"{owner}-{name}".lower()
    for entry in os.listdir(tmpdir):
        full = os.path.join(tmpdir, entry)
        if os.path.isdir(full) and entry.lower().startswith(prefix):
            return full
    return None


def _apply_update(tag: str, zip_url: str) -> bool:
    print(f"  [*] Downloading {tag}...")
    tmpdir = tempfile.mkdtemp(prefix="glass_update_")
    zpath = os.path.join(tmpdir, "update.zip")
    try:
        req = urllib.request.Request(zip_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT_DOWNLOAD) as r, open(zpath, "wb") as f:
            shutil.copyfileobj(r, f)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmpdir)
        src_root = _find_extracted_root(tmpdir)
        if src_root is None:
            print("  [!] Update package didn't look right - skipping, launching current version.")
            return False
        _copy_tree_preserving(src_root, ROOT)
        with open(VERSION_PATH, "w", encoding="utf-8") as f:
            f.write(tag)
        if os.path.isfile(SKIP_MARKER):
            try:
                os.remove(SKIP_MARKER)
            except OSError:
                pass
        print(f"  [+] Updated to {tag}.")
        return True
    except Exception as e:
        print(f"  [!] Update failed ({e}) - launching the current version instead.")
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def check_and_maybe_update(auto_yes: bool = False) -> bool:
    """Returns True if an update was actually applied. Prompts on stdin
    unless auto_yes is set (for non-interactive testing)."""
    local = _local_version()
    tag, zip_url = _latest_release()
    if not tag:
        return False                      # offline / rate-limited / no releases - just launch
    if tag == local:
        return False                      # already current
    if _already_skipped(tag):
        return False                      # user already said no to exactly this version

    print(f"\n  [*] Update available ({local} -> {tag})")
    if auto_yes:
        ans = "y"
    else:
        try:
            ans = input("      Update now? (y/n): ").strip().lower()
        except EOFError:
            ans = "n"
    if ans != "y":
        _remember_skip(tag)
        return False
    return _apply_update(tag, zip_url)


if __name__ == "__main__":
    check_and_maybe_update()
