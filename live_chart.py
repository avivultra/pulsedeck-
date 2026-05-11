"""Live performance chart — Tkinter window embedding matplotlib that
auto-refreshes every few seconds. Replaces the static PNG view.
"""
from __future__ import annotations

import csv
import logging
import re
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

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
    panel.title("התראות היום")
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
    tk.Label(hdr, text=f"התראות {today_str}", bg=PANEL, fg=FG,
             font=("Segoe UI", 12, "bold"), anchor="e").pack(fill="x")
    tk.Label(hdr, text=f"{len(events)} אירועים היום  ·  לחץ על אירוע לפתיחת פרטים מלאים",
             bg=PANEL, fg=DIM, font=("Segoe UI", 9), anchor="e").pack(fill="x", pady=(2, 0))
    tk.Frame(panel, bg=GRID, height=1).pack(fill="x")

    if not events:
        tk.Label(panel, text="אין התראות היום עדיין",
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
            messagebox.showerror("שגיאה", "לא הצלחתי לפתוח את חלון ההתראה.", parent=panel)

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
                 font=("Segoe UI", 10), anchor="e",
                 wraplength=420, justify="right").pack(fill="x", pady=(2, 0))
        meta = (f"CPU {ev['cpu_before']:.0f}% → {ev['cpu_after']:.0f}%   ·   "
                f"RAM {ev['ram_before']:.0f}% → {ev['ram_after']:.0f}%")
        tk.Label(text_col, text=meta, bg=PANEL, fg=DIM,
                 font=("Segoe UI", 9), anchor="e").pack(fill="x", pady=(2, 0))

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


def _read_rows(history_dir: Path, include_archive: bool, max_rows: int = 50_000) -> list[dict]:
    """Read CSV rows, sorted by unix_time, optionally merging archives.

    NOTE: full read — used as a fallback. For the hot path inside an open
    live-chart window, see `_IncrementalCsvReader` which only reads the
    appended tail since the last refresh.
    """
    try:
        from metric_history import iter_history_rows
        rows = iter_history_rows(history_dir, include_archive=include_archive)
    except Exception:
        log.exception("Failed to read history rows")
        return []
    return rows[-max_rows:] if len(rows) > max_rows else rows


class _IncrementalCsvReader:
    """Reads only the new tail of metrics.csv since the previous call.

    First call: reads the entire file. Subsequent calls: seek to the byte
    offset where the previous read stopped and parse just the appended rows.
    If the file shrinks (e.g. rotation moved old rows to an archive) the
    cache is invalidated and we re-read from scratch.

    The chart's max_rows window protects memory — once cached_rows exceeds
    it, we keep only the tail.
    """

    def __init__(self, csv_path: Path, max_rows: int = 50_000) -> None:
        self.csv_path = Path(csv_path)
        self.max_rows = max_rows
        self._offset = 0
        self._size = 0
        self._fieldnames: list[str] | None = None
        self._rows: list[dict] = []

    def _reset(self) -> None:
        self._offset = 0
        self._size = 0
        self._fieldnames = None
        self._rows = []

    def read(self) -> list[dict]:
        try:
            stat = self.csv_path.stat()
        except FileNotFoundError:
            self._reset()
            return []

        # File shrank (rotation) — invalidate everything
        if stat.st_size < self._size:
            log.debug("CSV shrank from %d to %d bytes; invalidating cache",
                      self._size, stat.st_size)
            self._reset()

        # No change since last read — return cached
        if stat.st_size == self._size and self._rows:
            return list(self._rows)

        try:
            with self.csv_path.open("r", encoding="utf-8", newline="") as f:
                if self._offset == 0:
                    # First read — parse header normally
                    reader = csv.DictReader(f)
                    self._fieldnames = list(reader.fieldnames or [])
                    for row in reader:
                        self._rows.append(row)
                    self._offset = f.tell()
                else:
                    # Tail read — seek past header + already-consumed rows
                    f.seek(self._offset)
                    new_reader = csv.DictReader(f, fieldnames=self._fieldnames)
                    for row in new_reader:
                        # Guard: empty trailing line
                        if not row or not any(row.values()):
                            continue
                        self._rows.append(row)
                    self._offset = f.tell()
        except OSError:
            log.exception("Failed to read CSV %s incrementally", self.csv_path)
            return list(self._rows)

        self._size = stat.st_size
        # Bound memory — keep only the tail
        if len(self._rows) > self.max_rows:
            self._rows = self._rows[-self.max_rows:]
        return list(self._rows)


def _parse_series(rows: list[dict]) -> dict:
    """Convert CSV rows into parallel lists for plotting."""
    times: list[datetime] = []
    cpu: list[float] = []
    ram: list[float] = []
    disk: list[float | None] = []
    swap: list[float | None] = []
    temps: list[float | None] = []

    for r in rows:
        ts_raw = (r.get("timestamp_iso") or "").strip()
        try:
            ts = datetime.fromisoformat(ts_raw)
        except (ValueError, TypeError):
            continue
        try:
            c = float(r.get("cpu_percent") or "")
            rm = float(r.get("ram_percent") or "")
        except (ValueError, TypeError):
            continue
        times.append(ts)
        cpu.append(c)
        ram.append(rm)
        for series, key in ((disk, "disk_percent"), (swap, "swap_percent"), (temps, "temp_celsius")):
            raw = (r.get(key) or "").strip()
            try:
                series.append(float(raw) if raw else None)
            except ValueError:
                series.append(None)
    return {"times": times, "cpu": cpu, "ram": ram, "disk": disk, "swap": swap, "temps": temps}


def open_live_chart(history_dir: Path, parent: tk.Misc | None = None,
                    refresh_ms: int = 2000) -> tk.Toplevel | tk.Tk | None:
    """Open the live chart window. Returns the window (or None on failure)."""
    try:
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.dates as mdates
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
    win.title("גרף ביצועים — חי")
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
    paused = tk.BooleanVar(value=False)

    # Time window options (label, minutes — None = show all)
    WINDOW_OPTIONS: list[tuple[str, int | None]] = [
        ("5 דקות אחרונות", 5),
        ("15 דקות אחרונות", 15),
        ("שעה אחרונה", 60),
        ("6 שעות אחרונות", 360),
        ("24 שעות אחרונות", 1440),
        ("הכל", None),
    ]
    window_label = tk.StringVar(value="15 דקות אחרונות")

    def _check(parent_w, text: str, var: tk.BooleanVar) -> tk.Checkbutton:
        return tk.Checkbutton(parent_w, text=text, variable=var,
                              bg=PANEL, fg=DIM, selectcolor=PANEL2,
                              activebackground=PANEL, activeforeground=FG,
                              font=("Segoe UI", 9), bd=0, highlightthickness=0,
                              command=lambda: redraw())

    # Right-side title (Hebrew)
    tk.Label(top, text="גרף ביצועים חי", bg=PANEL, fg=FG,
             font=("Segoe UI", 11, "bold"), anchor="e").pack(side="right", padx=(8, 0))

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

    _check(top, "ארכיון", include_archive).pack(side="left")
    _check(top, "Disk", show_disk).pack(side="left", padx=(6, 0))
    _check(top, "Swap", show_swap).pack(side="left", padx=(6, 0))
    _check(top, "Temp", show_temp).pack(side="left", padx=(6, 0))

    tk.Frame(top, bg=GRID, width=1, height=18).pack(side="left", padx=10)

    pause_btn = tk.Checkbutton(top, text="השהה רענון", variable=paused,
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

    alerts_btn = tk.Button(top, text="🔔 התראות אחרונות",
                           bg=PANEL, fg=FG,
                           activebackground=PANEL2, activeforeground=FG,
                           relief="flat", bd=0, font=("Segoe UI", 9, "bold"),
                           padx=8, pady=2, cursor="hand2",
                           command=_open_alerts_panel)
    alerts_btn.pack(side="left")

    status_var = tk.StringVar(value="טוען…")
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

    # Incremental reader for the common case (main CSV only, no archive merge).
    # Avoids re-parsing a 1.5 MB file every 2 seconds.
    incremental = _IncrementalCsvReader(history_dir / "metrics.csv", max_rows=50_000)

    def redraw() -> None:
        if include_archive.get():
            # Archive merge requires reading multiple files — bypass incremental
            rows = _read_rows(history_dir, include_archive=True)
        else:
            rows = incremental.read()
        series = _parse_series(rows)
        ax1.clear()
        ax2.clear()
        _style_axes()

        all_times = series["times"]
        if len(all_times) < 2:
            ax1.text(0.5, 0.5, "Waiting for samples...",
                     ha="center", va="center", transform=ax1.transAxes,
                     color=DIM, fontsize=12)
            status_var.set("ממתין לדגימות…")
            canvas.draw_idle()
            return

        # Window filtering: keep only points within the selected window
        from datetime import timedelta
        window_min = _selected_window_minutes()
        if window_min is not None:
            cutoff = all_times[-1] - timedelta(minutes=window_min)
            keep_idx = [i for i, t in enumerate(all_times) if t >= cutoff]
            if not keep_idx:
                keep_idx = [len(all_times) - 1]
        else:
            keep_idx = list(range(len(all_times)))

        def _slice(values):
            return [values[i] for i in keep_idx]

        t = _slice(all_times)
        cpu = _slice(series["cpu"])
        ram = _slice(series["ram"])
        disk_v = _slice(series["disk"])
        swap_v = _slice(series["swap"])
        temps_v = _slice(series["temps"])

        # Subtle area fill under CPU only — gives a sense of "load", very faint
        ax1.fill_between(t, cpu, 0, color=CPU_C, alpha=0.06, linewidth=0)

        # Crisp, thin lines — single pass, no glow
        ax1.plot(t, cpu, color=CPU_C, linewidth=1.3,
                 solid_capstyle="round", solid_joinstyle="round", label="CPU")
        ax1.plot(t, ram, color=RAM_C, linewidth=1.3,
                 solid_capstyle="round", solid_joinstyle="round", label="RAM")

        if show_disk.get() and any(v is not None for v in disk_v):
            disk_clean = [v if v is not None else float("nan") for v in disk_v]
            ax1.plot(t, disk_clean, color=DISK_C, linewidth=1.0,
                     alpha=0.7, label="Disk")
        if show_swap.get() and any(v is not None for v in swap_v):
            swap_clean = [v if v is not None else float("nan") for v in swap_v]
            ax1.plot(t, swap_clean, color=SWAP_C, linewidth=1.0,
                     alpha=0.7, label="Swap")
        if show_temp.get() and any(v is not None for v in temps_v):
            temps_clean = [v if v is not None else float("nan") for v in temps_v]
            ax2.plot(t, temps_clean, color=TEMP_C, linewidth=1.0,
                     alpha=0.85, label="Temp")

        # Last-point markers: small filled dot, no white sparkle
        for value, color in ((cpu[-1], CPU_C), (ram[-1], RAM_C)):
            ax1.scatter([t[-1]], [value], color=color, s=28, zorder=5,
                        edgecolors="none")

        # Inline labels at the end of each line — Vercel/Tremor style:
        # plain colored text floating to the right of the latest point, no box
        for value, color in ((cpu[-1], CPU_C), (ram[-1], RAM_C)):
            ax1.annotate(
                f"  {value:.0f}%",
                xy=(t[-1], value),
                xytext=(6, 0), textcoords="offset points",
                color=color, fontsize=10, fontweight="bold",
                va="center",
            )

        # Force x-limits to the window bounds (so chart "scrolls" as time advances)
        if window_min is not None:
            xmin = t[-1] - timedelta(minutes=window_min)
            xmax = t[-1] + timedelta(seconds=max(2, window_min * 60 * 0.02))
        else:
            xmin = t[0]
            xmax = t[-1] + timedelta(seconds=2)
        ax1.set_xlim(xmin, xmax)

        # Vercel-style: small subtitle in muted gray + big inline metrics
        # Show only the subtitle as the chart's matplotlib title; the live
        # values are conveyed by the inline end-of-line labels above.
        ax1.set_title(
            "system metrics",
            color=DIM, fontsize=10, fontweight="normal",
            pad=12, loc="left",
        )
        ax1.set_xlabel("")
        # Pick a sensible time format based on window length
        if window_min is None or window_min >= 1440:
            ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        elif window_min >= 60:
            ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        else:
            ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

        # No matplotlib legend — inline labels at line ends do the job
        if ax1.get_legend() is not None:
            ax1.get_legend().remove()

        fig.autofmt_xdate()
        fig.subplots_adjust(left=0.05, right=0.93, top=0.90, bottom=0.12)
        canvas.draw_idle()

        latest = (f"CPU {cpu[-1]:.0f}%  RAM {ram[-1]:.0f}%"
                  + (f"  Disk {disk_v[-1]:.0f}%" if disk_v[-1] is not None else "")
                  + (f"  Temp {temps_v[-1]:.0f}°C" if temps_v[-1] is not None else ""))
        status_var.set(
            f"רענון אחרון: {datetime.now().strftime('%H:%M:%S')}  ·  "
            f"{len(t)}/{len(all_times)} נקודות בחלון  ·  עכשיו: {latest}"
        )

    def tick() -> None:
        if not win.winfo_exists():
            return
        if not paused.get():
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
