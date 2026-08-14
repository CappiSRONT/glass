"""Tiny shared helper: atomic JSON writes.

Several local data stores (prefs, history, the login vault, theme files,
project manifests) each separately re-implemented "open the real file and
write" on their own. If the process dies mid-write - a crash, a force-quit,
a power cut - that leaves a truncated file on disk. Every one of those
stores treats a corrupt/unparsable file as "start empty" on next load, so a
badly-timed crash could silently wipe all of it: every setting, all of
history, every saved login.

write_json() avoids that by writing to a temp file in the same folder first,
then atomically replacing the real file with os.replace() - on both Windows
and POSIX that either fully succeeds or leaves the original completely
untouched. There's no in-between, half-written state to ever be caught in.
"""

import json
import os
import tempfile


def write_json(path, data, **json_kwargs):
    """Write `data` to `path` as JSON, atomically. Returns True on success,
    False if it couldn't write at all (disk full, permissions, etc.) - in
    which case the ORIGINAL file (if any) is left exactly as it was."""
    folder = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    try:
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=folder)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, **json_kwargs)
        os.replace(tmp, path)          # atomic on both Windows and POSIX
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
