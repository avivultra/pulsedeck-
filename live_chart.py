"""The "What's happening" window (plain Tk, refreshes every few seconds) and
the history data layer behind its timeline. Also hosts today's alerts panel.
"""
from __future__ import annotations

import csv
import logging
import re
import tkinter as tk
from array import array
from bisect import bisect_left
import math
from datetime import datetime, timedelta
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


# ---------- "What's happening" window ----------
#
# One window that answers "why is my computer slow?": a plain-words verdict,
# who is using the machine right now, a compact timeline with load events
# marked, today's events with their suspects, and what PulseDeck itself costs.
# Drawn with plain Tk widgets and one Canvas: no matplotlib, so opening it is
# instant and a refresh costs a few milliseconds.

GREEN = "#22c55e"
AMBER = "#f59e0b"
RED = "#ef4444"
CPU_FILL = "#0c1a33"     # CPU_C blended into BG (Tk has no alpha)
FONT = "Segoe UI"

RANGE_OPTIONS: list[tuple[str, int]] = [
    ("5m", 5), ("15m", 15), ("1h", 60), ("6h", 360), ("24h", 1440),
]
_REFRESH_MS = 2000
_TOP_N = 5
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


def _days_to_datetime(d: float) -> datetime:
    return _EPOCH + timedelta(days=float(d))


# Tick spacing candidates (seconds) for the time axis; first one giving at
# most six labels wins.
_TICK_STEPS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200)


def _time_ticks(xmin: float, xmax: float) -> list[float]:
    span = (xmax - xmin) * 86400
    step = next((s for s in _TICK_STEPS if span / s <= 6), _TICK_STEPS[-1])
    first = math.ceil(xmin * 86400 / step) * step
    return [s / 86400 for s in range(int(first), int(xmax * 86400) + 1, step)]


class _Timeline:
    """CPU (area + line) and RAM (line) over time, 0–100 %, on one Canvas.

    Items are created once and moved with coords(); only the handful of grid
    and tick items are rebuilt per render. Hovering shows the values under
    the cursor, and any load event near it.
    """

    PAD_L, PAD_R, PAD_T, PAD_B = 36, 12, 12, 22

    def __init__(self, parent: tk.Misc) -> None:
        self.c = tk.Canvas(parent, bg=BG, height=180, highlightthickness=0, bd=0)
        c = self.c
        self.area = c.create_polygon(0, 0, 0, 0, 0, 0, fill=CPU_FILL, outline="", state="hidden")
        self.ram = c.create_line(0, 0, 0, 0, fill=RAM_C, width=1.5, state="hidden")
        self.cpu = c.create_line(0, 0, 0, 0, fill=CPU_C, width=1.5, state="hidden")
        self.vline = c.create_line(0, 0, 0, 0, fill=MUTED, dash=(2, 3), state="hidden")
        self.tip_bg = c.create_rectangle(0, 0, 0, 0, fill=PANEL2, outline=GRID, state="hidden")
        self.tip = c.create_text(0, 0, text="", fill=FG, anchor="nw",
                                 font=(FONT, 9), state="hidden")
        self.empty = c.create_text(0, 0, text=tr("ממתין לדגימות…", "Waiting for samples…"),
                                   fill=DIM, font=(FONT, 11))
        self._data: dict | None = None
        c.bind("<Configure>", lambda _e: self._render())
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", lambda _e: self._hide_tip())

    def plot(self, t, cols: dict, xmin: float, xmax: float,
             events: list[tuple[float, str]]) -> None:
        self._data = {"t": t, "cols": cols, "xmin": xmin, "xmax": xmax, "events": events}
        self._render()

    def clear(self) -> None:
        self._data = None
        self._render()

    # -- geometry
    def _size(self) -> tuple[int, int]:
        return self.c.winfo_width(), self.c.winfo_height()

    def _x(self, t, w: int):
        d = self._data
        return self.PAD_L + (t - d["xmin"]) / (d["xmax"] - d["xmin"]) * (w - self.PAD_L - self.PAD_R)

    def _y(self, v, h: int):
        return self.PAD_T + (1.0 - v / 100.0) * (h - self.PAD_T - self.PAD_B)

    def plot_width(self) -> int:
        return max(50, self._size()[0] - self.PAD_L - self.PAD_R)

    def _render(self) -> None:
        import numpy as np
        c = self.c
        w, h = self._size()
        if w < 60 or h < 40:
            return
        c.delete("axis")
        c.coords(self.empty, w / 2, h / 2)
        y_base = self._y(0, h)
        for v in (0, 25, 50, 75, 100):
            y = self._y(v, h)
            c.create_line(self.PAD_L, y, w - self.PAD_R, y, fill=GRID,
                          dash=(1, 4) if v else (), tags="axis")
            c.create_text(self.PAD_L - 6, y, text=f"{v}%" if v else "0", fill=MUTED,
                          anchor="e", font=(FONT, 8), tags="axis")
        d = self._data
        if d is None or len(d["t"]) < 2:
            for item in (self.area, self.cpu, self.ram):
                c.itemconfigure(item, state="hidden")
            c.itemconfigure(self.empty, state="normal")
            c.tag_lower("axis")
            return
        c.itemconfigure(self.empty, state="hidden")
        for tv in _time_ticks(d["xmin"], d["xmax"]):
            x = float(self._x(tv, w))
            c.create_text(x, h - self.PAD_B + 6, text=_days_to_datetime(tv).strftime("%H:%M"),
                          fill=MUTED, anchor="n", font=(FONT, 8), tags="axis")
        xs = self._x(d["t"], w)
        for item, key in ((self.cpu, "cpu"), (self.ram, "ram")):
            v = d["cols"][key]
            ok = ~np.isnan(v)
            if ok.sum() < 2:
                c.itemconfigure(item, state="hidden")
                continue
            pts = np.column_stack((xs[ok], self._y(v[ok], h))).ravel().tolist()
            c.coords(item, *pts)
            c.itemconfigure(item, state="normal")
            if key == "cpu":
                c.coords(self.area, pts[0], y_base, *pts, pts[-2], y_base)
                c.itemconfigure(self.area, state="normal")
        for et, _label in d["events"]:
            if d["xmin"] <= et <= d["xmax"]:
                x = float(self._x(et, w))
                c.create_polygon(x - 5, self.PAD_T - 2, x + 5, self.PAD_T - 2, x, self.PAD_T + 6,
                                 fill=RED, outline="", tags="axis")
                c.create_line(x, self.PAD_T + 6, x, y_base, fill=RED, dash=(1, 3), tags="axis")
        c.tag_lower("axis")
        c.tag_lower(self.area)

    # -- hover
    def _hide_tip(self) -> None:
        for item in (self.vline, self.tip_bg, self.tip):
            self.c.itemconfigure(item, state="hidden")

    def _on_motion(self, e) -> None:
        import numpy as np
        d = self._data
        w, h = self._size()
        if d is None or len(d["t"]) < 2 or not (self.PAD_L <= e.x <= w - self.PAD_R):
            self._hide_tip()
            return
        t = d["t"]
        tv = d["xmin"] + (e.x - self.PAD_L) / (w - self.PAD_L - self.PAD_R) * (d["xmax"] - d["xmin"])
        i = int(np.clip(np.searchsorted(t, tv), 1, len(t) - 1))
        if abs(t[i - 1] - tv) < abs(t[i] - tv):
            i -= 1
        x = float(self._x(t[i], w))
        cols = d["cols"]
        lines = [_days_to_datetime(t[i]).strftime("%H:%M:%S"),
                 f"CPU {cols['cpu'][i]:.0f}%   RAM {cols['ram'][i]:.0f}%"]
        temp = cols["temps"][i]
        if temp == temp:   # not NaN
            lines[-1] += f"   {temp:.0f}°C"
        for et, label in d["events"]:
            if abs(float(self._x(et, w)) - e.x) <= 6:
                lines.append("⚠ " + label)
        c = self.c
        c.coords(self.vline, x, self.PAD_T, x, self._y(0, h))
        # bbox() of a hidden item is None, so show it before measuring.
        for item in (self.vline, self.tip_bg, self.tip):
            c.itemconfigure(item, state="normal")
        c.itemconfigure(self.tip, text="\n".join(lines))
        bx0, by0, bx1, by1 = c.bbox(self.tip)
        tw = bx1 - bx0
        tx = x + 12 if x + 12 + tw + 8 < w else x - 12 - tw
        c.coords(self.tip, tx, self.PAD_T + 4)
        bx0, by0, bx1, by1 = c.bbox(self.tip)
        c.coords(self.tip_bg, bx0 - 6, by0 - 4, bx1 + 6, by1 + 4)
        for item in (self.vline, self.tip_bg, self.tip):
            c.tag_raise(item)


def open_live_chart(history_dir: Path, parent: tk.Misc | None = None,
                    refresh_ms: int = _REFRESH_MS) -> tk.Toplevel | tk.Tk | None:
    """Open the "What's happening" window. Returns the window (or None on failure).

    Kept under its historic name: the dock and tray menus call it.
    """
    import importlib.util
    if importlib.util.find_spec("numpy") is None:   # the data layer needs it
        log.error("numpy not installed; cannot open the window")
        return None
    from monitor import collect_snapshot, disk_root_path
    from self_usage import format_self_usage, self_usage
    from temperature_readings import read_gpu_temp_celsius

    history_dir = Path(history_dir)
    side, side_end, anchor = i18n.SIDE(), i18n.SIDE_END(), i18n.ANCHOR()

    win: tk.Toplevel | tk.Tk = tk.Tk() if parent is None else tk.Toplevel(parent)
    win.title(tr("מה קורה במחשב", "What's happening"))
    win.geometry("780x760")
    win.minsize(560, 600)
    win.configure(bg=BG)

    def _label(master, text="", fg=FG, size=10, bold=False, bg=None, **kw) -> tk.Label:
        return tk.Label(master, text=text, fg=fg, bg=bg or master["bg"],
                        font=(FONT, size, "bold" if bold else "normal"), **kw)

    def _section(title: str) -> tk.Frame:
        outer = tk.Frame(win, bg=BG, padx=18)
        outer.pack(fill="x", pady=(14, 0))
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        _label(head, title, fg=DIM, size=9, bold=True).pack(side=side)
        outer.head = head  # type: ignore[attr-defined]
        return outer

    # ---- verdict header
    header = tk.Frame(win, bg=PANEL, padx=18, pady=14)
    header.pack(fill="x")
    dot = tk.Canvas(header, width=14, height=14, bg=PANEL, highlightthickness=0, bd=0)
    dot_item = dot.create_oval(1, 1, 13, 13, fill=GREEN, outline="")
    dot.pack(side=side, padx=(0, 10) if side == "left" else (10, 0))
    verdict_var = tk.StringVar(value=tr("בודק…", "Checking…"))

    def _claude_snapshot() -> dict:
        from ai_advisor import build_snapshot
        s = _sampler()
        return build_snapshot(collect_snapshot(disk_path), _group_by_name(s.snapshot()) if s else [],
                              state["events"], gpu_temp=read_gpu_temp_celsius())

    def _ask_claude(_e=None) -> None:
        try:
            from claude_panel import open_claude_panel
            open_claude_panel(win, _claude_snapshot)
        except Exception:
            log.exception("Could not open the Ask Claude panel")

    ask_btn = _label(header, tr("✳ שאל את Claude", "✳ Ask Claude"), fg="#d97757", size=10,
                     bold=True, bg=PANEL2, padx=10, pady=3, cursor="hand2")
    ask_btn.pack(side=side_end)
    ask_btn.bind("<Button-1>", _ask_claude)
    _label(header, textvariable=verdict_var, size=14, bold=True, anchor=anchor).pack(
        side=side, fill="x", expand=True)
    tk.Frame(win, bg=GRID, height=1).pack(fill="x")

    # ---- stat cards
    cards_row = tk.Frame(win, bg=BG, padx=12)
    cards_row.pack(fill="x", pady=(12, 0))
    cards: dict[str, tuple[tk.StringVar, tk.StringVar]] = {}
    for key, title, color in (("cpu", tr("מעבד", "CPU"), CPU_C),
                              ("ram", tr("זיכרון", "Memory"), RAM_C),
                              ("disk", tr("דיסק", "Disk"), DISK_C),
                              ("temp", tr("טמפרטורה", "Temperature"), TEMP_C)):
        card = tk.Frame(cards_row, bg=PANEL, padx=12, pady=10)
        card.pack(side=side, fill="x", expand=True, padx=6)
        tk.Frame(card, bg=color, height=2).pack(fill="x", pady=(0, 8))
        _label(card, title, fg=DIM, size=9, anchor=anchor).pack(fill="x")
        value, sub = tk.StringVar(value="—"), tk.StringVar(value="")
        _label(card, textvariable=value, size=20, bold=True, anchor=anchor).pack(fill="x")
        _label(card, textvariable=sub, fg=MUTED, size=8, anchor=anchor).pack(fill="x")
        cards[key] = (value, sub)

    # ---- who is using the machine
    who = _section(tr("מי משתמש במחשב עכשיו", "Who is using the computer right now"))
    who_cols = tk.Frame(who, bg=BG)
    who_cols.pack(fill="x", pady=(6, 0))
    top_rows: dict[str, list[tuple[tk.StringVar, tk.StringVar]]] = {}
    for key, title in (("cpu", tr("הכי הרבה מעבד", "Most CPU")),
                       ("ram", tr("הכי הרבה זיכרון", "Most memory"))):
        col = tk.Frame(who_cols, bg=PANEL, padx=12, pady=8)
        col.pack(side=side, fill="both", expand=True, padx=(0, 6) if key == "cpu" else (6, 0))
        _label(col, title, fg=CPU_C if key == "cpu" else RAM_C, size=9, bold=True,
               anchor=anchor).pack(fill="x", pady=(0, 4))
        rows = []
        for _ in range(_TOP_N):
            r = tk.Frame(col, bg=PANEL)
            r.pack(fill="x")
            name, val = tk.StringVar(), tk.StringVar()
            _label(r, textvariable=name, size=9, anchor=anchor).pack(side=side, fill="x", expand=True)
            _label(r, textvariable=val, fg=DIM, size=9).pack(side=side_end)
            rows.append((name, val))
        top_rows[key] = rows

    # ---- timeline
    tl = _section(tr("ציר זמן", "Timeline"))
    range_min = tk.IntVar(value=60)
    pills: dict[int, tk.Label] = {}

    def _select_range(mins: int) -> None:
        range_min.set(mins)
        for m, lbl in pills.items():
            lbl.configure(bg=PANEL2 if m == mins else BG, fg=FG if m == mins else MUTED)
        state["key"] = None
        refresh_timeline()

    pill_box = tk.Frame(tl.head, bg=BG)
    pill_box.pack(side=side_end)
    for text, mins in RANGE_OPTIONS:
        lbl = _label(pill_box, text, fg=MUTED, size=9, padx=8, pady=1, cursor="hand2")
        lbl.pack(side="left", padx=1)
        lbl.bind("<Button-1>", lambda _e, m=mins: _select_range(m))
        pills[mins] = lbl
    legend = tk.Frame(tl.head, bg=BG)
    legend.pack(side=side, padx=12)
    for text, color in (("CPU", CPU_C), ("RAM", RAM_C), (tr("אירוע עומס", "load event"), RED)):
        _label(legend, "●", fg=color, size=9).pack(side=side)
        _label(legend, text, fg=MUTED, size=8).pack(side=side, padx=(0, 8))
    timeline = _Timeline(tl)
    timeline.c.pack(fill="x", pady=(6, 0))

    # ---- today's events
    ev_sec = _section(tr("אירועי עומס היום", "Load events today"))
    all_link = _label(ev_sec.head, tr("כל הפרטים ←", "All details →"), fg=CPU_C, size=9,
                      cursor="hand2")
    all_link.pack(side=side_end)
    ev_list = tk.Frame(ev_sec, bg=BG)
    ev_list.pack(fill="x", pady=(6, 0))

    # ---- footer
    tk.Frame(win, bg=BG).pack(fill="both", expand=True)
    tk.Frame(win, bg=GRID, height=1).pack(fill="x")
    footer = tk.Frame(win, bg=PANEL, padx=18, pady=6)
    footer.pack(fill="x")
    self_var, updated_var = tk.StringVar(), tk.StringVar()
    _label(footer, textvariable=self_var, fg=MUTED, size=8).pack(side=side)
    _label(footer, textvariable=updated_var, fg=MUTED, size=8).pack(side=side_end)

    state: dict = {"key": None, "events": [], "events_sig": None}
    incremental = _IncrementalSeriesReader(history_dir / "metrics.csv")
    disk_path = disk_root_path()
    from metric_history import DEFAULT_SPIKES_DIR as spikes_dir

    def _sampler():
        try:
            from process_monitor import get_default_sampler, peek_default_sampler
            s = peek_default_sampler() or get_default_sampler()
            s.notify_activity()   # sample faster while someone is looking
            return s
        except Exception:
            log.exception("Process sampler unavailable")
            return None

    def _open_event(ev: dict) -> None:
        try:
            from alerts import AlertEvent, show_alert_window
            s = _sampler()
            show_alert_window(AlertEvent(
                reason=ev["reason"], trigger="cpu",
                cpu_before=ev["cpu_before"], cpu_after=ev["cpu_after"],
                ram_before=ev["ram_before"], ram_after=ev["ram_after"],
                top_cpu=s.top_by_cpu(5) if s else [], top_rss=s.top_by_rss(5) if s else [],
            ), win)
        except Exception:
            log.exception("Failed to open alert window")

    all_link.bind("<Button-1>", lambda _e: _show_alerts_panel(win, state["events"]))

    def refresh_events() -> None:
        target = spikes_dir / f"spikes-{datetime.now():%Y-%m-%d}.md"
        try:
            st = target.stat()
            sig = (target.name, st.st_size, st.st_mtime)
        except OSError:
            sig = (target.name, None, None)
        if sig == state["events_sig"]:
            return
        state["events_sig"] = sig
        state["events"] = _read_today_spikes(spikes_dir)
        for child in ev_list.winfo_children():
            child.destroy()
        if not state["events"]:
            _label(ev_list, tr("לא היו אירועי עומס היום 👍", "No load events today 👍"),
                   fg=DIM, size=10, anchor=anchor).pack(fill="x", pady=4)
            return
        for ev in list(reversed(state["events"]))[:5]:
            row = tk.Frame(ev_list, bg=PANEL, padx=10, pady=6, cursor="hand2")
            row.pack(fill="x", pady=2)
            accent = _trigger_to_color(ev["reason"])
            tk.Frame(row, bg=accent, width=3).pack(side=side, fill="y")
            _label(row, ev["time"], fg=accent, size=10, bold=True).pack(side=side, padx=8)
            suspects = ", ".join(name for name, _size in ev.get("suspects", [])[:2])
            text = ev["reason"] + (tr(f"  ·  חשודים: {suspects}", f"  ·  suspects: {suspects}")
                                   if suspects else "")
            msg = _label(row, text, size=9, anchor=anchor)
            msg.pack(side=side, fill="x", expand=True)
            for wdg in (row, msg, *row.winfo_children()):
                wdg.bind("<Button-1>", lambda _e, e=ev: _open_event(e))

    def _event_marks() -> list[tuple[float, str]]:
        today = datetime.now().date()
        marks = []
        for ev in state["events"]:
            try:
                hh, mm, ss = (int(p) for p in ev["time"].split(":"))
            except ValueError:
                continue
            suspects = ", ".join(n for n, _s in ev.get("suspects", [])[:2])
            label = ev["time"] + "  " + ev["reason"] + (f"  ({suspects})" if suspects else "")
            marks.append((_wall_days(datetime(today.year, today.month, today.day, hh, mm, ss)),
                          label))
        return marks

    def refresh_timeline() -> None:
        series = incremental.read()
        mins = range_min.get()
        key = (len(series), series.t[-1] if len(series) else None, mins,
               len(state["events"]), timeline.plot_width())
        if key == state["key"]:
            return
        state["key"] = key
        if len(series) < 2:
            timeline.clear()
            return
        newest = series.t[-1]
        start = newest - mins / 1440.0
        t, cols = series.view(bisect_left(series.t, start))
        t, cols, _n = _window(t, cols, start, max_points=timeline.plot_width())
        timeline.plot(t, cols, start, newest + max(2, mins * 60 * 0.01) / 86400.0,
                      _event_marks())

    def refresh_now() -> None:
        snap = collect_snapshot(disk_path)
        gpu_t = read_gpu_temp_celsius()
        s = _sampler()
        groups = _group_by_name(s.snapshot()) if s else []
        by_cpu = sorted((g for g in groups if g[1] >= 0.1),
                        key=lambda g: g[1], reverse=True)[:_TOP_N]
        by_ram = sorted(groups, key=lambda g: g[2], reverse=True)[:_TOP_N]

        color, headline, issues = _verdict(snap.cpu_percent, snap.ram_percent, snap.temp_celsius)
        culprit = (by_cpu[0][0] if "cpu" in issues and by_cpu
                   else by_ram[0][0] if "ram" in issues and by_ram else None)
        if culprit:
            headline += tr(f" — בעיקר {culprit}", f" — mostly {culprit}")
        verdict_var.set(headline)
        dot.itemconfigure(dot_item, fill=color)

        cards["cpu"][0].set(f"{snap.cpu_percent:.0f}%")
        cards["cpu"][1].set(tr(f"{snap.cpu_logical} ליבות", f"{snap.cpu_logical} cores"))
        cards["ram"][0].set(f"{snap.ram_percent:.0f}%")
        cards["ram"][1].set(f"{_fmt_bytes(snap.ram_used)} / {_fmt_bytes(snap.ram_total)}")
        cards["disk"][0].set("—" if snap.disk_percent is None else f"{snap.disk_percent:.0f}%")
        cards["disk"][1].set(snap.disk_path if snap.disk_total is None
                             else f"{snap.disk_path}  {_fmt_bytes(snap.disk_total - (snap.disk_used or 0))} "
                                  + tr("פנוי", "free"))
        cards["temp"][0].set("—" if snap.temp_celsius is None else f"{snap.temp_celsius:.0f}°C")
        cards["temp"][1].set(f"GPU {gpu_t:.0f}°C" if gpu_t is not None
                             else "" if snap.temp_celsius is not None
                             else tr("אין חיישן זמין", "no sensor available"))

        for key, rows, fmt in (("cpu", by_cpu, lambda g: f"{g[1]:.1f}%"),
                               ("ram", by_ram, lambda g: _fmt_bytes(g[2]))):
            for i, (name_var, val_var) in enumerate(top_rows[key]):
                if i < len(rows):
                    g = rows[i]
                    name_var.set(g[0] + (f"  ×{g[3]}" if g[3] > 1 else ""))
                    val_var.set(fmt(g))
                else:
                    name_var.set("")
                    val_var.set("")
        pct, rss = self_usage()
        self_var.set("PulseDeck: " + format_self_usage(pct, rss))
        updated_var.set(tr(f"עודכן {datetime.now():%H:%M:%S}", f"Updated {datetime.now():%H:%M:%S}"))

    def refresh() -> None:
        for step in (refresh_now, refresh_events, refresh_timeline):
            try:
                step()
            except Exception:
                log.exception("Window refresh step %s failed", step.__name__)

    def tick() -> None:
        if not win.winfo_exists():
            return
        # Minimised: nobody is looking, so don't do the work.
        if win.state() not in ("iconic", "withdrawn"):
            refresh()
        win.after(refresh_ms, tick)

    _select_range(60)
    win.update_idletasks()
    refresh()
    win.after(refresh_ms, tick)

    if parent is None:
        try:
            win.mainloop()
        except tk.TclError:
            pass
    return win
