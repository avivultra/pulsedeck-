"""
Append metrics to CSV under ./history/ and render a PNG chart for analysis.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_HISTORY_DIR = PROJECT_DIR / "history"
DEFAULT_CSV_PATH = DEFAULT_HISTORY_DIR / "metrics.csv"
DEFAULT_CHART_PATH = DEFAULT_HISTORY_DIR / "metrics_chart.png"

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
    span_sec = (times[-1] - times[0]).total_seconds()
    if span_sec < 7200:
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    else:
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.2)
    last_t = temps[-1] if temps else None
    t_txt = "—" if last_t is None else f"{last_t:.1f}°C"
    d_last = disk[-1] if disk else None
    d_txt = "—" if d_last is None else f"{d_last:.1f}%"
    summary = (
        f"Last: {times[-1].strftime('%Y-%m-%d %H:%M:%S')}  |  "
        f"CPU {cpu[-1]:.1f}%  RAM {ram[-1]:.1f}%  Disk {d_txt}  Temp {t_txt}"
    )
    fig.text(0.02, 0.02, summary, fontsize=8, color="#333333", transform=fig.transFigure)
    fig.savefig(png_path)
    plt.close(fig)
    return True
