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

import freeze_watch
import hardware
from i18n import tr

log = logging.getLogger(__name__)

from monitor import HistoryLogger, collect_snapshot, disk_root_path, format_gib_usage, spike_reports_enabled
from temperature_readings import read_gpu_memory_mib, read_gpu_temp_celsius


def _work_area() -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of the primary screen minus the taskbar.

    Works wherever the taskbar sits (bottom, top, left, right). With a bottom
    taskbar the bottom edge is exactly the taskbar's top edge, so the dock lands
    where it always did."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import byref, wintypes

    SPI_GETWORKAREA = 0x0030
    rect = wintypes.RECT()
    if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, byref(rect), 0):
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    return None


def _point_on_a_monitor(x: int, y: int) -> bool | None:
    """Whether (x, y) is on any connected monitor; None when unknown (non-Windows).

    A position saved on a second monitor stays valid while that monitor is
    plugged in, and is dropped (back to auto-placement) once it is unplugged."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    MONITOR_DEFAULTTONULL = 0
    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    return bool(user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONULL))


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
    root.title(tr("מוניטור ביצועים", "Performance Monitor"))
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
    # Fan / Extreme-Cooling toggle button (far left). We can't read the real
    # cooling state, so we track a believed state for the tint only.
    fan_state = {"on": False}
    lbl_fan = tk.Label(top_row, text="🌀", bg=bg, fg=dim,
                       font=_font(11), cursor="hand2")
    # Janitor indicator (left side, hidden when zombie count == 0)
    lbl_janitor = tk.Label(top_row, text="", bg=bg, fg="#ebcb8b",
                           font=_font(9, bold=True), anchor="w", cursor="hand2")
    # Ghost-sweeper indicator (hidden when nothing has been abandoned)
    lbl_ghosts = tk.Label(top_row, text="", bg=bg, fg="#a78bfa",
                          font=_font(9, bold=True), anchor="w", cursor="hand2")
    lbl_cpu = tk.Label(top_row, text="CPU …", bg=bg, fg=fg, font=_font(10), anchor="w")
    lbl_temp = tk.Label(top_row, text=tr("טמפ …", "Temp …"), bg=bg, fg=accent,
                        font=_font(9), anchor="e")
    # Order matters: fan button leftmost, then janitor (dynamic), then CPU.
    # Only on machines whose cooling utility answers the hotkey (hardware.py);
    # elsewhere Ctrl+Shift+1 would land in whatever window has focus.
    if hardware.fan_button_enabled(_cfg):
        lbl_fan.pack(side=tk.LEFT, padx=(10, 0), pady=4)
    lbl_cpu.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=16, pady=4)
    lbl_temp.pack(side=tk.RIGHT, padx=16, pady=4)
    # janitor label is packed/forgotten dynamically inside tick()
    top_row.pack(fill=tk.X)

    def _toggle_fan(_event=None):
        try:
            from fan_auto import send_extreme_cooling_toggle
            send_extreme_cooling_toggle()
            fan_state["on"] = not fan_state["on"]
            lbl_fan.config(fg=accent if fan_state["on"] else dim)
        except Exception:
            log.exception("Extreme Cooling toggle failed")

    lbl_fan.bind("<Button-1>", _toggle_fan)

    def _open_janitor_panel(_event=None):
        try:
            from janitor import open_cleanup_panel
            open_cleanup_panel(root)
        except Exception:
            log.exception("Failed to open janitor cleanup panel")

    lbl_janitor.bind("<Button-1>", _open_janitor_panel)

    def _open_ghost_panel(_event=None):
        try:
            from ghost_panel import open_ghost_panel
            open_ghost_panel(root)
        except Exception:
            log.exception("Failed to open ghost sweeper panel")

    lbl_ghosts.bind("<Button-1>", _open_ghost_panel)

    var_line2 = tk.StringVar(value=tr("טוען…", "Loading…"))
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
        lbl_fan.config(font=_font(11))
        lbl_janitor.config(font=_font(9, bold=True))
        lbl_ghosts.config(font=_font(9, bold=True))
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
        on_screen = None
        if saved_x is not None and saved_y is not None:
            # Check the dock's top-left corner and a point well inside it.
            on_screen = _point_on_a_monitor(int(saved_x) + 40, int(saved_y) + 10)
        if on_screen:
            x, y = int(saved_x), int(saved_y)
        elif on_screen is None and saved_x is not None and saved_y is not None:
            x = max(0, min(int(saved_x), sw - 80))
            y = max(0, min(int(saved_y), sh - 40))
        else:
            area = _work_area()
            if area:
                left, _top, right, bottom = area
                y = max(0, bottom - h)
                x = max(left, left + (right - left - w) // 2)
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
        freeze_watch.stop()
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

    def _install_desktop_shortcut() -> None:
        try:
            from install_shortcut import install_shortcut as _install
            from tkinter import messagebox
            path = _install()
            if path is not None:
                messagebox.showinfo(
                    tr("קיצור הותקן", "Shortcut installed"),
                    tr(f"נוצר קיצור בשולחן העבודה:\n{path}",
                       f"A desktop shortcut was created:\n{path}"),
                    parent=root,
                )
            else:
                messagebox.showwarning(
                    tr("התקנה נכשלה", "Installation failed"),
                    tr("לא הצלחתי ליצור את הקיצור. ראה history/monitor.log",
                       "Could not create the shortcut. See history/monitor.log"),
                    parent=root,
                )
        except Exception:
            log.exception("Desktop shortcut installation failed from dock menu")

    def _open_servers_panel() -> None:
        try:
            from server_scanner import open_servers_panel
            open_servers_panel(root)
        except Exception:
            log.exception("Failed to open servers panel")

    def _set_language(code: str) -> None:
        """Persist the language choice. Applied on next start: every window
        builds its text once, so a live switch would leave half the UI stale."""
        from tkinter import messagebox
        try:
            cfg = app_config.load_config()
            cfg.setdefault("ui", {})["language"] = code
            app_config.save_config(cfg)
        except Exception:
            log.exception("Could not save the language choice")
            return
        messagebox.showinfo(
            "PulseDeck",
            "השפה תתחלף בהפעלה הבאה של PulseDeck.\n\n"
            "The language will change the next time PulseDeck starts.",
            parent=root,
        )

    def menu_popup(event: tk.Event) -> None:
        m = tk.Menu(root, tearoff=0)
        m.add_command(label=tr("פתח גרף חי", "Open live chart"),
                      command=_open_live_chart)
        m.add_command(label=tr("🔔 זיהוי עומס / התראות", "🔔 Load events / alerts"),
                      command=_open_alerts_panel)
        m.add_command(label=tr("🌐 שרתים פתוחים", "🌐 Open servers"),
                      command=_open_servers_panel)
        m.add_command(label=tr("🧹 ניקוי תהליכים מיותרים", "🧹 Clean up leftover processes"),
                      command=_open_janitor_panel)
        m.add_command(label=tr("👻 תהליכים שננטשו", "👻 Abandoned processes"),
                      command=_open_ghost_panel)
        m.add_command(
            label=tr("תיעודים רגילים", "Metrics history"),
            command=lambda: _open_hist_folder(csv_path),
        )
        m.add_command(label=tr("תיעודי חריגות", "Spike reports"),
                      command=_open_spikes_folder)
        m.add_separator()
        m.add_command(
            label=(tr("📌 בטל נעיצה (כעת נעוץ)", "📌 Unpin (currently pinned)") if pinned
                   else tr("📌 נעץ למעלה", "📌 Pin on top")),
            command=_toggle_pin,
        )
        m.add_command(label=tr("הגדל גופן", "Larger font"),
                      command=lambda: _bump_font(+0.1))
        m.add_command(label=tr("הקטן גופן", "Smaller font"),
                      command=lambda: _bump_font(-0.1))
        m.add_command(label=tr("אפס מיקום", "Reset position"), command=_reset_position)
        lang_menu = tk.Menu(m, tearoff=0)
        for code, label in (("auto", tr("אוטומטי (לפי Windows)", "Automatic (follow Windows)")),
                            ("he", "עברית"), ("en", "English")):
            lang_menu.add_command(label=label, command=lambda c=code: _set_language(c))
        m.add_cascade(label="🌐 שפה / Language", menu=lang_menu)
        m.add_separator()
        m.add_command(label=tr("📌 התקן קיצור בשולחן העבודה", "📌 Install desktop shortcut"),
                      command=_install_desktop_shortcut)
        m.add_separator()
        m.add_command(label=tr("יציאה", "Exit"), command=on_close)
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
        freeze_watch.beat()
        snap = collect_snapshot(disk_path)
        history.log(snap)

        lbl_cpu.config(text=f"CPU  {snap.cpu_percent:.0f}%  ·  {snap.cpu_logical} " + tr("ליבות", "cores"))
        temp_parts: list[str] = []
        if snap.temp_celsius is not None:
            temp_parts.append(tr("מחשב", "CPU") + f" {snap.temp_celsius:.0f}°C")
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
        lbl_temp.config(text=" · ".join(temp_parts) if temp_parts else tr("טמפ —", "Temp —"))

        d_pct = "—" if snap.disk_percent is None else f"{snap.disk_percent:.0f}%"
        short = snap.disk_path.rstrip("\\/") or snap.disk_path
        if len(short) > 6:
            short = short[:5] + "…"
        var_line2.set(
            f"RAM {snap.ram_percent:.0f}%  ({format_gib_usage(snap.ram_used, snap.ram_total)})  ·  "
            + tr("דיסק", "Disk") + f" {short}: {d_pct}"
        )

        io = psutil.net_io_counters()
        now = time.time()
        net_txt = tr("רשת …", "Net …")
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
            extras.append(tr("סוויפ", "Swap") + f" {snap.swap_percent:.0f}%")
        extras.append(net_txt)
        if snap.battery_percent is not None:
            plug = tr("חשמל", "AC") if snap.battery_plugged else tr("סוללה", "Battery")
            extras.append(f"{plug} {snap.battery_percent:.0f}%")
        var_line3.set("  ·  ".join(extras))

        # Janitor indicator — show "🧹 N" only when zombies detected.
        # peek (not get): the badge must never create/start the janitor —
        # if the user ran --no-janitor, it stays off.
        try:
            from janitor import peek_default_janitor
            j = peek_default_janitor()
            n = j.count_total_zombies() if j is not None else 0
        except Exception:
            n = 0
        if n > 0:
            lbl_janitor.config(text=f"🧹 {n}")
            if not lbl_janitor.winfo_ismapped():
                lbl_janitor.pack(side=tk.LEFT, padx=(8, 0), pady=4,
                                 before=lbl_cpu)
        else:
            if lbl_janitor.winfo_ismapped():
                lbl_janitor.pack_forget()

        # Ghost-sweeper badge. Same contract as the janitor badge: peek, never
        # get — drawing the dock must not resurrect a sweeper the user turned
        # off, and this read is a lock-protected list length, never a scan.
        try:
            from ghost_sweeper import peek_default_sweeper
            sw = peek_default_sweeper()
            ghost_n = sw.count() if sw is not None else 0
        except Exception:
            ghost_n = 0
        if ghost_n > 0:
            lbl_ghosts.config(text=f"👻 {ghost_n}")
            if not lbl_ghosts.winfo_ismapped():
                lbl_ghosts.pack(side=tk.LEFT, padx=(8, 0), pady=4,
                                before=lbl_cpu)
        else:
            if lbl_ghosts.winfo_ismapped():
                lbl_ghosts.pack_forget()

        if tray_icon is not None:
            try:
                tray_icon.title = _tray_tooltip(snap)  # type: ignore[attr-defined]
            except Exception:
                log.exception("Failed to update tray tooltip from dock")
        place_counter += 1
        # Re-place only during the first two ticks to let the geometry settle.
        # After that, place_window is only called explicitly (font change, reset
        # position) — saves a layout pass every tick.
        if place_counter <= 2 and saved_x is None and saved_y is None:
            place_window()
        # Re-assert always-on-top so taskbar/other apps can't cover the dock when pinned.
        # Throttle to every 5th tick (~5s) — Windows respects topmost between
        # checks; constant re-assertion was wasted work.
        if pinned and place_counter % 5 == 0:
            try:
                root.attributes("-topmost", True)
                root.lift()
            except tk.TclError:
                pass
        root.after(interval_ms, tick)

    place_window()
    from metric_history import DEFAULT_HISTORY_DIR
    freeze_watch.start(DEFAULT_HISTORY_DIR / "freeze.log")
    root.after(100, tick)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
