"""
Centered strip flush with the Windows taskbar (cannot embed inside the bar itself).

Shows CPU, system/GPU temps, RAM+GiB, disk, swap, network rates, battery when available.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import tkinter as tk
from pathlib import Path

import psutil

log = logging.getLogger(__name__)

from monitor import HistoryLogger, collect_snapshot, disk_root_path, format_gib_usage, spike_reports_enabled
from temperature_readings import read_gpu_memory_mib, read_gpu_temp_celsius


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
    disk_path = getattr(args, "disk_path", None) or disk_root_path()
    interval_ms = max(400, int(float(args.interval) * 1000))

    tray_icon: object | None = None
    if getattr(args, "tray", False):
        tray_icon = start_tray_daemon_visual(
            args, stop, history, csv_path, png_path, on_quit_render_final=False
        )

    net_last: dict[str, float | int | None] = {"t": None, "sent": None, "recv": None}

    # Read persisted dock state (position + font scale + pin) — must be
    # available before _apply_pin_state() runs.
    import config as app_config
    _cfg = app_config.load_config()
    dock_cfg = _cfg.get("dock", {}) or {}
    saved_x = dock_cfg.get("x")
    saved_y = dock_cfg.get("y")
    font_scale = float(dock_cfg.get("font_scale", 1.0) or 1.0)
    font_scale = max(0.7, min(1.6, font_scale))
    pinned = bool(dock_cfg.get("pinned", True))

    root = tk.Tk()
    root.title("מוניטור ביצועים")
    root.overrideredirect(True)

    def _apply_pin_state() -> None:
        try:
            root.attributes("-topmost", bool(pinned))
            if pinned:
                root.lift()
        except tk.TclError:
            pass

    _apply_pin_state()

    dispatcher = getattr(args, "_alert_dispatcher", None)
    if dispatcher is not None and spike_reports_enabled(args):
        from alerts import make_alert_callback

        history.alert_callback = make_alert_callback(dispatcher, root_provider=lambda: root)

    def _persist_dock_state() -> None:
        try:
            cfg = app_config.load_config()
            cfg.setdefault("dock", {})
            cfg["dock"]["x"] = root.winfo_x()
            cfg["dock"]["y"] = root.winfo_y()
            cfg["dock"]["font_scale"] = round(font_scale, 2)
            cfg["dock"]["pinned"] = pinned
            app_config.save_config(cfg)
        except Exception:
            log.exception("Could not persist dock state")

    # Restored 3-row dock (richer look)
    taskbar_edge = "#1a1c22"
    bg = "#1e2229"
    fg = "#e6e9ef"
    accent = "#8fbcbb"
    dim = "#b8c0cc"

    def _font(size_pt: float, bold: bool = False) -> tuple:
        size = max(7, int(round(size_pt * font_scale)))
        family = "Segoe UI" if os.name == "nt" else "DejaVu Sans"
        return (family, size, "bold") if bold else (family, size)

    edge = tk.Frame(root, bg=taskbar_edge, height=3)
    edge.pack(side=tk.BOTTOM, fill=tk.X)

    body = tk.Frame(root, bg=bg)
    body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    top_row = tk.Frame(body, bg=bg)
    lbl_cpu = tk.Label(top_row, text="CPU …", bg=bg, fg=fg, font=_font(10), anchor="w")
    lbl_temp = tk.Label(top_row, text="טמפ …", bg=bg, fg=accent, font=_font(9), anchor="e")
    lbl_cpu.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=16, pady=4)
    lbl_temp.pack(side=tk.RIGHT, padx=16, pady=4)
    top_row.pack(fill=tk.X)

    var_line2 = tk.StringVar(value="טוען…")
    lbl2 = tk.Label(body, textvariable=var_line2, bg=bg, fg=fg, font=_font(9),
                    padx=16, pady=2)
    lbl2.pack(fill=tk.X)

    var_line3 = tk.StringVar(value="")
    lbl3 = tk.Label(body, textvariable=var_line3, bg=bg, fg=dim, font=_font(9),
                    padx=16, pady=4)
    lbl3.pack(fill=tk.X)

    # Drag-to-move: bind on body and child labels
    drag_state = {"x": 0, "y": 0, "moved": False}

    def _on_drag_start(event: tk.Event) -> None:
        drag_state["x"] = event.x
        drag_state["y"] = event.y
        drag_state["moved"] = False

    def _on_drag_motion(event: tk.Event) -> None:
        nx = root.winfo_x() + (event.x - drag_state["x"])
        ny = root.winfo_y() + (event.y - drag_state["y"])
        root.geometry(f"+{nx}+{ny}")
        drag_state["moved"] = True

    def _on_drag_release(_event: tk.Event) -> None:
        if drag_state["moved"]:
            _persist_dock_state()

    for w in (body, top_row, lbl_cpu, lbl_temp, lbl2, lbl3):
        w.bind("<Button-1>", _on_drag_start)
        w.bind("<B1-Motion>", _on_drag_motion)
        w.bind("<ButtonRelease-1>", _on_drag_release)

    def _bump_font(delta: float) -> None:
        nonlocal font_scale
        font_scale = max(0.7, min(1.6, font_scale + delta))
        lbl_cpu.config(font=_font(10))
        lbl_temp.config(font=_font(9))
        lbl2.config(font=_font(9))
        lbl3.config(font=_font(9))
        place_window()
        _persist_dock_state()

    def place_window() -> None:
        root.update_idletasks()
        w = max(420, min(1200, root.winfo_reqwidth() + 28))
        h = root.winfo_reqheight() + 4
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        if saved_x is not None and saved_y is not None:
            x = max(0, min(int(saved_x), sw - 80))
            y = max(0, min(int(saved_y), sh - 40))
        else:
            rect = _taskbar_bottom_rect()
            if rect:
                top = rect[1]
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
                log.exception("Failed to stop tray icon during dock close")
        history.render_final()
        try:
            root.destroy()
        except tk.TclError:
            pass

    def _open_hist_folder(p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        _open_path(p.parent)

    def _reset_position() -> None:
        nonlocal saved_x, saved_y
        saved_x = None
        saved_y = None
        place_window()
        _persist_dock_state()

    def _toggle_pin() -> None:
        nonlocal pinned
        pinned = not pinned
        _apply_pin_state()
        _persist_dock_state()

    def _open_live_chart() -> None:
        try:
            from live_chart import open_live_chart
            from metric_history import DEFAULT_REGULAR_DIR

            open_live_chart(DEFAULT_REGULAR_DIR, parent=root)
        except Exception:
            log.exception("Failed to open live chart")

    def _open_spikes_folder() -> None:
        from metric_history import DEFAULT_SPIKES_DIR
        DEFAULT_SPIKES_DIR.mkdir(parents=True, exist_ok=True)
        _open_path(DEFAULT_SPIKES_DIR)

    def _open_alerts_panel() -> None:
        try:
            from live_chart import _read_today_spikes, _show_alerts_panel
            from metric_history import DEFAULT_SPIKES_DIR
            events = _read_today_spikes(DEFAULT_SPIKES_DIR)
            _show_alerts_panel(root, events)
        except Exception:
            log.exception("Failed to open alerts panel from dock")

    def menu_popup(event: tk.Event) -> None:
        m = tk.Menu(root, tearoff=0)
        m.add_command(label="פתח גרף חי", command=_open_live_chart)
        m.add_command(label="🔔 זיהוי עומס / התראות", command=_open_alerts_panel)
        m.add_command(
            label="תיעודים רגילים",
            command=lambda: _open_hist_folder(csv_path),
        )
        m.add_command(label="תיעודי חריגות", command=_open_spikes_folder)
        m.add_separator()
        m.add_command(
            label=("📌 בטל נעיצה (כעת נעוץ)" if pinned else "📌 נעץ למעלה"),
            command=_toggle_pin,
        )
        m.add_command(label="הגדל גופן", command=lambda: _bump_font(+0.1))
        m.add_command(label="הקטן גופן", command=lambda: _bump_font(-0.1))
        m.add_command(label="אפס מיקום", command=_reset_position)
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
        vram = read_gpu_memory_mib()
        if vram is not None:
            used_mib, total_mib = vram
            if total_mib >= 1024:
                temp_parts.append(f"VRAM {used_mib/1024:.1f}/{total_mib/1024:.1f} GiB")
            else:
                temp_parts.append(f"VRAM {used_mib}/{total_mib} MiB")
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
                log.exception("Failed to update tray tooltip from dock")
        place_counter += 1
        # Re-place only during initial settle, AND only if user hasn't moved it
        if place_counter <= 3 and saved_x is None and saved_y is None:
            place_window()
        # Re-assert always-on-top so taskbar/other apps can't cover the dock when pinned
        if pinned:
            try:
                root.attributes("-topmost", True)
                root.lift()
            except tk.TclError:
                pass
        root.after(interval_ms, tick)

    place_window()
    root.after(100, tick)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
