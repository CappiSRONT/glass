"""Glass preferences: tiny persistent key -> value store.

Used from .glass scripts as:
    pref.save("nameofsave", value)     # value: bool, int, float/double, or string
    pref.load("nameofsave")            # returns the stored value (or 0 if unset)
'preference' is accepted as an alias for 'pref'. Values are kept in a JSON file
next to the app so they survive between runs.
"""

import json
import os

import atomicio

HERE = os.path.dirname(os.path.abspath(__file__))
PREFS_PATH = os.path.join(HERE, "prefs.json")

_cache = None


def _all():
    global _cache
    if _cache is None:
        try:
            with open(PREFS_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
            if not isinstance(_cache, dict):
                _cache = {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def save(name, value):
    """Store a value (bool/int/float/str). Returns the value."""
    d = _all()
    d[str(name)] = _jsonable(value)
    atomicio.write_json(PREFS_PATH, d, indent=2)
    return value


def load(name, default=0):
    """Return the stored value for name, or default (0) if not set."""
    return _all().get(str(name), default)


def has(name):
    return str(name) in _all()


def delete(name):
    d = _all()
    d.pop(str(name), None)
    atomicio.write_json(PREFS_PATH, d, indent=2)


def _jsonable(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)
