"""
Live terminal system monitor using psutil.

CPU usage uses a non-blocking delta between loop iterations; the first
sample is primed at startup so the first screen already shows a real value.

Optional CSV history and PNG charts live under ./history/ next to this file
(--history). Temperature is shown when sensors or Windows WMI expose it.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from metric_history import (
    DEFAULT_CHART_PATH,
    DEFAULT_CSV_PATH,
    append_metrics_row,
    render_history_chart,
)
from temperature_readings import read_primary_temp_celsius


@dataclass(frozen=True)
class Snapshot:
    cpu_percent: float
    ram_percent: float
    ram_used: int
    ram_total: int
    disk_path: str
    disk_percent: float | None
    disk_used: int | None
    disk_total: int | None
    swap_percent: float | None
    uptime_sec: float
    battery_percent: float | None
    battery_plugged: bool | None
    cpu_logical: int
    temp_celsius: float | None


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def disk_root_path() -> str:
    if os.name == "nt":
        drive = os.environ.get("SystemDrive", "C:")
        if not drive.endswith(("\\", "/")):
            return drive + "\\"
        return drive
    return "/"


def bytes_to_gib(n: int) -> float:
    return n / (1024**3)


def format_gib_usage(used: int, total: int) -> str:
    return f"{bytes_to_gib(used):.1f} / {bytes_to_gib(total):.1f} GiB"


def format_uptime(seconds: float) -> str:
    """Compact uptime: largest non-zero units, no redundant zeros (e.g. 1h not 1h 0m 0s)."""
    if seconds < 0:
        return "?"
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, secs = divmod(s, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def ascii_bar(percent: float, width: int = 14) -> str:
    """Horizontal bar for 0..100 percent (clamped)."""
    p = max(0.0, min(100.0, percent))
    filled = int(round((p / 100.0) * width))
    filled = min(width, max(0, filled))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _swap_percent() -> float | None:
    try:
        sw = psutil.swap_memory()
    except (OSError, RuntimeError):
        # Windows: PDH / performance counters may be disabled or unavailable.
        return None
    if sw.total == 0:
        return None
    return sw.percent


def _battery_fields() -> tuple[float | None, bool | None]:
    try:
        batt = psutil.sensors_battery()
    except Exception:
        return None, None
    if batt is None:
        return None, None
    return float(batt.percent), bool(batt.power_plugged)


def collect_snapshot(disk_path: str) -> Snapshot:
    cpu_usage = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    disk_percent: float | None = None
    disk_used: int | None = None
    disk_total: int | None = None
    try:
        disk = psutil.disk_usage(disk_path)
        if disk.total > 0:
            disk_percent = 100.0 * disk.used / disk.total
            disk_used = disk.used
            disk_total = disk.total
    except OSError:
        pass

    uptime_sec = max(0.0, time.time() - psutil.boot_time())
    batt_pct, batt_plug = _battery_fields()
    temp_c = read_primary_temp_celsius()

    return Snapshot(
        cpu_percent=cpu_usage,
        ram_percent=ram.percent,
        ram_used=ram.used,
        ram_total=ram.total,
        disk_path=disk_path,
        disk_percent=disk_percent,
        disk_used=disk_used,
        disk_total=disk_total,
        swap_percent=_swap_percent(),
        uptime_sec=uptime_sec,
        battery_percent=batt_pct,
        battery_plugged=batt_plug,
        cpu_logical=psutil.cpu_count(logical=True) or 0,
        temp_celsius=temp_c,
    )


def render_snapshot(s: Snapshot, *, no_clear: bool) -> None:
    if not no_clear:
        clear_screen()

    width = 42
    label_w = 15
    print("System Monitor")
    print("-" * width)
    print(f"{'CPUs (logical)':<{label_w}}: {s.cpu_logical}")
    print(f"{'CPU':<{label_w}}: {s.cpu_percent:5.1f}%  {ascii_bar(s.cpu_percent)}")
    if s.temp_celsius is not None:
        print(f"{'Temperature':<{label_w}}: {s.temp_celsius:5.1f} °C")
    else:
        print(f"{'Temperature':<{label_w}}: (n/a)")
    print(
        f"{'RAM':<{label_w}}: {s.ram_percent:5.1f}%  {ascii_bar(s.ram_percent)}  "
        f"({format_gib_usage(s.ram_used, s.ram_total)})"
    )
    if s.swap_percent is not None:
        print(f"{'Swap':<{label_w}}: {s.swap_percent:5.1f}%  {ascii_bar(s.swap_percent)}")
    if s.disk_percent is not None and s.disk_used is not None and s.disk_total is not None:
        short = s.disk_path.rstrip("\\/") or s.disk_path
        print(
            f"{'Disk (' + short + ')':<{label_w}}: {s.disk_percent:5.1f}%  {ascii_bar(s.disk_percent)}  "
            f"({format_gib_usage(s.disk_used, s.disk_total)})"
        )
    else:
        print(f"{'Disk':<{label_w}}: (unavailable) {s.disk_path}")
    print(f"{'Uptime':<{label_w}}: {format_uptime(s.uptime_sec)}")
    if s.battery_percent is not None:
        if s.battery_plugged is True:
            plug = "AC"
        elif s.battery_plugged is False:
            plug = "on battery"
        else:
            plug = "unknown"
        print(f"{'Battery':<{label_w}}: {s.battery_percent:.0f}% ({plug})")
    print("-" * width)
    if sys.stdin.isatty():
        print("Press Ctrl+C to stop.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Live CPU, RAM, disk, swap, uptime, temperature; optional CSV/PNG history.",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=1.0,
        metavar="SEC",
        help="Seconds between updates in loop mode (default: 1.0).",
    )
    p.add_argument(
        "--no-clear",
        action="store_true",
        help="Do not clear the screen each tick (scroll-friendly / logs).",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Print one snapshot and exit (good for scripts).",
    )
    p.add_argument(
        "--history",
        action="store_true",
        help="Append each sample to CSV under ./history/ (next to this script).",
    )
    p.add_argument(
        "--history-csv",
        type=Path,
        default=None,
        help=f"CSV file (default: {DEFAULT_CSV_PATH}).",
    )
    p.add_argument(
        "--chart-png",
        type=Path,
        default=None,
        help=f"Chart image (default: {DEFAULT_CHART_PATH}).",
    )
    p.add_argument(
        "--plot-every",
        type=int,
        default=30,
        metavar="N",
        help="Redraw chart every N new rows; 0 = no redraw while running (still refreshes on exit).",
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive.")
    if args.plot_every < 0:
        parser.error("--plot-every must be >= 0.")

    psutil.cpu_percent(interval=0.1)
    disk_path = disk_root_path()

    csv_path = Path(args.history_csv) if args.history_csv is not None else DEFAULT_CSV_PATH
    png_path = Path(args.chart_png) if args.chart_png is not None else DEFAULT_CHART_PATH
    history_on = args.history
    rows_logged = 0

    def log_history_row(snap: Snapshot) -> None:
        nonlocal rows_logged
        if not history_on:
            return
        now = time.time()
        append_metrics_row(
            csv_path,
            unix_time=now,
            cpu_percent=snap.cpu_percent,
            ram_percent=snap.ram_percent,
            disk_percent=snap.disk_percent,
            swap_percent=snap.swap_percent,
            temp_celsius=snap.temp_celsius,
        )
        rows_logged += 1
        if args.plot_every > 0 and rows_logged % args.plot_every == 0:
            try:
                render_history_chart(csv_path, png_path)
            except Exception:
                pass

    if args.once:
        snap = collect_snapshot(disk_path)
        render_snapshot(snap, no_clear=args.no_clear)
        log_history_row(snap)
        if history_on and rows_logged:
            try:
                render_history_chart(csv_path, png_path)
            except Exception:
                pass
        return

    first_tick = True
    try:
        while True:
            snap = collect_snapshot(disk_path)
            if args.no_clear and not first_tick:
                print()
            render_snapshot(snap, no_clear=args.no_clear)
            log_history_row(snap)
            first_tick = False
            time.sleep(args.interval)
    except KeyboardInterrupt:
        sys.stdout.write("\nMonitoring stopped.\n")
    finally:
        if history_on and rows_logged:
            try:
                render_history_chart(csv_path, png_path)
            except Exception:
                pass


if __name__ == "__main__":
    main()
