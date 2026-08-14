"""Local browsing history + session restore (Zen-style).

Everything is stored on this PC only (history.json / session.json next to the
app); nothing is uploaded. History is a list of visits {url,title,time}; the
session is the set of open web tabs, offered for restore on the next launch.
"""

import json
import os
import time

import atomicio

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(HERE, "history.json")
SESSION_PATH = os.path.join(HERE, "session.json")
MAX_HISTORY = 5000


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data):
    return atomicio.write_json(path, data)


_hist_cache = None


def _hist():
    """The in-memory history list, loaded from disk once. Avoids re-reading
    the whole file from disk on every single page visit - add_history() used
    to _load() the file fresh every call just to append one entry."""
    global _hist_cache
    if _hist_cache is None:
        _hist_cache = _load(HISTORY_PATH, [])
        if not isinstance(_hist_cache, list):
            _hist_cache = []
    return _hist_cache


# ---- history --------------------------------------------------------------
def add_history(url, title=""):
    if not url or not url.startswith(("http://", "https://")):
        return
    hist = _hist()
    if hist and hist[-1].get("url") == url:        # collapse consecutive repeats
        hist[-1]["title"] = title or hist[-1].get("title", "")
        hist[-1]["time"] = time.time()
    else:
        hist.append({"url": url, "title": title or url, "time": time.time()})
    if len(hist) > MAX_HISTORY:
        del hist[:-MAX_HISTORY]     # trim in place - still writes every call,
    _write(HISTORY_PATH, hist)      # just skips the redundant read beforehand


def load_history(limit=None):
    """Most-recent-first list of visits."""
    hist = list(reversed(_hist()))
    return hist[:limit] if limit else hist


def search_history(query, limit=200):
    q = (query or "").lower().strip()
    out = []
    for h in load_history():
        if not q or q in h.get("url", "").lower() or q in h.get("title", "").lower():
            out.append(h)
            if len(out) >= limit:
                break
    return out


def clear_history():
    global _hist_cache
    _hist_cache = []
    _write(HISTORY_PATH, [])


# ---- session --------------------------------------------------------------
def save_session(tabs):
    """tabs: list of {'url','title'} for the open web tabs."""
    _write(SESSION_PATH, [t for t in tabs if t.get("url", "").startswith(("http://", "https://"))])


def load_session():
    return _load(SESSION_PATH, [])


def clear_session():
    _write(SESSION_PATH, [])
