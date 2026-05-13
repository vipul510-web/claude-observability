#!/usr/bin/env python3
"""
Installs Claude Code observability hooks into ~/.claude/settings.json.

Usage:
    python install.py          # install
    python install.py remove   # remove hooks
"""

import json
import sys
import shutil
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
PROJECT_DIR   = Path(__file__).parent.absolute()
HOOK_SCRIPT   = PROJECT_DIR / "hooks" / "capture.py"


def find_python() -> str:
    """Return the best available Python executable."""
    venv_python = PROJECT_DIR / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH) as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                print(f"⚠  {SETTINGS_PATH} has invalid JSON — backing up and starting fresh.")
                shutil.copy(SETTINGS_PATH, SETTINGS_PATH.with_suffix(".json.bak"))
                return {}
    return {}


def save_settings(settings: dict):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)
    print(f"✓  Saved {SETTINGS_PATH}")


def hook_command(python: str, event: str) -> str:
    return f"{python} {HOOK_SCRIPT} {event}"


def add_hook(settings: dict, event_key: str, command: str, matcher: str = ""):
    hooks = settings.setdefault("hooks", {})
    event_hooks = hooks.setdefault(event_key, [])

    # Check if already installed
    for entry in event_hooks:
        for h in entry.get("hooks", []):
            if h.get("command") == command:
                print(f"  already installed: {event_key}")
                return

    if not event_hooks:
        event_hooks.append({"matcher": matcher, "hooks": [{"type": "command", "command": command}]})
    else:
        # Append to first entry
        event_hooks[0].setdefault("hooks", []).append({"type": "command", "command": command})

    print(f"  ✓ {event_key} hook added")


def remove_hook(settings: dict, event_key: str, command: str):
    for entry in settings.get("hooks", {}).get(event_key, []):
        entry["hooks"] = [h for h in entry.get("hooks", []) if h.get("command") != command]
    print(f"  ✓ {event_key} hook removed")


def install():
    print("\n=== Claude Code Observability — Hook Installer ===\n")

    python = find_python()
    print(f"Python: {python}")
    print(f"Hook script: {HOOK_SCRIPT}")
    print(f"Settings: {SETTINGS_PATH}\n")

    if not HOOK_SCRIPT.exists():
        print(f"✗  Hook script not found: {HOOK_SCRIPT}")
        print("   Run this script from the claude-observability directory.")
        sys.exit(1)

    settings = load_settings()

    add_hook(settings, "Stop",        hook_command(python, "stop"),      matcher="")
    add_hook(settings, "PostToolUse", hook_command(python, "post_tool"), matcher=".*")

    save_settings(settings)

    # Ensure data dir exists
    (PROJECT_DIR / "data").mkdir(exist_ok=True)

    print("""
┌─────────────────────────────────────────────────────────┐
│  Hooks installed successfully!                          │
│                                                         │
│  Start the dashboard:                                   │
│    ./run.sh                                             │
│                                                         │
│  Then open: http://localhost:7842                       │
│                                                         │
│  Data will appear after your next Claude Code session.  │
└─────────────────────────────────────────────────────────┘
""")


def uninstall():
    print("\n=== Claude Code Observability — Removing Hooks ===\n")
    python = find_python()
    settings = load_settings()

    remove_hook(settings, "Stop",        hook_command(python, "stop"))
    remove_hook(settings, "PostToolUse", hook_command(python, "post_tool"))

    save_settings(settings)
    print("\n✓  Hooks removed. Dashboard data in data/ is untouched.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "remove":
        uninstall()
    else:
        install()
