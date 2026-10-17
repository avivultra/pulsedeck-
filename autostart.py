"""Start PulseDeck with Windows — a switch in the strip menu, never set on
PulseDeck's own initiative.

It is one value under HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run:
for this user only, no admin rights, and listed in Task Manager → Startup, so
it can be seen and turned off there too. Task Manager's own off switch lives
in the StartupApproved key; the menu reads it, so both always agree.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from app_paths import CODE_DIR, is_frozen

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "PulseDeck"


def command() -> str:
    """What Windows runs at sign-in: this same PulseDeck, strip + tray + history."""
    if is_frozen():
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")          # no console window at sign-in
    return f'"{pythonw if pythonw.exists() else exe}" "{CODE_DIR / "monitor.py"}" --dock --history --tray'


def _read(key_path: str):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            return winreg.QueryValueEx(key, VALUE_NAME)[0]
    except OSError:
        return None


def _delete(key_path: str) -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass


def is_enabled() -> bool:
    """On when the Run value exists and Task Manager has not turned it off
    (StartupApproved: first byte 2 = on, 3 = off)."""
    if sys.platform != "win32" or not _read(RUN_KEY):
        return False
    approved = _read(APPROVED_KEY)
    return not (isinstance(approved, bytes) and approved[:1] and approved[0] & 1)


def set_enabled(on: bool) -> None:
    """Raises OSError when the registry refuses; the caller tells the user."""
    if sys.platform != "win32":
        raise OSError("Start with Windows is only available on Windows")
    import winreg
    _delete(APPROVED_KEY)          # a stale "off" from Task Manager would hide our "on"
    if on:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
        log.info("Start with Windows: on (%s)", command())
    else:
        _delete(RUN_KEY)
        log.info("Start with Windows: off")
