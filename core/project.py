"""
Glass projects
==============
A project is a folder under projects/ containing one or more .glass scripts
plus a `project.json` manifest that records the project name, its scripts, and
the entry script. The manifest is what the server reads, and what `getGlass`
uses to keep script lookups inside one project.

    projects/
      MyProject/
        project.json     <- manifest (name, entry, scripts)
        home.glass       <- entry script
        page2.glass
"""

from __future__ import annotations
import json
import os
import time

MANIFEST = "project.json"

DEFAULT_STARTER = '''// Entry script for this project. Other scripts in the same project can be
// opened with getGlass. openNew: false replaces this view; true opens a new tab.

*.main {
    menu.full
    background: #0f1419
    center { center }
    title: {ProjectName}

    text "New Glass project." { color: #6cf09a }
    button { { color: #1e88e5, width: 200, height: 38 } "Go to page 2" } { action: getGlass, target: page2, openNew: false }
    button { { color: #2e7d32, width: 200, height: 38 } "Page 2 in new tab" } { action: getGlass, target: page2, openNew: true }
}
'''

SECOND_PAGE = '''*.main {
    menu.full
    background: #11161c
    center { center }
    title: Page 2

    text "This is page2.glass in the same project." { color: #6cf09a }
    button { { color: #e53935, width: 200, height: 38 } "Back home" } { action: getGlass, target: home, openNew: false }
}
'''


def manifest_path(project_dir):
    return os.path.join(project_dir, MANIFEST)


def is_project(path):
    return os.path.isdir(path) and os.path.isfile(manifest_path(path))


def list_projects(projects_root):
    out = []
    if os.path.isdir(projects_root):
        for name in sorted(os.listdir(projects_root)):
            d = os.path.join(projects_root, name)
            if is_project(d):
                out.append(name)
    return out


def scan_scripts(project_dir):
    if not os.path.isdir(project_dir):
        return []
    return sorted(n for n in os.listdir(project_dir) if n.endswith(".glass"))


def load_manifest(project_dir):
    try:
        with open(manifest_path(project_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_manifest(project_dir, data):
    import atomicio
    atomicio.write_json(manifest_path(project_dir), data, indent=2)


def refresh_manifest(project_dir):
    """Re-scan scripts and rewrite the manifest; returns the manifest dict."""
    data = load_manifest(project_dir)
    data.setdefault("name", os.path.basename(project_dir.rstrip("/\\")))
    data.setdefault("version", 1)
    data["scripts"] = scan_scripts(project_dir)
    if data.get("entry") not in data["scripts"]:
        data["entry"] = data["scripts"][0] if data["scripts"] else ""
    save_manifest(project_dir, data)
    return data


def create_project(projects_root, name):
    """Create a new, empty project folder (just project.json, no scripts yet)."""
    safe = "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip()
    safe = safe.replace(" ", "_") or "Untitled"
    d = os.path.join(projects_root, safe)
    os.makedirs(d, exist_ok=True)
    data = {
        "name": safe,
        "version": 1,
        "entry": "",
        "scripts": scan_scripts(d),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_manifest(d, data)
    return d
