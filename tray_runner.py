"""
Windows / Linux / macOS system tray (notification area next to the clock).

Run via: python monitor.py --tray
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import psutil
import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem

from monitor import HistoryLogger, Snapshot, collect_snapshot, disk_root_path, spike_reports_enabled


def _tray_tooltip(s: Snapshot) -> str:
    parts = [f"CPU {s.cpu_percent:.0f}%", f"RAM {s.ram_percent:.0f}%"]
    if s.disk_percent is not None:
        parts.append(f"Disk {s.disk_percent:.0f}%")
    if s.temp_celsius is not None:
        parts.append(f"{s.temp_celsius:.0f}°C")
    else:
        parts.append("Temp —")
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
        if png_path.exists():
            _open_path(png_path)
        elif csv_path.parent.exists():
            _open_path(csv_path.parent)

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
        MenuItem("פתח גרף", on_open_chart, default=True),
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
    disk_path = disk_root_path()
    history = HistoryLogger(
        enabled=bool(args.history),
        csv_path=csv_path,
        png_path=png_path,
        plot_every=int(args.plot_every),
        spike_reports=spike_reports_enabled(args),
    )
    icon = build_tray_icon(args, stop, history, csv_path, png_path, on_quit_render_final=True)

    def worker() -> None:
        psutil.cpu_percent(interval=0.1)
        while not stop.is_set():
            snap = collect_snapshot(disk_path)
            history.log(snap)
            try:
                icon.title = _tray_tooltip(snap)
            except Exception:
                pass
            if stop.wait(tray_interval):
                break

    threading.Thread(target=worker, name="tray-metrics", daemon=True).start()
    icon.run()
