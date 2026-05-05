"""
Centered strip above the Windows taskbar (cannot embed inside the taskbar itself).

Shows live CPU / RAM / disk / temp; optional tray icon driven by the same sampler.
"""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from pathlib import Path

import psutil

from monitor import HistoryLogger, collect_snapshot, disk_root_path, spike_reports_enabled


def _taskbar_bottom_rect() -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of the taskbar, or None if unknown."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import Structure, byref, wintypes

    class RECT(Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class APPBARDATA(Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uCallbackMessage", wintypes.UINT),
            ("uEdge", wintypes.UINT),
            ("rc", RECT),
            ("lParam", wintypes.LPARAM),
        ]

    ABM_GETTASKBARPOS = 5
    abd = APPBARDATA()
    abd.cbSize = ctypes.sizeof(APPBARDATA)
    if ctypes.windll.shell32.SHAppBarMessage(ABM_GETTASKBARPOS, byref(abd)):
        r = abd.rc
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    return None


def run_dock_main(args: object) -> None:
    from metric_history import DEFAULT_CHART_PATH, DEFAULT_CSV_PATH
    from tray_runner import _open_path, _tray_tooltip, start_tray_daemon_visual

    stop = threading.Event()
    closing = False

    csv_path = Path(args.history_csv) if args.history_csv is not None else DEFAULT_CSV_PATH
    png_path = Path(args.chart_png) if args.chart_png is not None else DEFAULT_CHART_PATH
    history = HistoryLogger(
        enabled=bool(args.history),
        csv_path=csv_path,
        png_path=png_path,
        plot_every=int(args.plot_every),
        spike_reports=spike_reports_enabled(args),
    )
    disk_path = disk_root_path()
    interval_ms = max(400, int(float(args.interval) * 1000))

    tray_icon: object | None = None
    if getattr(args, "tray", False):
        tray_icon = start_tray_daemon_visual(
            args, stop, history, csv_path, png_path, on_quit_render_final=False
        )

    root = tk.Tk()
    root.title("מוניטור ביצועים")
    root.overrideredirect(True)
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    bg = "#1e2229"
    fg = "#e6e9ef"
    root.configure(bg=bg)
    font = ("Segoe UI", 11) if os.name == "nt" else ("Ubuntu", 11)
    var_line = tk.StringVar(value="טוען…")
    lbl = tk.Label(root, textvariable=var_line, bg=bg, fg=fg, font=font, padx=24, pady=8)
    lbl.pack()

    def format_line() -> str:
        return var_line.get()

    def place_window() -> None:
        root.update_idletasks()
        lbl.update_idletasks()
        w = max(480, min(960, lbl.winfo_reqwidth() + 48))
        h = lbl.winfo_reqheight() + 16
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        rect = _taskbar_bottom_rect()
        if rect:
            top = rect[1]
            y = max(0, top - h - 2)
        else:
            y = max(0, sh - h - 52)
        x = max(0, (sw - w) // 2)
        root.geometry(f"{w}x{h}+{x}+{y}")

    def on_close() -> None:
        nonlocal closing
        if closing:
            return
        closing = True
        stop.set()
        if tray_icon is not None:
            try:
                tray_icon.stop()  # type: ignore[attr-defined]
            except Exception:
                pass
        history.render_final()
        try:
            root.destroy()
        except tk.TclError:
            pass

    def _open_hist_folder(p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        _open_path(p.parent)

    def menu_popup(event: tk.Event) -> None:
        m = tk.Menu(root, tearoff=0)
        m.add_command(
            label="פתח גרף",
            command=lambda: _open_path(png_path) if png_path.exists() else _open_path(csv_path.parent),
        )
        m.add_command(
            label="תיקיית היסטוריה",
            command=lambda: _open_hist_folder(csv_path),
        )
        m.add_separator()
        m.add_command(label="יציאה", command=on_close)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    root.bind("<Button-3>", menu_popup)
    root.bind("<Escape>", lambda _e: on_close())

    psutil.cpu_percent(interval=0.1)
    place_counter = 0

    def tick() -> None:
        nonlocal place_counter
        if stop.is_set() or closing:
            root.after(0, on_close)
            return
        snap = collect_snapshot(disk_path)
        history.log(snap)
        t = "—" if snap.temp_celsius is None else f"{snap.temp_celsius:.0f}°C"
        d = "—" if snap.disk_percent is None else f"{snap.disk_percent:.0f}%"
        var_line.set(f"  CPU {snap.cpu_percent:.0f}%    RAM {snap.ram_percent:.0f}%    דיסק {d}    טמפ {t}  ")
        if tray_icon is not None:
            try:
                tray_icon.title = _tray_tooltip(snap)  # type: ignore[attr-defined]
            except Exception:
                pass
        place_counter += 1
        if place_counter <= 3 or place_counter % 20 == 0:
            place_window()
        root.after(interval_ms, tick)

    place_window()
    root.after(100, tick)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
