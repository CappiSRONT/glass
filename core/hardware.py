"""Glass hardware helpers - for the #overrideOPLim pragma.

Cross-platform total-system-RAM and current-process-RAM detection, plus
parsing the g/m/k RAM-spec shorthand ("g3" = 3 gigabytes). No third-party
dependency (no psutil) - Windows goes through ctypes + the real WinAPI,
POSIX through /proc or the stdlib resource module.

Everything here is read-only detection. Nothing in this module allocates or
reserves memory - see the note in engine.py's apply_override_limits() for why.
"""

import sys
import re

_UNITS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}


def parse_ram_spec(spec):
    """'g3' -> 3*1024**3 bytes. 'm512' -> 512 MB. 'k100' -> 100 KB.
    Returns None if it doesn't look like a valid spec."""
    if spec is None:
        return None
    s = str(spec).strip().lower()
    m = re.fullmatch(r"([gmk])\s*([0-9]*\.?[0-9]+)", s)
    if not m:
        return None
    unit, num = m.group(1), m.group(2)
    try:
        return int(float(num) * _UNITS[unit])
    except (TypeError, ValueError):
        return None


def format_ram_bytes(n):
    """Bytes -> a short human string, e.g. 3221225472 -> '3.0 GB'."""
    if n is None:
        return "an unknown amount of"
    for unit, size in (("GB", _UNITS["g"]), ("MB", _UNITS["m"]), ("KB", _UNITS["k"])):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} bytes"


def total_ram_bytes():
    """Total installed system RAM in bytes, or None if it can't be determined
    (detection failure should never be treated as 'the user has 0 RAM')."""
    try:
        if sys.platform.startswith("win"):
            return _win_total_ram()
        if sys.platform == "darwin":
            return _mac_total_ram()
        return _linux_total_ram()
    except Exception:
        return None


def process_ram_bytes():
    """This process's current real memory usage in bytes, or None."""
    try:
        if sys.platform.startswith("win"):
            return _win_process_ram()
        return _posix_process_ram()
    except Exception:
        return None


# ---- Windows ----------------------------------------------------------------
def _win_total_ram():
    import ctypes

    class MEMSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    stat = MEMSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return None
    return int(stat.ullTotalPhys)


def _win_process_ram():
    import ctypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return None
    return int(counters.WorkingSetSize)


# ---- Linux --------------------------------------------------------------
def _linux_total_ram():
    with open("/proc/meminfo", "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                return kb * 1024
    return None


def _posix_process_ram():
    """Linux: /proc/self/status VmRSS (kB). Mac: resource.ru_maxrss (bytes,
    unlike Linux where the SAME field is kilobytes - a well-known quirk)."""
    if sys.platform == "darwin":
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    import resource
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


# ---- macOS ----------------------------------------------------------------
def _mac_total_ram():
    import ctypes
    libc = ctypes.CDLL("libc.dylib")
    size = ctypes.c_uint64(0)
    size_len = ctypes.c_size_t(ctypes.sizeof(size))
    if libc.sysctlbyname(b"hw.memsize", ctypes.byref(size), ctypes.byref(size_len), None, 0) != 0:
        return None
    return int(size.value)
