"""
Centered strip flush with the Windows taskbar (cannot embed inside the bar itself).

Shows CPU, system/GPU temps, RAM+GiB, disk, swap, network rates, battery when available.
"""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from pathlib import Path

import psutil

from monitor import HistoryLogger, collect_snapshot, disk_root_path, format_gib_usage, spike_reports_enabled
from temperature_readings import read_gpu_temp_celsius


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


def _fmt_bps(bps: float) -> str:
    if bps < 512:
        return f"{bps:.0f}B/s"
    kb = bps / 1024.0
    if kb < 1024:
        return f"{kb:.1f}KB/s"
    return f"{kb / 1024:.1f}MB/s"


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

    net_last: dict[str, float | int | None] = {"t": None, "sent": None, "recv": None}

    root = tk.Tk()
    root.title("מוניטור ביצועים")
    root.overrideredirect(True)
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    # Bottom strip: reads a bit like the taskbar edge (visual “dock”).
    taskbar_edge = "#1a1c22"
    bg = "#1e2229"
    fg = "#e6e9ef"
    accent = "#8fbcbb"
    dim = "#b8c0cc"

    edge = tk.Frame(root, bg=taskbar_edge, height=3)
    edge.pack(side=tk.BOTTOM, fill=tk.X)

    body = tk.Frame(root, bg=bg)
    body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    font_big = ("Segoe UI", 10) if os.name == "nt" else ("Ubuntu", 10)
    font_small = ("Segoe UI", 9) if os.name == "nt" else ("Ubuntu", 9)

    top_row = tk.Frame(body, bg=bg)
    lbl_cpu = tk.Label(top_row, text="CPU …", bg=bg, fg=fg, font=font_big, anchor="w")
    lbl_temp = tk.Label(top_row, text="טמפ …", bg=bg, fg=accent, font=font_small, anchor="e")
    lbl_cpu.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=16, pady=4)
    lbl_temp.pack(side=tk.RIGHT, padx=16, pady=4)
    top_row.pack(fill=tk.X)

    var_line2 = tk.StringVar(value="טוען…")
    lbl2 = tk.Label(body, textvariable=var_line2, bg=bg, fg=fg, font=font_small, padx=16, pady=2)
    lbl2.pack(fill=tk.X)

    var_line3 = tk.StringVar(value="")
    lbl3 = tk.Label(body, textvariable=var_line3, bg=bg, fg=dim, font=font_small, padx=16, pady=4)
    lbl3.pack(fill=tk.X)

    def place_window() -> None:
        root.update_idletasks()
        w = max(560, min(1040, root.winfo_reqwidth() + 28))
        h = root.winfo_reqheight() + 4
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        rect = _taskbar_bottom_rect()
        if rect:
            top = rect[1]
            # Flush with taskbar top (no gap) — sits visually “on” the bar.
            y = max(0, top - h)
        else:
            y = max(0, sh - h - 48)
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

        lbl_cpu.config(text=f"CPU  {snap.cpu_percent:.0f}%  ·  {snap.cpu_logical} ליבות")
        temp_parts: list[str] = []
        if snap.temp_celsius is not None:
            temp_parts.append(f"מחשב {snap.temp_celsius:.0f}°C")
        gt = read_gpu_temp_celsius()
        if gt is not None:
            temp_parts.append(f"GPU {gt:.0f}°C")
        lbl_temp.config(text=" · ".join(temp_parts) if temp_parts else "טמפ —")

        d_pct = "—" if snap.disk_percent is None else f"{snap.disk_percent:.0f}%"
        short = snap.disk_path.rstrip("\\/") or snap.disk_path
        if len(short) > 6:
            short = short[:5] + "…"
        var_line2.set(
            f"RAM {snap.ram_percent:.0f}%  ({format_gib_usage(snap.ram_used, snap.ram_total)})  ·  "
            f"דיסק {short}: {d_pct}"
        )

        io = psutil.net_io_counters()
        now = time.time()
        net_txt = "רשת …"
        if net_last["t"] is not None and net_last["sent"] is not None and net_last["recv"] is not None:
            dt = now - float(net_last["t"])
            if dt > 0.05:
                up = (io.bytes_sent - int(net_last["sent"])) / dt
                dn = (io.bytes_recv - int(net_last["recv"])) / dt
                net_txt = f"↑{_fmt_bps(up)}  ↓{_fmt_bps(dn)}"
        net_last["t"] = now
        net_last["sent"] = io.bytes_sent
        net_last["recv"] = io.bytes_recv

        extras: list[str] = []
        if snap.swap_percent is not None:
            extras.append(f"סוויפ {snap.swap_percent:.0f}%")
        extras.append(net_txt)
        if snap.battery_percent is not None:
            plug = "חשמל" if snap.battery_plugged else "סוללה"
            extras.append(f"{plug} {snap.battery_percent:.0f}%")
        var_line3.set("  ·  ".join(extras))

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
