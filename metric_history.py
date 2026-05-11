"""
Append metrics to CSV under ./history/ and render a PNG chart for analysis.
"""

from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_HISTORY_DIR = PROJECT_DIR / "history"
DEFAULT_REGULAR_DIR = DEFAULT_HISTORY_DIR / "regular"
DEFAULT_SPIKES_DIR = DEFAULT_HISTORY_DIR / "spikes"
DEFAULT_CSV_PATH = DEFAULT_REGULAR_DIR / "metrics.csv"
DEFAULT_CHART_PATH = DEFAULT_REGULAR_DIR / "metrics_chart.png"

ARCHIVE_PREFIX = "metrics-"
ARCHIVE_SUFFIX = ".csv"
RETENTION_DAYS = 7
SECONDS_PER_DAY = 86_400

CSV_FIELDNAMES = (
    "timestamp_iso",
    "unix_time",
    "cpu_percent",
    "ram_percent",
    "disk_percent",
    "swap_percent",
    "temp_celsius",
)


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def append_metrics_row(
    csv_path: Path,
    *,
    unix_time: float,
    cpu_percent: float,
    ram_percent: float,
    disk_percent: float | None,
    swap_percent: float | None,
    temp_celsius: float | None,
) -> None:
    ensure_parent_dir(csv_path)
    file_exists = csv_path.exists() and csv_path.stat().st_size > 0
    row = {
        "timestamp_iso": datetime.fromtimestamp(unix_time).isoformat(timespec="seconds"),
        "unix_time": f"{unix_time:.3f}",
        "cpu_percent": f"{cpu_percent:.2f}",
        "ram_percent": f"{ram_percent:.2f}",
        "disk_percent": "" if disk_percent is None else f"{disk_percent:.2f}",
        "swap_percent": "" if swap_percent is None else f"{swap_percent:.2f}",
        "temp_celsius": "" if temp_celsius is None else f"{temp_celsius:.1f}",
    }
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if not file_exists:
            w.writeheader()
        w.writerow(row)


def render_history_chart(
    csv_path: Path,
    png_path: Path,
    *,
    max_rows: int = 10_000,
) -> bool:
    """Read CSV and write a multi-series chart (percent + temperature). Returns True if PNG written."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return False

    times: list[datetime] = []
    cpu: list[float] = []
    ram: list[float] = []
    disk: list[float | None] = []
    swap: list[float | None] = []
    temps: list[float | None] = []

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)[-max_rows:]

    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp_iso"])
        except (KeyError, ValueError):
            continue
        try:
            c = float(r["cpu_percent"])
            rm = float(r["ram_percent"])
        except (KeyError, ValueError):
            continue
        times.append(ts)
        cpu.append(c)
        ram.append(rm)
        d_raw = (r.get("disk_percent") or "").strip()
        try:
            disk.append(float(d_raw) if d_raw else None)
        except ValueError:
            disk.append(None)
        s_raw = (r.get("swap_percent") or "").strip()
        try:
            swap.append(float(s_raw) if s_raw else None)
        except ValueError:
            swap.append(None)
        t_raw = (r.get("temp_celsius") or "").strip()
        try:
            temps.append(float(t_raw) if t_raw else None)
        except ValueError:
            temps.append(None)

    if len(times) < 2:
        return False

    ensure_parent_dir(png_path)
    fig, ax1 = plt.subplots(figsize=(11, 5), dpi=120)
    ax1.plot(times, cpu, label="CPU %", color="#1f77b4", linewidth=1.2)
    ax1.plot(times, ram, label="RAM %", color="#ff7f0e", linewidth=1.2)
    if any(v is not None for v in disk):
        ax1.plot(
            times,
            [v if v is not None else float("nan") for v in disk],
            label="Disk %",
            color="#2ca02c",
            linewidth=1.0,
        )
    if any(v is not None for v in swap):
        ax1.plot(
            times,
            [v if v is not None else float("nan") for v in swap],
            label="Swap %",
            color="#9467bd",
            linewidth=1.0,
        )
    ax1.set_ylabel("Percent")
    ax1.set_ylim(0, 105)
    ax1.grid(True, alpha=0.25)
    ax1.legend(loc="upper left", fontsize=8)

    if any(v is not None for v in temps):
        ax2 = ax1.twinx()
        ax2.plot(
            times,
            [v if v is not None else float("nan") for v in temps],
            label="Temp °C",
            color="#d62728",
            linewidth=1.2,
            alpha=0.9,
        )
        ax2.set_ylabel("Temperature °C", color="#d62728")
        ax2.tick_params(axis="y", labelcolor="#d62728")
        ax2.legend(loc="upper right", fontsize=8)

    ax1.set_title("System metrics history")
    ax1.set_xlabel("Time")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(png_path)
    plt.close(fig)
    return True


def _row_unix_time(row: dict) -> float:
    """Return unix_time for sorting; rows without a parseable timestamp sort first."""
    raw = (row.get("unix_time") or "").strip()
    try:
        return float(raw)
    except ValueError:
        return float("-inf")


def iter_history_rows(history_dir: str | Path, *, include_archive: bool = False):
    """Yield CSV rows from the main metrics file plus (optionally) all archive files,
    sorted chronologically by unix_time.

    For include_archive=False this behaves like reading the main CSV directly.
    """
    history_dir = Path(history_dir)
    main = history_dir / "metrics.csv"
    sources: list[Path] = []
    if main.exists():
        sources.append(main)
    if include_archive:
        sources.extend(sorted(history_dir.glob(f"{ARCHIVE_PREFIX}*{ARCHIVE_SUFFIX}")))

    rows: list[dict] = []
    for src in sources:
        try:
            with src.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                rows.extend(reader)
        except OSError:
            log.exception("Failed to read history source %s", src)

    rows.sort(key=_row_unix_time)
    return rows


def render_combined_chart(history_dir: str | Path, png_path: Path,
                          *, include_archive: bool = True,
                          max_rows: int = 200_000) -> bool:
    """Render a chart spanning main CSV + archives. Returns True if PNG written."""
    import tempfile

    history_dir = Path(history_dir)
    rows = iter_history_rows(history_dir, include_archive=include_archive)
    if not rows:
        return False
    rows = rows[-max_rows:]

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="",
                                     suffix=".csv", delete=False) as tmp:
        writer = csv.DictWriter(tmp, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in CSV_FIELDNAMES})
        tmp_path = Path(tmp.name)

    try:
        return render_history_chart(tmp_path, Path(png_path), max_rows=max_rows)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            log.debug("Could not delete temp combined CSV %s", tmp_path)


def _archive_path_for_unix(history_dir: Path, unix_time: float) -> Path:
    """Return weekly-archive path for a given unix timestamp (UTC ISO week)."""
    dt = datetime.fromtimestamp(unix_time, tz=timezone.utc)
    iso_year, iso_week, _ = dt.isocalendar()
    name = f"{ARCHIVE_PREFIX}{iso_year:04d}-{iso_week:02d}{ARCHIVE_SUFFIX}"
    return history_dir / name


def rotate_history(
    history_dir: str | Path,
    weeks_to_keep: int = 12,
    *,
    csv_name: str = "metrics.csv",
    retention_days: int = RETENTION_DAYS,
) -> dict:
    """Move rows older than ``retention_days`` from the main CSV into weekly archives.

    Returns a stats dict (moved/kept/archives_written/skipped_unparseable). Leaves
    the main CSV untouched if any archive write fails.
    """
    history_dir = Path(history_dir)
    main = history_dir / csv_name
    stats = {"moved": 0, "kept": 0, "archives_written": 0, "skipped_unparseable": 0}

    if not main.exists() or main.stat().st_size == 0:
        log.debug("No %s yet; nothing to rotate", main)
        return stats

    import time as _time

    cutoff = _time.time() - retention_days * SECONDS_PER_DAY

    with main.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            log.warning("%s has no header; skipping rotation", main)
            return stats
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    if not rows:
        return stats

    def _row_unix(row: dict) -> float | None:
        raw = (row.get("unix_time") or "").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    first_unix = _row_unix(rows[0])
    if first_unix is not None and first_unix >= cutoff:
        log.debug("Oldest row newer than cutoff; rotation not needed")
        return stats

    keep_rows: list[dict] = []
    archive_buckets: dict[Path, list[dict]] = {}

    for row in rows:
        ts = _row_unix(row)
        if ts is None:
            stats["skipped_unparseable"] += 1
            keep_rows.append(row)
            continue
        if ts >= cutoff:
            keep_rows.append(row)
            stats["kept"] += 1
        else:
            archive_buckets.setdefault(_archive_path_for_unix(history_dir, ts), []).append(row)
            stats["moved"] += 1

    if not archive_buckets:
        return stats

    history_dir.mkdir(parents=True, exist_ok=True)
    for archive, bucket in archive_buckets.items():
        is_new = not archive.exists() or archive.stat().st_size == 0
        try:
            with archive.open("a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if is_new:
                    writer.writeheader()
                    stats["archives_written"] += 1
                writer.writerows(bucket)
        except OSError:
            log.exception("Failed to write archive %s; aborting rotation", archive)
            return stats

    tmp = main.with_suffix(main.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(keep_rows)
        tmp.replace(main)
    except OSError:
        log.exception("Failed to rewrite %s", main)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                log.exception("Could not clean up temp file %s", tmp)
        return stats

    log.info(
        "Rotation done: moved=%d kept=%d archives_written=%d unparseable=%d",
        stats["moved"], stats["kept"], stats["archives_written"], stats["skipped_unparseable"],
    )

    prune_old_archives(history_dir, weeks_to_keep)
    return stats


def prune_old_archives(history_dir: str | Path, weeks_to_keep: int = 12) -> int:
    """Delete metrics-YYYY-WW.csv archives older than `weeks_to_keep`. Returns count deleted."""
    history_dir = Path(history_dir)
    if not history_dir.exists():
        return 0

    now = datetime.now(tz=timezone.utc)
    deleted = 0

    for archive in history_dir.glob(f"{ARCHIVE_PREFIX}*{ARCHIVE_SUFFIX}"):
        stem = archive.stem  # metrics-YYYY-WW
        parts = stem.split("-")
        if len(parts) != 3:
            log.debug("Skipping unrecognized archive name: %s", archive.name)
            continue
        try:
            year = int(parts[1])
            week = int(parts[2])
        except ValueError:
            log.debug("Non-numeric ISO week in %s", archive.name)
            continue

        try:
            archive_date = datetime.fromisocalendar(year, week, 1).replace(tzinfo=timezone.utc)
        except ValueError:
            log.debug("Invalid ISO year/week in %s", archive.name)
            continue

        age_weeks = (now - archive_date).days / 7
        # ">=" so an archive that is exactly N weeks old is deleted when
        # weeks_to_keep=N. Without the equality, the boundary archive lingers
        # forever (its age never exceeds N by any meaningful margin).
        if age_weeks >= weeks_to_keep:
            try:
                archive.unlink()
                deleted += 1
                log.info("Pruned old archive: %s", archive.name)
            except OSError:
                log.warning("Failed to delete %s", archive, exc_info=True)

    return deleted


def start_daily_rotation_timer(
    history_dir: str | Path,
    weeks_to_keep: int = 12,
    *,
    interval_seconds: float = SECONDS_PER_DAY,
) -> threading.Timer:
    """Schedule rotate_history to run once every `interval_seconds` (default: 24h).

    Returns the underlying daemon Timer so callers can cancel it on shutdown.
    """
    history_dir = Path(history_dir)

    def _tick() -> None:
        try:
            rotate_history(history_dir, weeks_to_keep=weeks_to_keep)
        except Exception:
            log.exception("Scheduled rotation failed")
        finally:
            t = threading.Timer(interval_seconds, _tick)
            t.daemon = True
            t.start()
            # Replace the reference so callers can cancel the latest one if they kept it.
            start_daily_rotation_timer._current = t  # type: ignore[attr-defined]

    timer = threading.Timer(interval_seconds, _tick)
    timer.daemon = True
    timer.start()
    start_daily_rotation_timer._current = timer  # type: ignore[attr-defined]
    return timer
