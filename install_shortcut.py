"""Create a desktop shortcut to PulseDeck.

Run once:    python install_shortcut.py
Or via:      python monitor.py --install-shortcut

On Windows, creates a .lnk pointing at Start-Monitor-Hidden.vbs with the
PulseDeck custom icon. On Linux, creates a .desktop file. On macOS, prints
a manual-setup hint (AppleScript .app generation is outside our scope).
"""
from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
ICON_ICO = PROJECT_DIR / "assets" / "icon.ico"
ICON_PNG = PROJECT_DIR / "assets" / "icon.png"


def _desktop_dir() -> Path:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            CSIDL_DESKTOPDIRECTORY = 0x10
            SHGFP_TYPE_CURRENT = 0
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(
                None, CSIDL_DESKTOPDIRECTORY, None, SHGFP_TYPE_CURRENT, buf
            )
            return Path(buf.value)
        except Exception:
            log.exception("Could not resolve Windows Desktop folder; using fallback")
    # Cross-platform fallback
    return Path.home() / "Desktop"


def _windows_install(shortcut_name: str = "PulseDeck") -> Path:
    """Create the .lnk on the Desktop. Returns the shortcut path."""
    import subprocess

    desktop = _desktop_dir()
    desktop.mkdir(parents=True, exist_ok=True)
    target_vbs = PROJECT_DIR / "Start-Monitor-Hidden.vbs"
    if not target_vbs.exists():
        raise FileNotFoundError(f"Launcher not found: {target_vbs}")

    icon = ICON_ICO if ICON_ICO.exists() else None
    icon_arg = f"{icon},0" if icon else f"{os.environ.get('SystemRoot', 'C:\\Windows')}\\System32\\shell32.dll,173"

    shortcut_path = desktop / f"{shortcut_name}.lnk"

    # Use PowerShell + WScript.Shell COM to build the .lnk (no extra deps).
    ps_script = f"""
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut('{shortcut_path}')
$lnk.TargetPath = '{target_vbs}'
$lnk.WorkingDirectory = '{PROJECT_DIR}'
$lnk.WindowStyle = 7
$lnk.IconLocation = '{icon_arg}'
$lnk.Description = 'PulseDeck — Real-time CPU/RAM/Disk monitor with live charts, spike alerts, and process janitor'
$lnk.Save()
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"PowerShell failed creating shortcut:\n{result.stdout}\n{result.stderr}"
        )
    return shortcut_path


def _linux_install(shortcut_name: str = "PulseDeck") -> Path:
    """Create a .desktop file on the Desktop. Returns the file path."""
    desktop = _desktop_dir()
    desktop.mkdir(parents=True, exist_ok=True)

    monitor_py = PROJECT_DIR / "monitor.py"
    python_bin = sys.executable
    icon = ICON_PNG if ICON_PNG.exists() else ICON_ICO

    desktop_entry = f"""[Desktop Entry]
Type=Application
Name={shortcut_name}
Comment=Real-time CPU/RAM/Disk monitor with live charts and spike alerts
Exec={python_bin} {monitor_py} --dock --history --tray
Path={PROJECT_DIR}
Icon={icon if icon.exists() else ''}
Terminal=false
Categories=Utility;System;Monitor;
"""
    path = desktop / f"{shortcut_name}.desktop"
    path.write_text(desktop_entry, encoding="utf-8")
    path.chmod(0o755)
    return path


def install_shortcut(name: str | None = None) -> Path | None:
    """Top-level entry. Returns the created shortcut path or None on failure."""
    system = platform.system()
    try:
        if system == "Windows":
            return _windows_install(name or "PulseDeck")
        if system == "Linux":
            return _linux_install(name or "PulseDeck")
        if system == "Darwin":
            print("macOS: no automatic shortcut installer. Suggested approach:")
            print("  1. Open Automator and create an Application that runs:")
            print(f"     {sys.executable} {PROJECT_DIR / 'monitor.py'} --dock --history --tray")
            print(f"  2. Save it to ~/Desktop with the icon from {ICON_ICO}.")
            return None
    except Exception:
        log.exception("Failed to install shortcut")
        return None
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    path = install_shortcut()
    if path is not None:
        print(f"✓ Created shortcut: {path}")
    else:
        print("✗ Shortcut creation skipped or failed. See log for details.")
        sys.exit(1)
