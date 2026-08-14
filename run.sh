#!/usr/bin/env bash
# Glass launcher for macOS / Linux
cd "$(dirname "$0")" || exit 1

printf '\n'
printf '   ===========================================\n'
printf '           G L A S S\n'
printf '     a transparent, you-own-it browser\n'
printf '   ===========================================\n\n'

# --- make sure this folder is actually writable - a protected/read-only ----
# location (a system-owned path, a read-only mount) is the most common real
# cause of "this needs root" failures. Glass keeps everything (its local
# Python environment, settings, saved projects) right next to this script,
# so if THIS spot isn't writable, nothing downstream will be either - catch
# it here with a clear fix instead of a confusing failure later.
if ! touch .glass_write_test 2>/dev/null; then
  echo "  [X] This folder isn't writable: $(pwd)"
  echo
  echo "      Glass needs to create a few files right next to this script -"
  echo "      a local Python environment, your settings, saved projects."
  echo
  echo "      Fix: move the whole extracted Glass folder somewhere you own,"
  echo "      like your home folder or Desktop, then run this again from there."
  exit 1
fi
rm -f .glass_write_test 2>/dev/null

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "  [X] Python 3.10+ not found. Install it from python.org and try again."
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "  [*] First run: creating a local environment..."
  "$PY" -m venv .venv
  if [ ! -x ".venv/bin/python" ]; then
    echo "  [!] Couldn't create a local Python environment here, so Glass will"
    echo "      use your system-wide Python instead. If installing dependencies"
    echo "      below fails with a permissions error, that's almost always a"
    echo "      system-protected Python needing sudo - the step below already"
    echo "      retries without it automatically if the first attempt fails."
  fi
fi
# shellcheck disable=SC1091
. .venv/bin/activate 2>/dev/null
VPY="$(pwd)/.venv/bin/python"
USING_VENV=1
if [ ! -x "$VPY" ]; then
  VPY="$PY"
  USING_VENV=0
fi

echo "  [*] Checking dependencies (first run can take a minute)..."
if ! "$VPY" -m pip install --disable-pip-version-check -r requirements.txt; then
  if [ "$USING_VENV" = "0" ]; then
    # --user (and --break-system-packages) only mean anything against a
    # system-wide Python - pip flatly rejects --user inside a venv ("User
    # site-packages are not visible in this virtualenv"), and the venv's
    # own location was already confirmed writable above, so a permissions
    # wall here would only ever come from the system-Python fallback path.
    echo "  [*] That needs elevated rights here - retrying without them..."
    "$VPY" -m pip install --disable-pip-version-check --user -r requirements.txt \
      || "$VPY" -m pip install --disable-pip-version-check --break-system-packages -r requirements.txt \
      || echo "  [!] Some dependencies may not have installed. Trying to start anyway..."
  else
    echo "  [!] Some dependencies may not have installed. Trying to start anyway..."
  fi
fi
echo

"$VPY" core/widevine_setup.py
READY="$(pwd)/core/.glass_ready"
rm -f "$READY" 2>/dev/null

echo "  [*] Starting Glass..."
nohup "$VPY" core/launch.py >/dev/null 2>&1 &

tries=0
while [ ! -f "$READY" ]; do
  tries=$((tries + 1))
  [ "$tries" -ge 120 ] && { echo; echo "  [!] Glass is taking a while - it may still be loading."; exit 1; }
  printf '.'
  sleep 1
done

echo
echo "  [+] Glass is open."
exit 0
