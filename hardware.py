"""One-time detection of machine-specific features.

PulseDeck grew up on one Lenovo Legion laptop. Everything that only makes sense
on particular hardware is gated here, so the same code runs on any desktop or
laptop and simply hides what the machine does not have.

Every check runs once and is cached: they read the registry or walk the
process table, which is fine at startup and wasteful on every tick.

Config override (config.json):
  "hardware": {"fan_button": "auto" | true | false}
"""

from __future__ import annotations

import functools
import logging
import os
from pathlib import Path

import psutil

log = logging.getLogger(__name__)

# Lenovo's gaming utility ships as "Nerve Center" / "Nerve Sense" on Legion and
# Y-series machines; its Extreme Cooling toggle is the Ctrl+Shift+1 hotkey that
# fan_auto.py sends. Without the utility the hotkey would go to whatever window
# has focus, so the button must not appear.
_NERVE_PROCESS_MARKERS = ("nervecenter", "nervesense")
_NERVE_INSTALL_GLOBS = ("Lenovo/*Nerve*", "Lenovo/*nerve*")


@functools.lru_cache(maxsize=1)
def system_manufacturer() -> str:
    """BIOS manufacturer string, lower-case ('' when unknown)."""
    if os.name != "nt":
        try:
            return Path("/sys/class/dmi/id/sys_vendor").read_text().strip().lower()
        except OSError:
            return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\BIOS") as key:
            return str(winreg.QueryValueEx(key, "SystemManufacturer")[0]).strip().lower()
    except OSError:
        return ""


def _nerve_center_running() -> bool:
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if any(marker in name for marker in _NERVE_PROCESS_MARKERS):
            return True
    return False


def _nerve_center_installed() -> bool:
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if not base:
            continue
        for pattern in _NERVE_INSTALL_GLOBS:
            if any(Path(base).glob(pattern)):
                return True
    return False


@functools.lru_cache(maxsize=1)
def lenovo_cooling_available() -> bool:
    """True when the Extreme Cooling hotkey has something to talk to."""
    if os.name != "nt" or "lenovo" not in system_manufacturer():
        return False
    try:
        return _nerve_center_running() or _nerve_center_installed()
    except Exception:
        log.debug("Nerve Center detection failed", exc_info=True)
        return False


def fan_button_enabled(cfg: dict | None = None) -> bool:
    """Whether the dock shows the 🌀 Extreme Cooling button."""
    setting = ((cfg or {}).get("hardware") or {}).get("fan_button", "auto")
    if isinstance(setting, bool):
        return setting
    return lenovo_cooling_available()


@functools.lru_cache(maxsize=1)
def has_battery() -> bool:
    try:
        return psutil.sensors_battery() is not None
    except Exception:
        return False
