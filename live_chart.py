"""Live performance chart — Tkinter window embedding matplotlib that
auto-refreshes every few seconds. Replaces the static PNG view.
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
                               "ram_before": 0.0, "ram_after": 0.0}
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
# windows are found by binary search, and what reaches matplotlib is capped at
# MAX_PLOT_POINTS (an 1100-px-wide chart cannot show more anyway).

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

# Points handed to matplotlib per line. Longer windows are reduced by taking
# the max of each bucket, so a one-second spike in a 24-hour view still shows.
MAX_PLOT_POINTS = 1500
# How much of metrics.csv the open chart keeps at full resolution (about
# 10 MB for two days). Older data is shown, per minute, via the archive toggle.
LIVE_KEEP_DAYS = 2.0
ARCHIVE_BUCKET_DAYS = 60.0 / 86400.0
# Generous bytes-per-row, used to skip straight to the tail of a big file.
_ROW_BYTES_ESTIMATE = 120

_EPOCH = datetime(1970, 1, 1)   # matplotlib's default date epoch (>= 3.3)


def _wall_days(dt: datetime) -> float:
    """Naive local datetime -> matplotlib date number.

    Same value mdates.date2num gives for a naive datetime, without its
    per-call overhead; axis labels show the local time the row was written.
    """
    return (dt - _EPOCH).total_seconds() / 86400.0


class _Series:
    """Parallel float arrays: `t` (matplotlib date numbers) + one per metric.

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


def _to_minutes(t, cols: dict):
    """Collapse sorted numpy series to one point per ARCHIVE_BUCKET_DAYS."""
    import numpy as np
    if not len(t):
        return t.copy(), {k: v.copy() for k, v in cols.items()}
    bucket = np.floor(t / ARCHIVE_BUCKET_DAYS)
    starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
    return _bucket_max(t, cols, starts)


class _ArchiveSeries:
    """Per-minute view of everything older than the live reader holds.

    Built on first use of the archive toggle (archive files + the older part
    of metrics.csv the live reader skipped), extended as the live reader ages
    points out, rebuilt only when the archive files change (daily rotation).
    """

    def __init__(self, history_dir: Path, live: _IncrementalSeriesReader) -> None:
        self.history_dir = Path(history_dir)
        self.live = live
        self._signature: tuple | None = None
        self.t = None
        self.cols: dict = {}
        live.on_drop = self._absorb

    def _files(self) -> list[Path]:
        from metric_history import ARCHIVE_PREFIX, ARCHIVE_SUFFIX
        return sorted(self.history_dir.glob(f"{ARCHIVE_PREFIX}*{ARCHIVE_SUFFIX}"))

    @staticmethod
    def _sig(files: list[Path]) -> tuple:
        out = []
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            out.append((p.name, st.st_size, st.st_mtime))
        return tuple(out)

    def get(self):
        files = self._files()
        sig = self._sig(files)
        if self.t is None or sig != self._signature:
            self._build(files)
            self._signature = sig
        return self.t, self.cols

    def _build(self, files: list[Path]) -> None:
        import numpy as np
        live = self.live.series
        live_start = live.t[0] if len(live) else float("inf")
        parts_t, parts = [], {k: [] for k in _METRIC_COLUMNS}
        # One file at a time, reduced to minutes right away: a quarter of
        # per-second archives never sits in memory at once.
        for src in files + [self.live.csv_path]:
            s = _Series()
            try:
                with src.open("r", encoding="utf-8", newline="", errors="replace") as f:
                    reader = csv.reader(f)
                    header = next(reader, None)
                    if header:
                        s.extend_from_rows(reader, header)
            except OSError:
                log.exception("Failed to read history source %s", src)
                continue
            t, cols = s.view()
            keep = t < live_start
            order = np.argsort(t[keep], kind="stable")
            mt, mcols = _to_minutes(t[keep][order], {k: v[keep][order] for k, v in cols.items()})
            del t, cols
            parts_t.append(mt)
            for k in parts:
                parts[k].append(mcols[k])
        if not parts_t:
            self.t, self.cols = np.empty(0), {k: np.empty(0) for k in _METRIC_COLUMNS}
            return
        t = np.concatenate(parts_t)
        order = np.argsort(t, kind="stable")
        self.t = t[order]
        self.cols = {k: np.concatenate(v)[order] for k, v in parts.items()}

    def _absorb(self, old: _Series) -> None:
        if self.t is None:
            return
        import numpy as np
        t, cols = _to_minutes(*old.view())
        self.t = np.concatenate([self.t, t])
        self.cols = {k: np.concatenate([self.cols[k], cols[k]]) for k in cols}


def _window(t, cols: dict, start: float | None):
    """Slice sorted numpy series to t >= start, then cap at MAX_PLOT_POINTS.

    Always returns fresh arrays (never views), plus the in-window point count
    before reduction.
    """
    import numpy as np
    lo = 0 if start is None else int(np.searchsorted(t, start, side="left"))
    lo = min(lo, max(0, len(t) - 1))      # keep at least the newest point
    t = t[lo:]
    cols = {k: v[lo:] for k, v in cols.items()}
    raw_n = len(t)
    if raw_n > MAX_PLOT_POINTS:
        step = -(-raw_n // MAX_PLOT_POINTS)
        t, cols = _bucket_max(t, cols, np.arange(0, raw_n, step))
    else:
        t, cols = t.copy(), {k: v.copy() for k, v in cols.items()}
    return t, cols, raw_n


def open_live_chart(history_dir: Path, parent: tk.Misc | None = None,
                    refresh_ms: int = 2000) -> tk.Toplevel | tk.Tk | None:
    """Open the live chart window. Returns the window (or None on failure)."""
    try:
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.dates as mdates
        import numpy as np
        from matplotlib.backends.backend_tkagg import (
            FigureCanvasTkAgg, NavigationToolbar2Tk,
        )
        from matplotlib.figure import Figure
    except ImportError:
        log.exception("matplotlib not installed; cannot open live chart")
        return None

    history_dir = Path(history_dir)

    if parent is None:
        win: tk.Toplevel | tk.Tk = tk.Tk()
    else:
        win = tk.Toplevel(parent)
    win.title(tr("גרף ביצועים — חי", "Performance chart — live"))
    win.geometry("1100x620")
    win.configure(bg=BG)

    # Top control bar — slim, single hairline divider below
    top = tk.Frame(win, bg=PANEL, padx=14, pady=8)
    top.pack(fill="x")
    tk.Frame(win, bg=GRID, height=1).pack(fill="x")  # hairline divider

    include_archive = tk.BooleanVar(value=False)
    show_disk = tk.BooleanVar(value=True)
    show_swap = tk.BooleanVar(value=True)
    show_temp = tk.BooleanVar(value=True)
    show_vram = tk.BooleanVar(value=True)
    paused = tk.BooleanVar(value=False)

    # Time window options (label, minutes — None = show all)
    WINDOW_OPTIONS: list[tuple[str, int | None]] = [
        (tr("5 דקות אחרונות", "Last 5 minutes"), 5),
        (tr("15 דקות אחרונות", "Last 15 minutes"), 15),
        (tr("שעה אחרונה", "Last hour"), 60),
        (tr("6 שעות אחרונות", "Last 6 hours"), 360),
        (tr("24 שעות אחרונות", "Last 24 hours"), 1440),
        (tr("הכל", "All"), None),
    ]
    window_label = tk.StringVar(value=tr("15 דקות אחרונות", "Last 15 minutes"))

    def _check(parent_w, text: str, var: tk.BooleanVar) -> tk.Checkbutton:
        return tk.Checkbutton(parent_w, text=text, variable=var,
                              bg=PANEL, fg=DIM, selectcolor=PANEL2,
                              activebackground=PANEL, activeforeground=FG,
                              font=("Segoe UI", 9), bd=0, highlightthickness=0,
                              command=lambda: redraw())

    # Right-side title (Hebrew)
    tk.Label(top, text=tr("גרף ביצועים חי", "Live performance chart"), bg=PANEL, fg=FG,
             font=("Segoe UI", 11, "bold"), anchor=i18n.ANCHOR()).pack(side=i18n.SIDE(), padx=(8, 0))

    # Time window combobox — themed via ttk style
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("Live.TCombobox",
                    fieldbackground=PANEL2, background=PANEL2,
                    foreground=FG, arrowcolor=DIM,
                    bordercolor=GRID, lightcolor=GRID, darkcolor=GRID,
                    selectbackground=PANEL2, selectforeground=FG)

    win_combo = ttk.Combobox(top, textvariable=window_label,
                              values=[lbl for lbl, _ in WINDOW_OPTIONS],
                              state="readonly", width=20, font=("Segoe UI", 9),
                              style="Live.TCombobox")
    win_combo.pack(side="left")
    win_combo.bind("<<ComboboxSelected>>", lambda _e: redraw())

    # Subtle divider before checkboxes
    tk.Frame(top, bg=GRID, width=1, height=18).pack(side="left", padx=10)

    _check(top, tr("ארכיון", "Archive"), include_archive).pack(side="left")
    _check(top, "Disk", show_disk).pack(side="left", padx=(6, 0))
    _check(top, "Swap", show_swap).pack(side="left", padx=(6, 0))
    _check(top, "VRAM", show_vram).pack(side="left", padx=(6, 0))
    _check(top, "Temp", show_temp).pack(side="left", padx=(6, 0))

    tk.Frame(top, bg=GRID, width=1, height=18).pack(side="left", padx=10)

    pause_btn = tk.Checkbutton(top, text=tr("השהה רענון", "Pause refresh"), variable=paused,
                               bg=PANEL, fg=DIM, selectcolor=PANEL2,
                               activebackground=PANEL, activeforeground=FG,
                               font=("Segoe UI", 9), bd=0, highlightthickness=0)
    pause_btn.pack(side="left")

    tk.Frame(top, bg=GRID, width=1, height=18).pack(side="left", padx=10)

    # 🔔 Alerts button — opens the spike browser
    def _open_alerts_panel() -> None:
        from metric_history import DEFAULT_SPIKES_DIR
        events = _read_today_spikes(DEFAULT_SPIKES_DIR)
        _show_alerts_panel(win, events)

    alerts_btn = tk.Button(top, text=tr("🔔 התראות אחרונות", "🔔 Recent alerts"),
                           bg=PANEL, fg=FG,
                           activebackground=PANEL2, activeforeground=FG,
                           relief="flat", bd=0, font=("Segoe UI", 9, "bold"),
                           padx=8, pady=2, cursor="hand2",
                           command=_open_alerts_panel)
    alerts_btn.pack(side="left")

    status_var = tk.StringVar(value=tr("טוען…", "Loading…"))
    tk.Label(top, textvariable=status_var, bg=PANEL, fg=MUTED,
             font=("Segoe UI", 9)).pack(side="left", padx=(14, 0))

    # Figure
    fig = Figure(figsize=(11, 5.5), facecolor=BG)
    ax1 = fig.add_subplot(111, facecolor=BG)
    ax2 = ax1.twinx()
    ax2.set_facecolor(BG)

    canvas = FigureCanvasTkAgg(fig, master=win)
    canvas.get_tk_widget().pack(fill="both", expand=True)

    # Toolbar (zoom/pan/save/home)
    toolbar_frame = tk.Frame(win, bg=PANEL)
    toolbar_frame.pack(fill="x", side="bottom")
    toolbar = NavigationToolbar2Tk(canvas, toolbar_frame, pack_toolbar=False)
    toolbar.config(bg=PANEL)
    for child in toolbar.winfo_children():
        try:
            child.config(bg=PANEL)
        except tk.TclError:
            pass
    toolbar.update()
    toolbar.pack(side="left")

    def _style_axes() -> None:
        # Vercel-style minimalism: hide all spines except a hairline bottom
        for ax in (ax1, ax2):
            ax.tick_params(colors=MUTED, labelsize=8, length=0, pad=8)
            for side in ("top", "right", "left"):
                ax.spines[side].set_visible(False)
            ax.spines["bottom"].set_color(GRID)
            ax.spines["bottom"].set_linewidth(0.5)

        # Very subtle dotted horizontal reference lines (zinc-800)
        ax1.grid(True, axis="y", color=GRID, alpha=0.6,
                 linewidth=0.4, linestyle=(0, (1, 4)))  # dotted with wide gaps
        ax1.grid(False, axis="x")
        ax1.set_axisbelow(True)

        # No y-axis label — y ticks already say "Percent" by being 0-100
        ax1.set_ylabel("")
        ax2.set_ylabel("")
        ax1.set_ylim(0, 105)
        ax1.set_yticks([0, 25, 50, 75, 100])

        # Hide y-tick labels on the right axis (keep its data plotted, label-free)
        ax2.set_yticklabels([])
        ax1.title.set_color(FG)

    def _selected_window_minutes() -> int | None:
        for lbl, mins in WINDOW_OPTIONS:
            if lbl == window_label.get():
                return mins
        return 15

    # Static styling happens once; each refresh only moves data into the
    # artists below (no ax.clear(), no re-styling, no new Line2D objects).
    _style_axes()
    ax1.xaxis_date()
    ax1.set_title("system metrics", color=DIM, fontsize=10, fontweight="normal",
                  pad=12, loc="left")
    ax1.set_xlabel("")
    fig.subplots_adjust(left=0.05, right=0.93, top=0.90, bottom=0.12)

    def _line(ax, color, width, alpha=1.0, label=None, rounded=False):
        style = {"solid_capstyle": "round", "solid_joinstyle": "round"} if rounded else {}
        return ax.plot([], [], color=color, linewidth=width, alpha=alpha,
                       label=label, **style)[0]

    lines = {
        "cpu": _line(ax1, CPU_C, 1.3, label="CPU", rounded=True),
        "ram": _line(ax1, RAM_C, 1.3, label="RAM", rounded=True),
        "disk": _line(ax1, DISK_C, 1.0, 0.7, "Disk"),
        "swap": _line(ax1, SWAP_C, 1.0, 0.7, "Swap"),
        "vram": _line(ax1, VRAM_C, 1.0, 0.75, "VRAM"),
        "temps": _line(ax2, TEMP_C, 1.0, 0.85, "Temp"),
    }
    toggles = {"disk": show_disk, "swap": show_swap, "vram": show_vram, "temps": show_temp}
    # Last-point markers and inline end-of-line labels for CPU and RAM.
    dots = {k: ax1.plot([], [], "o", color=c, markersize=28 ** 0.5,
                        markeredgewidth=0, zorder=5)[0]
            for k, c in (("cpu", CPU_C), ("ram", RAM_C))}
    end_labels = {k: ax1.annotate("", xy=(0, 0), xytext=(6, 0), textcoords="offset points",
                                  color=c, fontsize=10, fontweight="bold", va="center",
                                  visible=False)
                  for k, c in (("cpu", CPU_C), ("ram", RAM_C))}
    waiting_text = ax1.text(0.5, 0.5, "Waiting for samples...", ha="center", va="center",
                            transform=ax1.transAxes, color=DIM, fontsize=12, visible=False)
    state: dict = {"fill": None, "fmt": None, "key": None, "archive": None}

    # Incremental reader: each refresh parses only rows appended since the last.
    incremental = _IncrementalSeriesReader(history_dir / "metrics.csv")

    def redraw() -> None:
        series = incremental.read()
        arch_t = arch_cols = None
        if include_archive.get():
            if state["archive"] is None:
                state["archive"] = _ArchiveSeries(history_dir, incremental)
            arch_t, arch_cols = state["archive"].get()
        window_min = _selected_window_minutes()

        # Nothing new and no setting changed: skip the whole render.
        key = (len(series), series.t[-1] if len(series) else None,
               None if arch_t is None else len(arch_t), window_min,
               tuple(v.get() for v in toggles.values()))
        if key == state["key"]:
            return
        state["key"] = key

        if len(series):
            newest = series.t[-1]
        elif arch_t is not None and len(arch_t):
            newest = float(arch_t[-1])
        else:
            newest = None
        total = len(series) + (0 if arch_t is None else len(arch_t))

        if newest is None or total < 2:
            for artist in (*lines.values(), *dots.values()):
                artist.set_data([], [])
            for lbl in end_labels.values():
                lbl.set_visible(False)
            if state["fill"] is not None:
                state["fill"].remove()
                state["fill"] = None
            waiting_text.set_visible(True)
            status_var.set(tr("ממתין לדגימות…", "Waiting for samples…"))
            canvas.draw_idle()
            return
        waiting_text.set_visible(False)

        start = None if window_min is None else newest - window_min / 1440.0
        lo = 0 if start is None else bisect_left(series.t, start)
        t, cols = series.view(lo)
        if arch_t is not None and len(arch_t):
            a_lo = 0 if start is None else int(np.searchsorted(arch_t, start))
            t = np.concatenate([arch_t[a_lo:], t])
            cols = {k: np.concatenate([arch_cols[k][a_lo:], cols[k]]) for k in cols}
        latest = {k: float(v[-1]) for k, v in cols.items()}
        t, cols, raw_n = _window(t, cols, start)   # fresh arrays; views released

        for k, line in lines.items():
            v = cols[k]
            shown = (k not in toggles or toggles[k].get()) and not np.isnan(v).all()
            line.set_visible(shown)
            line.set_data(t, v) if shown else line.set_data([], [])

        # Subtle area fill under CPU only — gives a sense of "load", very faint
        if state["fill"] is not None:
            state["fill"].remove()
        state["fill"] = ax1.fill_between(t, cols["cpu"], 0, color=CPU_C, alpha=0.06,
                                         linewidth=0)

        t_last = float(t[-1])
        for k in ("cpu", "ram"):
            dots[k].set_data([t_last], [latest[k]])
            end_labels[k].xy = (t_last, latest[k])
            end_labels[k].set_text(f"  {latest[k]:.0f}%")
            end_labels[k].set_visible(True)

        # Force x-limits to the window bounds (so chart "scrolls" as time advances)
        if window_min is not None:
            xmin = newest - window_min / 1440.0
            xmax = t_last + max(2, window_min * 60 * 0.02) / 86400.0
        else:
            xmin = float(t[0])
            xmax = t_last + 2 / 86400.0
        ax1.set_xlim(xmin, xmax)
        ax2.relim(visible_only=True)
        ax2.autoscale_view(scalex=False)

        # Pick a sensible time format based on window length
        if window_min is None or window_min >= 1440:
            fmt = "%m-%d %H:%M"
        elif window_min >= 60:
            fmt = "%H:%M"
        else:
            fmt = "%H:%M:%S"
        if fmt != state["fmt"]:
            ax1.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
            # Tilted labels: ticks created later copy this from the first one.
            fig.autofmt_xdate(bottom=0.12)
            state["fmt"] = fmt
        canvas.draw_idle()

        disk_now, temp_now = latest["disk"], latest["temps"]
        now_txt = (f"CPU {latest['cpu']:.0f}%  RAM {latest['ram']:.0f}%"
                   + (f"  Disk {disk_now:.0f}%" if disk_now == disk_now else "")
                   + (f"  Temp {temp_now:.0f}°C" if temp_now == temp_now else ""))
        status_var.set(tr(
            f"רענון אחרון: {datetime.now().strftime('%H:%M:%S')}  ·  "
            f"{raw_n}/{total} נקודות בחלון  ·  עכשיו: {now_txt}",
            f"Last refresh: {datetime.now().strftime('%H:%M:%S')}  ·  "
            f"{raw_n}/{total} points in window  ·  now: {now_txt}",
        ))

    def tick() -> None:
        if not win.winfo_exists():
            return
        # Minimised or paused: nobody is looking, so don't render. The next
        # visible tick catches up in one go (only new rows are parsed).
        if not paused.get() and win.state() not in ("iconic", "withdrawn"):
            try:
                redraw()
            except Exception:
                log.exception("Live chart redraw failed")
        win.after(refresh_ms, tick)

    redraw()
    win.after(refresh_ms, tick)

    if parent is None:
        try:
            win.mainloop()
        except tk.TclError:
            pass

    return win
