"""
Live terminal system monitor using psutil.

CPU usage uses a non-blocking delta between loop iterations; the first
sample is primed at startup so the first screen already shows a real value.

Optional CSV history and PNG charts live under ./history/ next to this file
(--history). Temperature is shown when sensors or Windows WMI expose it.
Use --tray for the notification area, or --dock for a centered strip above the taskbar.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

import config as app_config
from dependencies import check_features, enabled_features_from_args, format_missing
from metric_history import (
    DEFAULT_CHART_PATH,
    DEFAULT_CSV_PATH,
    DEFAULT_HISTORY_DIR,
    DEFAULT_REGULAR_DIR,
    DEFAULT_SPIKES_DIR,
    append_metrics_row,
    prune_old_archives,
    render_history_chart,
    rotate_history,
    start_daily_rotation_timer,
)
from temperature_readings import read_gpu_temp_celsius, read_primary_temp_celsius

log = logging.getLogger(__name__)


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
    gpu_t = read_gpu_temp_celsius()
    temp_bits: list[str] = []
    if s.temp_celsius is not None:
        temp_bits.append(f"מחשב {s.temp_celsius:.0f}°C")
    if gpu_t is not None:
        temp_bits.append(f"GPU {gpu_t:.0f}°C")
    temp_suffix = ("  (" + " · ".join(temp_bits) + ")") if temp_bits else ""
    print(f"{'CPU':<{label_w}}: {s.cpu_percent:5.1f}%  {ascii_bar(s.cpu_percent)}{temp_suffix}")
    if not temp_bits:
        print(f"{'Temperature':<{label_w}}: (n/a — psutil / WMI / nvidia-smi)")
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


class HistoryLogger:
    """Append CSV rows and refresh the chart; shared by console and tray modes."""

    def __init__(
        self,
        enabled: bool,
        csv_path: Path,
        png_path: Path,
        plot_every: int,
        *,
        spike_reports: bool = False,
        spike_report_path: Path | None = None,
        alert_callback=None,
    ) -> None:
        from metric_history import DEFAULT_SPIKES_DIR

        self.enabled = enabled
        self.csv_path = csv_path
        self.png_path = png_path
        self.plot_every = plot_every
        self.rows_logged = 0
        self.spike_reports = spike_reports
        # Now points at the spikes/ directory — spike_reporter writes a per-day file there
        self.spike_report_path = spike_report_path or DEFAULT_SPIKES_DIR
        self._last_snap: Snapshot | None = None
        self.alert_callback = alert_callback  # callable(prev, curr, reason, trigger) | None

    def log(self, snap: Snapshot) -> None:
        if self.spike_reports:
            if self._last_snap is not None:
                try:
                    from spike_reporter import detect_spike, maybe_append_spike_report

                    maybe_append_spike_report(self._last_snap, snap, self.spike_report_path)
                    is_spike, reason, trigger = detect_spike(self._last_snap, snap)
                    if is_spike and self.alert_callback is not None:
                        try:
                            self.alert_callback(self._last_snap, snap, reason, trigger)
                        except Exception:
                            log.exception("Alert callback failed")
                except Exception:
                    log.exception("Failed to append spike report to %s", self.spike_report_path)
            self._last_snap = snap

        if not self.enabled:
            return
        now = time.time()
        append_metrics_row(
            self.csv_path,
            unix_time=now,
            cpu_percent=snap.cpu_percent,
            ram_percent=snap.ram_percent,
            disk_percent=snap.disk_percent,
            swap_percent=snap.swap_percent,
            temp_celsius=snap.temp_celsius,
        )
        self.rows_logged += 1
        # Static PNG rendering removed — use the live chart (right-click dock → "פתח גרף חי").
        # If you still want a one-off PNG, run `python plot_history.py` manually.

    def render_final(self) -> None:
        # Static PNG rendering removed — kept as no-op for API compatibility.
        return


def spike_reports_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "no_spike_reports", False):
        return False
    if getattr(args, "spike_reports", False):
        return True
    return bool(args.history or args.dock or args.tray)


def build_parser(cfg: dict) -> argparse.ArgumentParser:
    """Build CLI parser whose defaults are pulled from `cfg` (config.json)."""
    ui = cfg.get("ui", {})
    spike = cfg.get("spike", {})
    rotation = cfg.get("rotation", {})
    alerts_cfg = cfg.get("alerts", {})

    p = argparse.ArgumentParser(
        description="Live CPU, RAM, disk, swap, uptime, temperature; history; tray or dock strip.",
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
        action=argparse.BooleanOptionalAction,
        default=bool(ui.get("history", False)),
        help="Append each sample to CSV under ./history/ (next to this script).",
    )
    p.add_argument(
        "--no-spike-reports",
        dest="no_spike_reports",
        action="store_true",
        default=not bool(spike.get("enabled", True)),
        help="Do not append spike hints to history/spike_reports.md.",
    )
    p.add_argument(
        "--spike-reports",
        action="store_true",
        help="Always write spike_reports.md in console mode (otherwise spikes run with --history / --dock / --tray).",
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
    p.add_argument(
        "--tray",
        action=argparse.BooleanOptionalAction,
        default=bool(ui.get("tray", False)),
        help="Run in the system tray (next to the clock) instead of the terminal loop.",
    )
    p.add_argument(
        "--dock",
        action=argparse.BooleanOptionalAction,
        default=bool(ui.get("dock", False)),
        help="Floating metrics bar centered just above the taskbar (with --tray: same sampler, tray is visual only).",
    )
    p.add_argument(
        "--tray-interval",
        type=float,
        default=5.0,
        metavar="SEC",
        help="Seconds between tray tooltip updates (default: 5).",
    )
    p.add_argument(
        "--disk",
        dest="disk_path",
        default=cfg.get("disk_path"),
        metavar="PATH",
        help="Drive/path to monitor (e.g. 'E:\\\\' or '/mnt/data'). Defaults to system drive.",
    )
    p.add_argument(
        "--log-level",
        default=str(cfg.get("log_level", "WARNING")).upper(),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (also written to history/monitor.log).",
    )
    p.add_argument(
        "--no-rotation",
        dest="rotation_enabled",
        action="store_false",
        default=bool(rotation.get("enabled", True)),
        help="Disable automatic CSV rotation/archive at startup.",
    )
    p.add_argument(
        "--weeks-to-keep",
        type=int,
        default=int(rotation.get("weeks_to_keep", 12)),
        metavar="N",
        help="How many weekly archive files to keep (default: 12).",
    )
    p.add_argument(
        "--alerts",
        action=argparse.BooleanOptionalAction,
        default=bool(alerts_cfg.get("enabled", True)),
        help="Show a pop-up window when a CPU/RAM spike is detected.",
    )
    p.add_argument(
        "--alert-cooldown",
        type=float,
        default=float(alerts_cfg.get("cooldown_seconds", 300)),
        metavar="SEC",
        help="Minimum seconds between alert pop-ups (default: 300).",
    )
    p.add_argument(
        "--janitor",
        action=argparse.BooleanOptionalAction,
        default=bool(cfg.get("janitor", {}).get("enabled", True)),
        help="Run the Health Janitor background scanner (conhost zombies).",
    )
    p.add_argument(
        "--save-config",
        action="store_true",
        help="Save the effective configuration back to config.json and continue.",
    )
    return p


def args_to_config(args: argparse.Namespace) -> dict:
    """Convert effective argparse namespace back to a config-shaped dict."""
    return {
        "ui": {
            "dock": bool(args.dock),
            "tray": bool(args.tray),
            "history": bool(args.history),
            "console": not (args.dock or args.tray),
        },
        "disk_path": args.disk_path,
        "history_dir": str(DEFAULT_HISTORY_DIR.name),
        "spike": {
            "cpu_threshold": 12,
            "ram_threshold": 6,
            "enabled": not bool(args.no_spike_reports),
        },
        "rotation": {
            "enabled": bool(args.rotation_enabled),
            "weeks_to_keep": int(args.weeks_to_keep),
        },
        "log_level": args.log_level,
        "alerts": {
            "enabled": bool(args.alerts),
            "cooldown_seconds": float(args.alert_cooldown),
            "top_n": 5,
            "confirm_kill": True,
        },
    }


def _migrate_history_layout() -> None:
    """One-time move from flat history/ layout to history/regular + history/spikes.

    Idempotent — safe to call on every startup.
    """
    try:
        DEFAULT_REGULAR_DIR.mkdir(parents=True, exist_ok=True)
        DEFAULT_SPIKES_DIR.mkdir(parents=True, exist_ok=True)

        # Move old metrics.csv and metrics-YYYY-WW.csv archives into regular/
        old_csv = DEFAULT_HISTORY_DIR / "metrics.csv"
        if old_csv.exists() and not (DEFAULT_REGULAR_DIR / "metrics.csv").exists():
            old_csv.rename(DEFAULT_REGULAR_DIR / "metrics.csv")
            log.info("Migrated metrics.csv → regular/metrics.csv")

        for archive in DEFAULT_HISTORY_DIR.glob("metrics-*.csv"):
            target = DEFAULT_REGULAR_DIR / archive.name
            if not target.exists():
                archive.rename(target)
                log.info("Migrated %s → regular/", archive.name)

        # Delete obsolete static chart PNGs
        for png in (DEFAULT_HISTORY_DIR / "metrics_chart.png",
                    DEFAULT_REGULAR_DIR / "metrics_chart.png"):
            if png.exists():
                try:
                    png.unlink()
                    log.info("Removed obsolete static chart: %s", png)
                except OSError:
                    log.exception("Could not delete %s", png)

        # Old single-file spike report → move into spikes/ as a legacy file
        old_spike = DEFAULT_HISTORY_DIR / "spike_reports.md"
        if old_spike.exists():
            target = DEFAULT_SPIKES_DIR / "spikes-legacy.md"
            if not target.exists():
                old_spike.rename(target)
                log.info("Migrated spike_reports.md → spikes/spikes-legacy.md")
    except Exception:
        log.exception("History layout migration failed; continuing")


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.WARNING)
    DEFAULT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    log_file = DEFAULT_HISTORY_DIR / "monitor.log"
    handlers: list[logging.Handler] = []
    try:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError:
        # If the log file can't be opened (locked / permission), keep stderr only.
        pass
    handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def main() -> None:
    cfg = app_config.load_config()
    parser = build_parser(cfg)
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive.")
    if args.plot_every < 0:
        parser.error("--plot-every must be >= 0.")
    if args.tray and args.once:
        parser.error("Cannot combine --tray with --once.")
    if args.dock and args.once:
        parser.error("Cannot combine --dock with --once.")
    if args.weeks_to_keep < 1:
        parser.error("--weeks-to-keep must be >= 1.")

    _setup_logging(args.log_level)

    missing = check_features(enabled_features_from_args(args))
    if missing:
        message = format_missing(missing)
        log.error("%s", message)
        sys.stderr.write(message + "\n")
        sys.exit(2)

    effective = args_to_config(args)

    if args.save_config:
        try:
            app_config.save_config(effective)
            log.info("Saved effective configuration to config.json")
        except OSError:
            log.exception("Could not save config.json")

    if args.disk_path:
        disk_path = args.disk_path
    else:
        disk_path = disk_root_path()

    # Janitor — background scanner for conhost zombies (start eagerly so the
    # dock indicator can show counts on first tick).
    janitor_cfg = effective.get("janitor", {})
    if janitor_cfg.get("enabled", True) and getattr(args, "janitor", True):
        try:
            from janitor import get_default_janitor

            get_default_janitor(
                scan_interval_seconds=float(janitor_cfg.get("scan_interval_minutes", 5)) * 60,
                conhost_threshold=int(janitor_cfg.get("conhost_threshold_per_parent", 20)),
            )
        except Exception:
            log.exception("Could not start Janitor scanner")

    # Alert dispatcher (shared across modes; gates by cooldown)
    args._alert_dispatcher = None
    if effective["alerts"]["enabled"]:
        from alerts import AlertDispatcher

        args._alert_dispatcher = AlertDispatcher(
            cooldown_seconds=effective["alerts"]["cooldown_seconds"]
        )
        # Eagerly start the process sampler so the first alert has data ready.
        try:
            from process_monitor import get_default_sampler

            get_default_sampler()
        except Exception:
            log.exception("Could not start process sampler")

    # One-time migration: move old-layout files into regular/ and spikes/
    _migrate_history_layout()

    if effective["rotation"]["enabled"]:
        try:
            rotate_history(DEFAULT_REGULAR_DIR, weeks_to_keep=args.weeks_to_keep)
            prune_old_archives(DEFAULT_REGULAR_DIR, weeks_to_keep=args.weeks_to_keep)
        except Exception:
            log.exception("CSV rotation failed; continuing without it")
        # Long-running modes also schedule a daily timer.
        if args.dock or args.tray or not args.once:
            start_daily_rotation_timer(DEFAULT_REGULAR_DIR, weeks_to_keep=args.weeks_to_keep)

    if args.dock:
        psutil.cpu_percent(interval=0.1)
        from dock_strip import run_dock_main

        run_dock_main(args)
        return

    if args.tray:
        if args.tray_interval <= 0:
            parser.error("--tray-interval must be positive.")
        psutil.cpu_percent(interval=0.1)
        from tray_runner import run_tray_main

        run_tray_main(args)
        return

    psutil.cpu_percent(interval=0.1)

    csv_path = Path(args.history_csv) if args.history_csv is not None else DEFAULT_CSV_PATH
    png_path = Path(args.chart_png) if args.chart_png is not None else DEFAULT_CHART_PATH

    alert_cb = None
    if args._alert_dispatcher is not None and spike_reports_enabled(args):
        from alerts import make_alert_callback

        alert_cb = make_alert_callback(args._alert_dispatcher, root_provider=lambda: None)

    history = HistoryLogger(
        enabled=bool(args.history),
        csv_path=csv_path,
        png_path=png_path,
        plot_every=int(args.plot_every),
        spike_reports=spike_reports_enabled(args),
        alert_callback=alert_cb,
    )

    if args.once:
        snap = collect_snapshot(disk_path)
        render_snapshot(snap, no_clear=args.no_clear)
        history.log(snap)
        history.render_final()
        return

    first_tick = True
    try:
        while True:
            snap = collect_snapshot(disk_path)
            if args.no_clear and not first_tick:
                print()
            render_snapshot(snap, no_clear=args.no_clear)
            history.log(snap)
            first_tick = False
            time.sleep(args.interval)
    except KeyboardInterrupt:
        sys.stdout.write("\nMonitoring stopped.\n")
    finally:
        history.render_final()


if __name__ == "__main__":
    main()
