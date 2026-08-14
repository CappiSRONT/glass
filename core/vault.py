"""Glass saved-logins vault - 100% local, never sent anywhere.

Passwords are encrypted at rest:
  * Windows: the OS DPAPI (CryptProtectData), tied to your Windows account, so
    the file is unreadable on another account/machine.
  * Other OSes: a light obfuscation only (clearly NOT real encryption).

This keeps logins off the internet and off other accounts, but it is not a
hardened, audited password manager - anything running as you could read them.
Usernames are kept in clear so the settings list can show them.
"""

import base64
import json
import os
import sys

import atomicio

HERE = os.path.dirname(os.path.abspath(__file__))
VAULT_PATH = os.path.join(HERE, "saved_data.json")


# ---- at-rest encryption ---------------------------------------------------
def _win_dpapi(data, protect=True):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    fn = (ctypes.windll.crypt32.CryptProtectData if protect
          else ctypes.windll.crypt32.CryptUnprotectData)
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI call failed")
    out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return out


def _encrypt(text):
    raw = text.encode("utf-8")
    if sys.platform.startswith("win"):
        try:
            return "dpapi:" + base64.b64encode(_win_dpapi(raw, True)).decode("ascii")
        except Exception:
            pass
    return "b64:" + base64.b64encode(raw).decode("ascii")     # obfuscation only


def _decrypt(blob):
    try:
        tag, _, payload = blob.partition(":")
        data = base64.b64decode(payload)
        if tag == "dpapi" and sys.platform.startswith("win"):
            return _win_dpapi(data, False).decode("utf-8")
        if tag == "b64":
            return data.decode("utf-8")
    except Exception:
        pass
    return ""


# ---- store ----------------------------------------------------------------
def _load():
    try:
        with open(VAULT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(data):
    return atomicio.write_json(VAULT_PATH, data, indent=2)


def save_login(host, username, password):
    if not host:
        return False
    data = _load()
    data[host] = {"username": username or "",
                  "password": _encrypt(password or ""),
                  "never": False}
    return _write(data)


def get_login(host):
    """Return {'username','password'} (decrypted) or None."""
    e = _load().get(host)
    if not e or e.get("never"):
        return None
    return {"username": e.get("username", ""),
            "password": _decrypt(e.get("password", ""))}


def has_login(host):
    e = _load().get(host)
    return bool(e) and not e.get("never")


def set_never(host):
    data = _load()
    data[host] = {"username": "", "password": "", "never": True}
    _write(data)


def list_logins():
    """[(host, username)] for everything saved (excluding 'never' markers)."""
    out = []
    for host, e in _load().items():
        if not e.get("never"):
            out.append((host, e.get("username", "")))
    return sorted(out)


def reveal(host):
    e = _load().get(host)
    return _decrypt(e.get("password", "")) if e and not e.get("never") else ""


def delete_login(host):
    data = _load()
    if host in data:
        del data[host]
        return _write(data)
    return False


def clear_all():
    return _write({})
