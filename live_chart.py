"""Shared pieces behind PulseDeck's windows: today's spike report (parsed, plus
the alerts panel), the history data layer (float arrays, tail-only reads) and
the plain-words verdict. The main window itself lives in vault_window.py.
"""
from __future__ import annotations

import csv
import logging
import re
import tkinter as tk
from array import array
from bisect import bisect_left
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import i18n
from i18n import tr

log = logging.getLogger(__name__)


# ---------- Spike-event parsing (read today's spike markdown) ----------

_SPIKE_HEADING_RE = re.compile(r"^##\s*⚠\s*(\d{2}:\d{2}:\d{2})\s*—\s*(.+?)\s*$")
_SPIKE_CPU_RE    = re.compile(r"^- \*\*CPU:\*\*\s*([\d.]+)%\s*→\s*([\d.]+)%")
_SPIKE_RAM_RE    = re.compile(r"^- \*\*RAM:\*\*\s*([\d.]+)%\s*→\s*([\d.]+)%")
_SPIKE_SUSPECT_RE = re.compile(r"^\d+\.\s+`([^`]+)`\s+—\s+(.+?)\s*$")


def _trigger_to_color(reason: str) -> str:
    """Pick an accent color based on what the reason says fired."""
    if "RAM" in reason and "CPU" not in reason:
        return "#f97316"  # orange
    if "CPU" in reason and "RAM" not in reason:
        return "#3b82f6"  # blue
    return "#ef4444"  # red — both/other


def _show_alerts_panel(parent: tk.Misc, events: list[dict]) -> tk.Toplevel:
    """Open a Toplevel listing today's spike events. Click → full alert window.
    Returns the panel widget so callers can hook close events."""
    panel = tk.Toplevel(parent)
    panel.title(tr("התראות היום", "Today's alerts"))
    panel.geometry("520x460")
    panel.configure(bg=BG)
    try:
        panel.attributes("-topmost", True)
    except tk.TclError:
        pass

    # Header
    hdr = tk.Frame(panel, bg=PANEL, padx=14, pady=10)
    hdr.pack(fill="x")
    today_str = datetime.now().strftime("%Y-%m-%d")
    tk.Label(hdr, text=tr(f"התראות {today_str}", f"Alerts {today_str}"), bg=PANEL, fg=FG,
             font=("Segoe UI", 12, "bold"), anchor=i18n.ANCHOR()).pack(fill="x")
    tk.Label(hdr, text=tr(f"{len(events)} אירועים היום  ·  לחץ על אירוע לפתיחת פרטים מלאים",
                          f"{len(events)} events today  ·  click an event for full details"),
             bg=PANEL, fg=DIM, font=("Segoe UI", 9), anchor=i18n.ANCHOR()).pack(fill="x", pady=(2, 0))
    tk.Frame(panel, bg=GRID, height=1).pack(fill="x")

    if not events:
        tk.Label(panel, text=tr("אין התראות היום עדיין", "No alerts today yet"),
                 bg=BG, fg=DIM, font=("Segoe UI", 11),
                 pady=40).pack(fill="x")
        return panel

    # Scrollable list of events (newest first)
    list_frame = tk.Frame(panel, bg=BG)
    list_frame.pack(fill="both", expand=True)
    canvas = tk.Canvas(list_frame, bg=BG, highlightthickness=0, bd=0)
    scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=BG)

    inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    def _on_click(ev: dict) -> None:
        try:
            from alerts import AlertEvent, show_alert_window
            from process_monitor import get_default_sampler
            sampler = get_default_sampler()
            evt = AlertEvent(
                reason=ev["reason"], trigger="cpu",
                cpu_before=ev["cpu_before"], cpu_after=ev["cpu_after"],
                ram_before=ev["ram_before"], ram_after=ev["ram_after"],
                top_cpu=sampler.top_by_cpu(5),
                top_rss=sampler.top_by_rss(5),
            )
            show_alert_window(evt, panel)
        except Exception:
            log.exception("Failed to open alert window from spike list")
            messagebox.showerror(tr("שגיאה", "Error"),
                                 tr("לא הצלחתי לפתוח את חלון ההתראה.",
                                    "Could not open the alert window."),
                                 parent=panel)

    # Newest at top
    for ev in reversed(events):
        accent = _trigger_to_color(ev["reason"])
        card = tk.Frame(inner, bg=PANEL, padx=12, pady=8, cursor="hand2")
        card.pack(fill="x", padx=10, pady=4)
        # Accent stripe
        tk.Frame(card, bg=accent, width=3).pack(side="left", fill="y", padx=(0, 10))

        text_col = tk.Frame(card, bg=PANEL)
        text_col.pack(side="left", fill="both", expand=True)
        tk.Label(text_col, text=ev["time"], bg=PANEL, fg=accent,
                 font=("Cascadia Mono", 11, "bold"), anchor="w").pack(anchor="w")
        tk.Label(text_col, text=ev["reason"], bg=PANEL, fg=FG,
                 font=("Segoe UI", 10), anchor=i18n.ANCHOR(),
                 wraplength=420, justify="right").pack(fill="x", pady=(2, 0))
        meta = (f"CPU {ev['cpu_before']:.0f}% → {ev['cpu_after']:.0f}%   ·   "
                f"RAM {ev['ram_before']:.0f}% → {ev['ram_after']:.0f}%")
        tk.Label(text_col, text=meta, bg=PANEL, fg=DIM,
                 font=("Segoe UI", 9), anchor=i18n.ANCHOR()).pack(fill="x", pady=(2, 0))

        # Bind click to whole card recursively
        for w in (card, text_col):
            w.bind("<Button-1>", lambda _e, e=ev: _on_click(e))
        for w in text_col.winfo_children():
            w.bind("<Button-1>", lambda _e, e=ev: _on_click(e))

        # Hover effect
        def _on_enter(_e, c=card, h=text_col):
            try:
                c.configure(bg=PANEL2); h.configure(bg=PANEL2)
                for w in h.winfo_children():
                    w.configure(bg=PANEL2)
            except tk.TclError:
                pass
        def _on_leave(_e, c=card, h=text_col):
            try:
                c.configure(bg=PANEL); h.configure(bg=PANEL)
                for w in h.winfo_children():
                    w.configure(bg=PANEL)
            except tk.TclError:
                pass
        card.bind("<Enter>", _on_enter)
        card.bind("<Leave>", _on_leave)

    return panel


def _read_today_spikes(spikes_dir: Path) -> list[dict]:
    """Parse today's spike markdown into a list of {time, reason, cpu_before, cpu_after, ...}."""
    today = datetime.now().strftime("%Y-%m-%d")
    target = spikes_dir / f"spikes-{today}.md"
    if not target.exists():
        return []

    events: list[dict] = []
    current: dict | None = None
    try:
        with target.open("r", encoding="utf-8") as f:
            for line in f:
                m = _SPIKE_HEADING_RE.match(line)
                if m:
                    if current:
                        events.append(current)
                    current = {"time": m.group(1), "reason": m.group(2),
                               "cpu_before": 0.0, "cpu_after": 0.0,
                               "ram_before": 0.0, "ram_after": 0.0, "suspects": []}
                    continue
                if current is None:
                    continue
                m = _SPIKE_CPU_RE.match(line)
                if m:
                    current["cpu_before"] = float(m.group(1))
                    current["cpu_after"]  = float(m.group(2))
                    continue
                m = _SPIKE_RAM_RE.match(line)
                if m:
                    current["ram_before"] = float(m.group(1))
                    current["ram_after"]  = float(m.group(2))
                    continue
                m = _SPIKE_SUSPECT_RE.match(line)
                if m:
                    current["suspects"].append((m.group(1), m.group(2)))
        if current:
            events.append(current)
    except OSError:
        log.exception("Could not read spikes file %s", target)
    return events

# Theme — Vercel / Next.js inspired (pure black, zinc grays, restrained accents)
BG     = "#000000"   # pure black
PANEL  = "#09090b"   # zinc-950 (almost black)
PANEL2 = "#18181b"   # zinc-900
FG     = "#fafafa"   # zinc-50
DIM    = "#a1a1aa"   # zinc-400
MUTED  = "#52525b"   # zinc-600
GRID   = "#27272a"   # zinc-800

# Single, opinionated accent palette
CPU_C  = "#3b82f6"   # blue-500
RAM_C  = "#f97316"   # orange-500
DISK_C = "#10b981"   # emerald-500
SWAP_C = "#a855f7"   # purple-500
TEMP_C = "#ef4444"   # red-500
VRAM_C = "#eab308"   # yellow-500


# ---------- History data: parsed once, kept as float arrays ----------
#
# The chart refreshes every couple of seconds while history grows by one row
# per second. Everything below is shaped so a refresh costs the same whether
# the file holds five minutes or a week: only the appended tail is ever parsed,
# windows are found by binary search, and what reaches the screen is capped at
# about one point per pixel of chart width (more could not be seen anyway).

# Metric key -> CSV column. cpu/ram are required for a row to count.
_METRIC_COLUMNS = {
    "cpu": "cpu_percent",
    "ram": "ram_percent",
    "disk": "disk_percent",
    "swap": "swap_percent",
    "temps": "temp_celsius",
    "vram": "vram_percent",
}
_OPTIONAL = ("disk", "swap", "temps", "vram")

# Default cap on points per line. Longer windows are reduced by taking
# the max of each bucket, so a one-second spike in a 24-hour view still shows.
MAX_PLOT_POINTS = 1500
# How much of metrics.csv the open window keeps at full resolution (about
# 10 MB for two days; the longest range shown is 24 hours).
LIVE_KEEP_DAYS = 2.0
# Generous bytes-per-row, used to skip straight to the tail of a big file.
_ROW_BYTES_ESTIMATE = 120

_EPOCH = datetime(1970, 1, 1)


def _wall_days(dt: datetime) -> float:
    """Naive local datetime -> days since 1970 (local wall clock).

    Wall-clock days, not UTC: axis labels show the local time the row was
    written, and DST changes do not shift old rows.
    """
    return (dt - _EPOCH).total_seconds() / 86400.0


class _Series:
    """Parallel float arrays: `t` (wall-clock days, see _wall_days) + one per metric.

    Missing optional values are NaN. array('d') keeps appends cheap and lets
    numpy view the data without copying.
    """

    __slots__ = ("t", "cols")

    def __init__(self) -> None:
        self.t = array("d")
        self.cols = {k: array("d") for k in _METRIC_COLUMNS}

    def __len__(self) -> int:
        return len(self.t)

    def extend_from_rows(self, rows, header: list[str]) -> None:
        """Parse csv.reader rows (lists of str) and append the valid ones."""
        try:
            ts_i = header.index("timestamp_iso")
            cpu_i = header.index(_METRIC_COLUMNS["cpu"])
            ram_i = header.index(_METRIC_COLUMNS["ram"])
        except ValueError:
            return
        nan = float("nan")
        t_app = self.t.append
        cpu_app = self.cols["cpu"].append
        ram_app = self.cols["ram"].append
        optional = [(self.cols[k].append,
                     header.index(_METRIC_COLUMNS[k]) if _METRIC_COLUMNS[k] in header else None)
                    for k in _OPTIONAL]
        for r in rows:
            try:
                t = _wall_days(datetime.fromisoformat(r[ts_i].strip()))
                c = float(r[cpu_i])
                m = float(r[ram_i])
            except (ValueError, TypeError, IndexError):
                continue
            t_app(t)
            cpu_app(c)
            ram_app(m)
            for app, i in optional:
                raw = r[i] if i is not None and i < len(r) else ""
                if raw:
                    try:
                        app(float(raw))
                        continue
                    except ValueError:
                        pass
                app(nan)

    def drop_before(self, cutoff: float) -> "_Series":
        """Remove points older than `cutoff`; return them as a new _Series."""
        n = bisect_left(self.t, cutoff)
        old = _Series()
        if n:
            old.t = self.t[:n]
            del self.t[:n]
            for k, a in self.cols.items():
                old.cols[k] = a[:n]
                del a[:n]
        return old

    def view(self, lo: int = 0):
        """numpy views from index `lo` on. Drop them before the next append:
        array('d') cannot grow while a view of it is alive."""
        import numpy as np

        def _v(a):
            return np.frombuffer(memoryview(a)[lo:], dtype=float) if len(a) > lo else np.empty(0)
        return _v(self.t), {k: _v(a) for k, a in self.cols.items()}


class _IncrementalSeriesReader:
    """Keeps the last LIVE_KEEP_DAYS of metrics.csv parsed, reading only the tail.

    First call: reads the header, then jumps near the end of the file, so a
    week of history is not parsed just to show today. Later calls read only
    the bytes appended since, up to the last complete line: a row the logger
    is still writing is picked up next time, never half-parsed. If the file
    shrinks (rotation, schema migration) it is re-read from scratch.
    """

    def __init__(self, csv_path: Path, keep_days: float = LIVE_KEEP_DAYS) -> None:
        self.csv_path = Path(csv_path)
        self.keep_days = keep_days
        self.on_drop = None   # callable(_Series) given points that age out
        self._reset()

    def _reset(self) -> None:
        self._offset = 0
        self._header: list[str] | None = None
        self.series = _Series()

    def read(self) -> _Series:
        try:
            size = self.csv_path.stat().st_size
        except OSError:
            self._reset()
            return self.series
        if size < self._offset:
            log.debug("CSV shrank below offset %d (now %d); re-reading", self._offset, size)
            self._reset()
        if size == self._offset:
            return self.series
        try:
            with self.csv_path.open("rb") as f:
                if self._header is None:
                    first = f.readline()
                    if not first.endswith(b"\n"):
                        return self.series   # header still being written
                    self._header = next(csv.reader([first.decode("utf-8", "replace")]), [])
                    start = f.tell()
                    tail = size - int(self.keep_days * 86400 * _ROW_BYTES_ESTIMATE)
                    if tail > start:
                        f.seek(tail - 1)
                        f.readline()          # land on a line boundary
                        start = f.tell()
                    self._offset = start
                f.seek(self._offset)
                data = f.read(size - self._offset)
        except OSError:
            log.exception("Failed to read CSV %s incrementally", self.csv_path)
            return self.series
        end = data.rfind(b"\n") + 1
        if end:
            lines = data[:end].decode("utf-8", errors="replace").splitlines()
            self.series.extend_from_rows(csv.reader(lines), self._header)
            self._offset += end
        self._trim()
        return self.series

    def _trim(self) -> None:
        t = self.series.t
        # 10 % slack: trimming shifts the arrays, so do it rarely.
        if len(t) and t[0] < t[-1] - self.keep_days * 1.1:
            old = self.series.drop_before(t[-1] - self.keep_days)
            if self.on_drop is not None and len(old):
                self.on_drop(old)


def _bucket_max(t, cols: dict, starts):
    """One point per bucket (bucket i spans starts[i]..starts[i+1]).

    Time is the bucket's first sample; each metric its max, NaN ignored
    unless the whole bucket is NaN.
    """
    import numpy as np
    return t[starts], {k: np.fmax.reduceat(v, starts) for k, v in cols.items()}


def _window(t, cols: dict, start: float | None, max_points: int = MAX_PLOT_POINTS):
    """Slice sorted numpy series to t >= start, then cap at `max_points`.

    Always returns fresh arrays (never views), plus the in-window point count
    before reduction.
    """
    import numpy as np
    lo = 0 if start is None else int(np.searchsorted(t, start, side="left"))
    lo = min(lo, max(0, len(t) - 1))      # keep at least the newest point
    t = t[lo:]
    cols = {k: v[lo:] for k, v in cols.items()}
    raw_n = len(t)
    if raw_n > max_points:
        step = -(-raw_n // max_points)
        t, cols = _bucket_max(t, cols, np.arange(0, raw_n, step))
    else:
        t, cols = t.copy(), {k: v.copy() for k, v in cols.items()}
    return t, cols, raw_n


# ---------- Plain-words state, shared by the vault window and Ask Claude ----------

GREEN = "#22c55e"
AMBER = "#f59e0b"
RED = "#ef4444"

# Background processes that are never a useful answer to "who is using it".
_NOT_A_SUSPECT = {"system idle process", "idle", "registry", "memory compression"}


def _verdict(cpu: float, ram: float, temp: float | None) -> tuple[str, str, list[str]]:
    """(color, headline, which resources are the problem) for the current state."""
    problems: list[str] = []
    issues: list[str] = []
    if cpu >= 85:
        problems.append(tr("המעבד עמוס מאוד", "The CPU is very busy"))
        issues.append("cpu")
    elif cpu >= 60:
        problems.append(tr("המעבד די עסוק", "The CPU is fairly busy"))
        issues.append("cpu")
    if ram >= 90:
        problems.append(tr("הזיכרון כמעט מלא", "Memory is almost full"))
        issues.append("ram")
    elif ram >= 80:
        problems.append(tr("הזיכרון בשימוש גבוה", "Memory use is high"))
        issues.append("ram")
    if temp is not None and temp >= 85:
        problems.append(tr("המחשב חם", "The computer is running hot"))
    if not problems:
        return GREEN, tr("הכל רגוע", "All calm"), issues
    severe = cpu >= 85 or ram >= 90 or (temp is not None and temp >= 85)
    return (RED if severe else AMBER), " · ".join(problems), issues


def _group_by_name(procs) -> list[tuple[str, float, int, int]]:
    """Fold same-name processes (browser tabs, Electron helpers) into one row.

    Returns (name, cpu % of machine, rss bytes, process count), unsorted.
    """
    groups: dict[str, list] = {}
    for p in procs:
        key = p.name.lower()
        if key in _NOT_A_SUSPECT:
            continue
        g = groups.get(key)
        if g is None:
            groups[key] = [p.name, p.cpu_percent, p.rss_bytes, 1]
        else:
            g[1] += p.cpu_percent
            g[2] += p.rss_bytes
            g[3] += 1
    return [tuple(g) for g in groups.values()]


def _fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{n / 1024 ** 2:.0f} MB"


def open_live_chart(history_dir: Path, parent: tk.Misc | None = None,
                    refresh_ms: int | None = None) -> tk.Toplevel | tk.Tk | None:
    """Open PulseDeck's main window (the vault). Kept under its historic name
    because the dock and tray menus call it."""
    from vault_window import open_vault
    return open_vault(history_dir, parent)
