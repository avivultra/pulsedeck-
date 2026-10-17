"""Where PulseDeck keeps its files.

Run from source (python monitor.py), everything lives next to the code, as it
always has: config.json, .env and history/.

The packaged PulseDeck.exe must not write next to itself: it may sit in a
download folder, on a read-only drive or under Program Files, and a new
version replaces that folder. So its settings, key and history live in
%LOCALAPPDATA%\\PulseDeck, which stays put across versions.

PULSEDECK_DATA_DIR overrides both — for tests, and for trying the .exe
without touching the real data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Read-only files that ship with PulseDeck (assets/). In the .exe this is the
# bundle's own folder.
CODE_DIR = Path(__file__).resolve().parent


def is_frozen() -> bool:
    """True inside the packaged PulseDeck.exe (PyInstaller sets sys.frozen)."""
    return bool(getattr(sys, "frozen", False))


def _data_dir() -> Path:
    override = os.environ.get("PULSEDECK_DATA_DIR")
    if override:
        return Path(override)
    if is_frozen():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "PulseDeck"
    return CODE_DIR


DATA_DIR = _data_dir()
HISTORY_DIR = DATA_DIR / "history"
