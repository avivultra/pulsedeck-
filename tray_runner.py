"""
Windows / Linux / macOS system tray (notification area next to the clock).

Run via: python monitor.py --tray
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

import psutil
import pystray

log = logging.getLogger(__name__)
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem

from monitor import HistoryLogger, Snapshot, collect_snapshot, disk_root_path, spike_reports_enabled


def _tray_tooltip(s: Snapshot) -> str:
    from temperature_readings import read_gpu_temp_celsius

    parts = [f"CPU {s.cpu_percent:.0f}%", f"RAM {s.ram_percent:.0f}%"]
    if s.disk_percent is not None:
        parts.append(f"Disk {s.disk_percent:.0f}%")
    if s.temp_celsius is not None:
        parts.append(f"Sys {s.temp_celsius:.0f}°C")
    gt = read_gpu_temp_celsius()
    if gt is not None:
        parts.append(f"GPU {gt:.0f}°C")
    if s.temp_celsius is None and gt is None:
        parts.append("temp —")
    return " · ".join(parts)


def _create_icon_image() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 6
    draw.ellipse(
        (margin, margin, size - margin, size - margin),
        fill=(36, 99, 235, 255),
        outline=(12, 40, 120, 255),
        width=2,
    )
    return img


def _open_path(path: Path) -> None:
    path = path.resolve()
    if not path.exists():
        return
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        import subprocess

        subprocess.Popen(["open", str(path)])
    else:
        import subprocess

        subprocess.Popen(["xdg-open", str(path)])


def build_tray_icon(
    args: object,
    stop: threading.Event,
    history: HistoryLogger,
    csv_path: Path,
    png_path: Path,
    *,
    on_quit_render_final: bool = True,
) -> pystray.Icon:
    def on_open_chart(_icon: pystray.Icon, _item: pystray.MenuItem | None = None) -> None:
        try:
            from live_chart import open_live_chart
            from metric_history import DEFAULT_REGULAR_DIR
            # Tray has no Tk root; live_chart will spin up its own.
            threading.Thread(
                target=lambda: open_live_chart(DEFAULT_REGULAR_DIR, parent=None),
                name="live-chart", daemon=True,
            ).start()
        except Exception:
            log.exception("Failed to open live chart from tray")

    def on_open_alerts(_icon: pystray.Icon, _item: pystray.MenuItem | None = None) -> None:
        def _run() -> None:
            try:
                import tkinter as tk
                from live_chart import _read_today_spikes, _show_alerts_panel
                from metric_history import DEFAULT_SPIKES_DIR
                # Standalone Tk root just for this panel
                root = tk.Tk()
                root.withdraw()
                events = _read_today_spikes(DEFAULT_SPIKES_DIR)
                panel = _show_alerts_panel(root, events)
                # When the panel closes, exit this thread's mainloop
                if panel is not None:
                    panel.protocol("WM_DELETE_WINDOW",
                                   lambda: (panel.destroy(), root.quit()))
                    root.mainloop()
                    try:
                        root.destroy()
                    except tk.TclError:
                        pass
            except Exception:
                log.exception("Failed to open alerts panel from tray")
        threading.Thread(target=_run, name="alerts-panel", daemon=True).start()

    def on_open_folder(_icon: pystray.Icon, _item: pystray.MenuItem | None = None) -> None:
        folder = csv_path.parent
        folder.mkdir(parents=True, exist_ok=True)
        _open_path(folder)

    def on_quit(icon: pystray.Icon, _item: pystray.MenuItem | None = None) -> None:
        stop.set()
        if on_quit_render_final:
            history.render_final()
        icon.stop()

    menu = Menu(
        MenuItem("פתח גרף חי", on_open_chart, default=True),
        MenuItem("🔔 זיהוי עומס / התראות", on_open_alerts),
        MenuItem("תיקיית היסטוריה", on_open_folder),
        Menu.SEPARATOR,
        MenuItem("יציאה", on_quit),
    )

    image = _create_icon_image()
    return pystray.Icon(
        "graph_performance_monitor",
        image,
        menu=menu,
        title="מוניטור ביצועים",
    )


def start_tray_daemon_visual(
    args: object,
    stop: threading.Event,
    history: HistoryLogger,
    csv_path: Path,
    png_path: Path,
    *,
    on_quit_render_final: bool = False,
) -> pystray.Icon:
    """Tray without a metrics worker — use when --dock owns the sampling loop."""
    icon = build_tray_icon(
        args, stop, history, csv_path, png_path, on_quit_render_final=on_quit_render_final
    )
    threading.Thread(target=icon.run, name="pystray-loop", daemon=True).start()
    return icon


def run_tray_main(args: object) -> None:
    """Tray-only mode: blocks until the user chooses Quit."""
    from metric_history import DEFAULT_CHART_PATH, DEFAULT_CSV_PATH

    stop = threading.Event()
    tray_interval = float(args.tray_interval)
    csv_path = Path(args.history_csv) if args.history_csv is not None else DEFAULT_CSV_PATH
    png_path = Path(args.chart_png) if args.chart_png is not None else DEFAULT_CHART_PATH
    disk_path = getattr(args, "disk_path", None) or disk_root_path()
    history = HistoryLogger(
        enabled=bool(args.history),
        csv_path=csv_path,
        png_path=png_path,
        plot_every=int(args.plot_every),
        spike_reports=spike_reports_enabled(args),
    )
    dispatcher = getattr(args, "_alert_dispatcher", None)
    if dispatcher is not None and spike_reports_enabled(args):
        from alerts import make_alert_callback

        history.alert_callback = make_alert_callback(dispatcher, root_provider=lambda: None)
    icon = build_tray_icon(args, stop, history, csv_path, png_path, on_quit_render_final=True)

    def worker() -> None:
        psutil.cpu_percent(interval=0.1)
        while not stop.is_set():
            snap = collect_snapshot(disk_path)
            history.log(snap)
            try:
                icon.title = _tray_tooltip(snap)
            except Exception:
                log.exception("Failed to update tray tooltip")
            if stop.wait(tray_interval):
                break

    threading.Thread(target=worker, name="tray-metrics", daemon=True).start()
    icon.run()
