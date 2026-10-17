"""One PulseDeck at a time.

Two copies would draw two strips, sample everything twice and write the same
history files. Starting with Windows and from a shortcut makes that easy to do
by accident, so the strip and tray modes take a named mutex first; a second
copy finds it taken, says so and exits.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

MUTEX_NAME = "Local\\PulseDeck.SingleInstance"   # "Local\": one per signed-in user session
ERROR_ALREADY_EXISTS = 183
NOTICE_SECONDS = 15

_handle = None   # held for the life of the process; Windows frees the name when it exits


def acquire() -> bool:
    """True when no other PulseDeck runs in this session (always True off
    Windows, and when the check itself fails — better two strips than none)."""
    global _handle
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        log.warning("CreateMutexW failed (error %s); not guarding against a second copy",
                    ctypes.get_last_error())
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _handle = handle
    return True


def already_running_text() -> str:
    # The Hebrew line opens with a Hebrew word: Windows lays a label out in
    # the direction of its first letter, and "PulseDeck כבר פועל, ..." put
    # its comma on the wrong side.
    from i18n import tr
    return tr("כבר פועל עותק של PulseDeck: הפס שלו מעל שורת המשימות, והסמל שלו ליד השעון.",
              "PulseDeck is already running: its strip sits above the taskbar, its icon next to the clock.")


def tell_already_running() -> None:
    """A small notice in PulseDeck's own look, mid-screen; returns when it is
    closed (or after NOTICE_SECONDS, so a hidden notice never lingers)."""
    import tkinter as tk

    from dark_menu import notice

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-alpha", 0.0)
    root.geometry(f"1x1+{root.winfo_screenwidth() // 2}+{root.winfo_screenheight() // 2}")
    root.update_idletasks()
    win = notice(root, "PulseDeck", already_running_text())
    win.bind("<Destroy>", lambda e: root.quit() if e.widget is win else None)
    root.after(NOTICE_SECONDS * 1000, root.quit)
    root.mainloop()
    root.destroy()
