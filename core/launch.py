#!/usr/bin/env python3
"""
Glass launcher
==============
Run this to start Glass. On the first run (or if a dependency is missing) it
installs what's needed automatically, then opens the browser.

    python launch.py
"""

import sys

from bootstrap import ensure_dependencies, ensure_optional, BROWSER_DEPS

if not ensure_dependencies(BROWSER_DEPS):
    try:
        input("\nSetup did not complete. Press Enter to exit...")
    except EOFError:
        pass
    sys.exit(1)

ensure_optional()      # audio quality/hertz support; safe to skip if it fails

import browser
browser.main()
