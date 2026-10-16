"""The vault window: PulseDeck's main window, in the style of an Obsidian vault.

Left to right (mirrored in Hebrew): a ribbon, a file tree of everything on the
computer, the graph (every bubble is something real: a program, a resource,
an event, an abandoned process), and the note for the selected bubble. A
status bar at the bottom. F11 or the ⛶ button toggles full screen.

Cost: the graph's physics runs only while bubbles are moving and goes to sleep
when they settle; data refreshes every 2 s and only bubbles whose look changed
get a new image; nothing runs while the window is minimised.
"""

from __future__ import annotations

import logging
import os
import re
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path

import psutil

import i18n
from i18n import tr
from vault_graph import Layout, build_graph, group_programs, pad_of, pick_programs
from vault_sprites import RES_COLORS, SpriteCache, blend, hex_of

log = logging.getLogger(__name__)

# Obsidian's default dark palette
BG = "#1e1e1e"
SIDE = "#262626"
RIBBON = "#181818"
HOVER = "#333333"
ACTIVE = "#3a3a3a"
LINE = "#333333"
FG = "#dadada"
FG_SOFT = "#b3b3b3"
FG_FAINT = "#6f6f6f"
ACCENT = "#a882ff"
ACCENT_SOFT = "#2e2640"
WARN = "#fb464c"
WARN_SOFT = "#3a2422"
OK = "#44cf6e"
OK_SOFT = "#1f2e24"
CLAUDE = "#d97757"
FONT = "Segoe UI"
MONO = "Consolas"

REFRESH_MS = 2000
FRAME_MS = 33
FIT_MARGIN = 36        # px kept free around the graph when fitting
FIT_K_MAX = 1.6
FIT_K_SNAP = 0.06      # auto view re-zooms only when off by more than this


def room_k(canvas_height: int) -> float:
    """Bubble zoom to aim for: bigger on a taller canvas, the rest of the room
    is filled by spreading the layout (Layout.adapt_to), not by giant bubbles."""
    return min(1.5, max(0.9, canvas_height / 700))
_PROTECTED_CLOSE = {"explorer", "msmpeng", "pulsedeck", "csrss", "wininit", "winlogon",
                    "services", "lsass", "smss", "dwm", "svchost"}

# What common programs are, in one line (shown on their note).
ABOUT = {
    "chrome": ("דפדפן Google Chrome. כל לשונית ותוסף רצים כתהליך נפרד, ולכן יש הרבה.",
               "Google Chrome. Every tab and extension runs as its own process, so there are many."),
    "msedge": ("דפדפן Microsoft Edge. כמו Chrome, כל לשונית היא תהליך.",
               "Microsoft Edge. Like Chrome, every tab is a process."),
    "firefox": ("דפדפן Firefox.", "The Firefox browser."),
    "Code": ("Visual Studio Code, עורך הקוד. התוספים שלו מריצים תהליכים ברקע.",
             "Visual Studio Code. Its extensions run helper processes in the background."),
    "Teams": ("Microsoft Teams לשיחות ופגישות. תופס זיכרון גם כשהוא ממוזער.",
              "Microsoft Teams. Holds memory even when minimised."),
    "ms-teams": ("Microsoft Teams לשיחות ופגישות.", "Microsoft Teams."),
    "node": ("Node.js, מריץ קוד JavaScript. בדרך כלל נפתח מתוך עורך קוד או כלי פיתוח.",
             "Node.js runs JavaScript, usually started by an editor or a dev tool."),
    "MsMpEng": ("Windows Defender, האנטי-וירוס של Windows. לא סוגרים אותו.",
                "Windows Defender, the built-in antivirus. Leave it running."),
    "explorer": ("סייר הקבצים של Windows, וגם שורת המשימות. כמעט כל תוכנה נפתחת ממנו.",
                 "Windows Explorer, which is also the taskbar. Most programs are started from it."),
    "Discord": ("Discord לצ'אט וקול.", "Discord, for chat and voice."),
    "Spotify": ("Spotify למוזיקה.", "Spotify, for music."),
    "OneDrive": ("סנכרון קבצים לענן של Microsoft.", "Microsoft's cloud file sync."),
    "WindowsTerminal": ("חלון הטרמינל של Windows.", "The Windows Terminal."),
    "claude": ("Claude Code או אפליקציית Claude.", "Claude Code or the Claude app."),
    "svchost": ("תהליך מערכת של Windows שמריץ שירותים. לא סוגרים.",
                "A Windows system process that hosts services. Leave it running."),
    "dwm": ("מנהל החלונות של Windows, מצייר את המסך. לא סוגרים.",
            "The Windows window manager; it draws the screen. Leave it running."),
    "PulseDeck": ("הכלי הזה. מוצג כדי שתראה כמה הוא עצמו עולה למחשב.",
                  "This tool, shown so you can see what it costs the computer."),
}


def _fmt_bytes(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


def _short_rate(bps: float) -> str:
    """Fits inside a hexagon: 340K, 1.2M."""
    if bps >= 1024 ** 2:
        return f"{bps / 1024 ** 2:.1f}M"
    if bps >= 1024:
        return f"{bps / 1024:.0f}K"
    return f"{bps:.0f}B"


def _fmt_rate(bps: float) -> str:
    for unit, div in (("MB/s", 1024 ** 2), ("KB/s", 1024)):
        if bps >= div:
            return f"{bps / div:.1f} {unit}"
    return f"{bps:.0f} B/s"


def _event_reason(reason: str) -> list[str]:
    """A load event's reasons, one short line each, in the window's language.
    The spike log writes "CPU"/"RAM" inside Hebrew sentences; a Latin word in
    a Hebrew line is what Windows lays out in the wrong order, so Hebrew says
    מעבד/זיכרון. The "(from x% to y%)" part is dropped: it made the line wrap,
    and the event page shows before → after right under it."""
    if i18n.is_rtl():
        reason = reason.replace("CPU", "מעבד").replace("RAM", "זיכרון")
    return [p for p in (re.sub(r"\s*\([^)]*\)", "", part).strip() for part in reason.split(" · ")) if p]


def _event_line(d: dict) -> str:
    """One short line per load event ("01:57:32 · מעבד 82%"), short enough
    not to wrap: a wrapped mixed line is laid out line by line and scrambles.
    The full reason is on the event's own page."""
    raw = d.get("raw") or {}
    ram = d.get("res") == "ram"
    level = raw.get("ram_after" if ram else "cpu_after") or 0
    if level:
        what = (tr(f"זיכרון {level:.0f}%", f"Memory {level:.0f}%") if ram
                else tr(f"מעבד {level:.0f}%", f"CPU {level:.0f}%"))
    else:
        what = (_event_reason(d.get("reason", "")) or [""])[0]
    return i18n.bidi(f"{d.get('time', '')} · {what}")


def open_vault(history_dir: Path, parent: tk.Misc | None = None) -> tk.Toplevel | tk.Tk | None:
    try:
        return VaultWindow(Path(history_dir), parent).win
    except Exception:
        log.exception("Could not open the vault window")
        return None


class VaultWindow:
    def __init__(self, history_dir: Path, parent: tk.Misc | None) -> None:
        from monitor import disk_root_path

        self.history_dir = history_dir
        self.win = tk.Tk() if parent is None else tk.Toplevel(parent)
        self.win.title(tr("PulseDeck — המחשב שלי", "PulseDeck — My computer"))
        self.win.geometry("1240x780")
        self.win.minsize(820, 560)
        self.win.configure(bg=BG)
        self.side = i18n.SIDE()          # reading-start edge
        self.side_end = i18n.SIDE_END()
        self.anchor = i18n.ANCHOR()
        self.justify = i18n.JUSTIFY()

        self.layout = Layout()
        self.sprites = SpriteCache()
        self.view = {"x": 0.0, "y": 0.0, "k": 1.0}
        self.items: dict[str, dict] = {}     # node key -> canvas item ids + sprite key
        self.hover: str | None = None
        self.selected: str | None = None
        self.drag: dict | None = None
        self.full = False
        self.disk_path = disk_root_path()
        self._ppid_cache: dict[int, int | None] = {}
        self._net_last: tuple[float, int] | None = None
        self._events_sig = None
        self._events: list[dict] = []
        self._ghosts: list = []
        self._groups: list = []
        self._snap = None
        self._animating = False
        self._fitted = False
        # The view follows the graph (fits it to the canvas) until the user
        # zooms or pans; "⤢ Fit", the graph button and F11 hand it back.
        self._auto_view = True
        self._lines: list[int] = []          # reusable line items, one per visible link
        self._drawn: dict[int, dict] = {}    # canvas item -> what was last sent to Tk

        self._build_ui()
        self.win.bind("<F11>", lambda _e: self.toggle_full())
        self.win.bind("<Escape>", lambda _e: self.toggle_full(False) if self.full else None)
        self.refresh()
        self.layout.settle(600)
        self.win.after(60, self.fit)
        self.win.after(500, self.fit)      # again once the window has its real size
        self.win.after(REFRESH_MS, self._tick)
        if parent is None:
            self.win.mainloop()

    # ================================================================ UI
    def _label(self, master, text="", fg=FG, size=10, bold=False, **kw) -> tk.Label:
        return tk.Label(master, text=text, fg=fg, bg=kw.pop("bg", master["bg"]),
                        font=(FONT, size, "bold" if bold else "normal"), **kw)

    def _build_ui(self) -> None:
        w = self.win
        self.status = tk.Frame(w, bg=RIBBON, height=26)
        self.status.pack(side="bottom", fill="x")
        tk.Frame(w, bg=LINE, height=1).pack(side="bottom", fill="x")

        self.ribbon = tk.Frame(w, bg=RIBBON, width=44)
        self.ribbon.pack(side=self.side, fill="y")
        for glyph, tip, cmd in (("◈", tr("גרף", "Graph"), self.fit),
                                ("⌕", tr("חיפוש", "Search"), lambda: self.search.focus_set()),
                                ("✳", tr("שאל את Claude", "Ask Claude"), self.ask_claude),
                                ("⏱", tr("למה המחשב היה איטי?", "Why was it slow?"), lambda: self.open_slow()),
                                ("↻", tr("סדר מחדש", "Rearrange"), self.shake),
                                ("⛶", tr("מסך מלא (F11)", "Full screen (F11)"), self.toggle_full)):
            b = self._label(self.ribbon, glyph, fg=ACCENT if glyph == "◈" else FG_FAINT, size=13,
                            cursor="hand2", width=2, pady=4)
            b.pack(pady=3)
            b.bind("<Button-1>", lambda _e, c=cmd: c())
            b.bind("<Enter>", lambda _e, b=b: b.configure(bg=HOVER))
            b.bind("<Leave>", lambda _e, b=b: b.configure(bg=RIBBON))

        self.sidebar = tk.Frame(w, bg=SIDE, width=230)
        self.sidebar.pack(side=self.side, fill="y")
        self.sidebar.pack_propagate(False)
        self._label(self.sidebar, tr("המחשב שלי", "My computer"), size=10, bold=True,
                    anchor=self.anchor).pack(fill="x", padx=14, pady=(12, 6))
        self.search = tk.Entry(self.sidebar, bg="#1f1f1f", fg=FG, insertbackground=FG, relief="flat",
                               font=(FONT, 10), justify=self.justify, highlightthickness=1,
                               highlightbackground=LINE, highlightcolor=ACCENT)
        self.search.pack(fill="x", padx=12, pady=(0, 8), ipady=4)
        self.search.bind("<KeyRelease>", self._on_search)
        self.tree = tk.Text(self.sidebar, bg=SIDE, fg=FG_SOFT, relief="flat", wrap="none",
                            font=(FONT, 10), cursor="arrow", padx=6, pady=4, highlightthickness=0,
                            spacing1=2, spacing3=2)
        self.tree.pack(fill="both", expand=True)

        self.note_frame = tk.Frame(w, bg=BG, width=340)
        self.note_frame.pack(side=self.side_end, fill="y")
        self.note_frame.pack_propagate(False)
        tk.Frame(w, bg=LINE, width=1).pack(side=self.side_end, fill="y")
        self.note = tk.Text(self.note_frame, bg=BG, fg=FG_SOFT, relief="flat", wrap="word",
                            font=(FONT, 10), cursor="arrow", padx=20, pady=18, highlightthickness=0,
                            spacing1=1, spacing3=3)
        self.note.pack(fill="both", expand=True)
        self._note_tags()

        main = tk.Frame(w, bg=BG)
        main.pack(side=self.side, fill="both", expand=True)
        tabs = tk.Frame(main, bg=RIBBON, height=32)
        tabs.pack(fill="x")
        tab = self._label(tabs, "◈ " + tr("גרף המחשב", "Computer graph"), bg=BG, size=9,
                          padx=14, pady=6)
        tab.pack(side=self.side, padx=8, pady=(4, 0))
        self.canvas = tk.Canvas(main, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._build_controls()
        self._build_status()
        c = self.canvas
        c.bind("<Configure>", self._on_resize)
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<B1-Motion>", self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", lambda _e: self._set_hover(None))
        c.bind("<MouseWheel>", lambda e: self._zoom(e.x, e.y, 1.12 if e.delta > 0 else 1 / 1.12))
        c.bind("<Button-4>", lambda e: self._zoom(e.x, e.y, 1.12))
        c.bind("<Button-5>", lambda e: self._zoom(e.x, e.y, 1 / 1.12))
        self.tip_bg = c.create_rectangle(0, 0, 0, 0, fill=SIDE, outline=LINE, state="hidden", tags="tip")
        self.tip = c.create_text(0, 0, text="", fill=FG, anchor="nw", font=(FONT, 9),
                                 state="hidden", tags="tip", justify=self.justify)

    def _build_controls(self) -> None:
        box = tk.Frame(self.canvas, bg=SIDE, padx=10, pady=8, highlightthickness=1,
                       highlightbackground=LINE)
        self._label(box, tr("מה להציג", "Show"), size=9, bold=True, anchor=self.anchor).pack(fill="x")
        self.filters = {}
        for key, text in (("res", tr("קישורים למשאבים", "Links to resources")),
                          ("tree", tr("מי פתח את מי", "Who started whom")),
                          ("event", tr("אירועי עומס", "Load events")),
                          ("ghost", tr("תהליכים נטושים", "Abandoned processes"))):
            var = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(box, text=text, variable=var, bg=SIDE, fg=FG_SOFT, selectcolor=ACTIVE,
                                activebackground=SIDE, activeforeground=FG, font=(FONT, 9), bd=0,
                                highlightthickness=0, anchor=self.anchor,
                                command=self._apply_filters)
            cb.pack(fill="x")
            self.filters[key] = var
        row = tk.Frame(box, bg=SIDE)
        row.pack(fill="x", pady=(6, 0))
        for text, cmd in ((tr("↻ סדר", "↻ Shake"), self.shake), (tr("⤢ התאם", "⤢ Fit"), self.fit),
                          (tr("⛶ מסך מלא", "⛶ Full"), self.toggle_full)):
            b = self._label(row, text, size=9, bg=HOVER, padx=7, pady=2, cursor="hand2")
            b.pack(side=self.side, padx=(0, 4) if self.side == "left" else (4, 0))
            b.bind("<Button-1>", lambda _e, c=cmd: c())
        legend = self._label(box, tr("● תוכנה, גודל = זיכרון   ◔ טבעת = מעבד\n⬢ משאב   ◆ אירוע   ◌ נטוש",
                                     "● program, size = memory   ◔ ring = CPU\n⬢ resource   ◆ event   ◌ abandoned"),
                             fg=FG_FAINT, size=8, justify=self.justify, anchor=self.anchor)
        legend.pack(fill="x", pady=(8, 0))
        self.controls = box
        box.place(relx=0 if self.side == "left" else 1, x=12 if self.side == "left" else -12, y=12,
                  anchor="nw" if self.side == "left" else "ne")

    def _build_status(self) -> None:
        self.status_vars = {k: tk.StringVar() for k in ("verdict", "cpu", "ram", "temp", "ghosts", "self")}
        self._status_dot = tk.Canvas(self.status, width=10, height=10, bg=RIBBON, highlightthickness=0)
        self._status_dot_item = self._status_dot.create_oval(2, 2, 9, 9, fill=ACCENT, outline="")
        widgets = [self._status_dot]
        for key in ("verdict", "cpu", "ram", "temp", "ghosts"):
            widgets.append(self._label(self.status, textvariable=self.status_vars[key],
                                       fg=FG_SOFT if key == "verdict" else FG_FAINT, size=8))
        for wdg in widgets:
            wdg.pack(side=self.side_end, padx=6)
        self._label(self.status, textvariable=self.status_vars["self"], fg=FG_FAINT, size=8).pack(
            side=self.side, padx=10)

    # ================================================================ data
    def _sampler(self):
        try:
            from process_monitor import get_default_sampler, peek_default_sampler
            s = peek_default_sampler() or get_default_sampler()
            s.notify_activity()
            return s
        except Exception:
            log.exception("Process sampler unavailable")
            return None

    def _parent_of(self, pid: int) -> int | None:
        if pid not in self._ppid_cache:
            try:
                self._ppid_cache[pid] = psutil.Process(pid).ppid()
            except (psutil.Error, OSError):
                self._ppid_cache[pid] = None
        return self._ppid_cache[pid]

    def _read_events(self) -> list[dict]:
        from live_chart import _read_today_spikes
        from metric_history import DEFAULT_SPIKES_DIR
        target = DEFAULT_SPIKES_DIR / f"spikes-{datetime.now():%Y-%m-%d}.md"
        try:
            st = target.stat()
            sig = (target.name, st.st_size, st.st_mtime)
        except OSError:
            sig = (target.name, None, None)
        if sig != self._events_sig:
            self._events_sig = sig
            self._events = _read_today_spikes(DEFAULT_SPIKES_DIR)
        return self._events

    def refresh(self) -> None:
        from monitor import collect_snapshot
        from self_usage import self_usage
        from temperature_readings import read_gpu_temp_celsius

        snap = collect_snapshot(self.disk_path)
        self._snap = snap
        sampler = self._sampler()
        procs = sampler.snapshot() if sampler else []
        names = {p.pid: p.name for p in procs}
        live = {p.pid for p in procs}
        self._ppid_cache = {pid: v for pid, v in self._ppid_cache.items() if pid in live}
        groups = group_programs(procs, self._parent_of,
                                lambda pid: names.get(pid) if pid else None, os.getpid())
        self_pct, self_rss = self_usage()
        for g in groups:
            if g.is_self:
                g.cpu, g.rss = self_pct, self_rss
        self._groups = groups
        programs = pick_programs(groups)

        now = time.monotonic()
        io = psutil.net_io_counters()
        total = io.bytes_sent + io.bytes_recv
        rate = 0.0
        if self._net_last is not None and now > self._net_last[0]:
            rate = max(0.0, (total - self._net_last[1]) / (now - self._net_last[0]))
        self._net_last = (now, total)
        gpu_t = read_gpu_temp_celsius()
        resources = [
            {"id": "cpu", "label": tr("מעבד", "CPU"), "value": snap.cpu_percent,
             "text": f"{snap.cpu_percent:.0f}%", "sub": tr(f"{snap.cpu_logical} ליבות", f"{snap.cpu_logical} cores")},
            {"id": "ram", "label": tr("זיכרון", "Memory"), "value": snap.ram_percent,
             "text": f"{snap.ram_percent:.0f}%", "sub": f"{_fmt_bytes(snap.ram_used)} / {_fmt_bytes(snap.ram_total)}"},
        ]
        if snap.disk_percent is not None:
            free = (snap.disk_total or 0) - (snap.disk_used or 0)
            resources.append({"id": "disk", "label": tr("דיסק", "Disk"), "value": snap.disk_percent,
                              "text": f"{snap.disk_percent:.0f}%",
                              "sub": f"{snap.disk_path}  {_fmt_bytes(free)} " + tr("פנויים", "free")})
        resources.append({"id": "net", "label": tr("רשת", "Network"), "value": min(100.0, rate / 1024 ** 2 * 10),
                          "text": _short_rate(rate), "sub": _fmt_rate(rate)})
        if snap.temp_celsius is not None or gpu_t is not None:
            t = snap.temp_celsius if snap.temp_celsius is not None else gpu_t
            resources.append({"id": "temp", "label": tr("חום", "Temperature"), "value": t,
                              "text": f"{t:.0f}°", "sub": f"GPU {gpu_t:.0f}°" if gpu_t is not None and snap.temp_celsius is not None else ""})

        ghosts = []
        try:
            from ghost_sweeper import humanize_duration, peek_default_sweeper
            sw = peek_default_sweeper()
            self._ghosts = sw.get_ghosts()[:8] if sw else []
            for gh in self._ghosts:
                ghosts.append({"name": gh.name.removesuffix(".exe"), "pid": gh.pid, "rss": gh.rss_bytes,
                               "idle": humanize_duration(gh.idle_for_seconds),
                               "parent": gh.parent_name.removesuffix(".exe") if gh.parent_name else None,
                               "detail": gh.verdict_detail, "create_time": gh.create_time})
        except Exception:
            log.debug("Ghost sweeper unavailable", exc_info=True)

        events = []
        for ev in self._read_events()[-6:]:
            res = "ram" if "RAM" in ev["reason"] and "CPU" not in ev["reason"] else "cpu"
            events.append({"time": ev["time"], "reason": ev["reason"], "res": res,
                           "suspects": [n for n, _s in ev.get("suspects", [])], "raw": ev})

        nodes, links = build_graph(programs, resources, ghosts, events, tr("המחשב שלי", "My computer"))
        self.layout.update(nodes, links)
        if self.selected and self.selected != "slow" and self.selected not in nodes:
            self.selected = None
        self._update_status(snap)
        self._fill_tree()
        if self.selected is None or self.selected == "pc":
            self.show_note(self.selected)
        self._adapt_room()
        self._follow()
        self.render()
        self._kick()

    def _tick(self) -> None:
        if not self.win.winfo_exists():
            return
        if self.win.state() not in ("iconic", "withdrawn"):
            try:
                self.refresh()
            except Exception:
                log.exception("Vault refresh failed")
        self.win.after(REFRESH_MS, self._tick)

    def _verdict(self):
        from live_chart import _verdict
        s = self._snap
        return _verdict(s.cpu_percent, s.ram_percent, s.temp_celsius)

    def _update_status(self, snap) -> None:
        color, text, _issues = self._verdict()
        from live_chart import GREEN
        self._status_dot.itemconfigure(self._status_dot_item, fill=ACCENT if color == GREEN else color)
        v = self.status_vars
        v["verdict"].set(text)
        v["cpu"].set(tr(f"מעבד {snap.cpu_percent:.0f}%", f"CPU {snap.cpu_percent:.0f}%"))
        v["ram"].set(tr(f"זיכרון {snap.ram_percent:.0f}%", f"Memory {snap.ram_percent:.0f}%"))
        v["temp"].set("" if snap.temp_celsius is None else tr(f"חום {snap.temp_celsius:.0f}°", f"Temp {snap.temp_celsius:.0f}°"))
        v["ghosts"].set(f"👻 {len(self._ghosts)}" if self._ghosts else "")
        from self_usage import format_self_usage
        me = next((g for g in self._groups if g.is_self), None)
        if me is not None:
            v["self"].set("PulseDeck: " + format_self_usage(me.cpu, me.rss))

    # ================================================================ view
    def _size(self) -> tuple[int, int]:
        return max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        w, h = self._size()
        v = self.view
        return w / 2 + v["x"] + x * v["k"], h / 2 + v["y"] + y * v["k"]

    def to_world(self, sx: float, sy: float) -> tuple[float, float]:
        w, h = self._size()
        v = self.view
        return (sx - w / 2 - v["x"]) / v["k"], (sy - h / 2 - v["y"]) / v["k"]

    def _fit_target(self) -> tuple[float, float, float] | None:
        """(k, x, y) that shows the whole graph, or None before the canvas has a size."""
        x0, y0, x1, y1 = self.layout.bounds()
        w, h = self._size()
        if w < 50:
            return None
        k = min(FIT_K_MAX, max(0.35, min((w - 2 * FIT_MARGIN) / (x1 - x0),
                                         (h - 2 * FIT_MARGIN) / (y1 - y0))))
        return k, -(x0 + x1) / 2 * k, -(y0 + y1) / 2 * k

    def fit(self) -> None:
        """Show the whole graph now, and keep following it (auto view)."""
        self._auto_view = True
        w, h = self._size()
        if w >= 50 and self.drag is None:
            # Converge the layout to this canvas off-screen (a few hundred
            # physics steps, bounded in time) so it opens already spread out.
            deadline = time.perf_counter() + 0.35
            while time.perf_counter() < deadline:
                self.layout.settle(250)
                if not self.layout.adapt_to(w - 2 * FIT_MARGIN, h - 2 * FIT_MARGIN, room_k(h)):
                    break
            self._kick()
        target = self._fit_target()
        if target is None:
            return
        k, x, y = target
        self.view.update(k=k, x=x, y=y)
        self._fitted = True
        self.render()

    def _follow(self) -> None:
        """Auto view: glide toward the fitted view as the graph moves.

        The pan eases; the zoom only changes when it is off by more than
        FIT_K_SNAP, because every new zoom level re-renders every bubble sprite.
        """
        if not self._auto_view or self.drag is not None:
            return
        target = self._fit_target()
        if target is None:
            return
        k, x, y = target
        v = self.view
        if abs(k - v["k"]) > FIT_K_SNAP * v["k"]:
            # Re-centre at the new zoom from where the old one was looking.
            v["x"], v["y"] = v["x"] / v["k"] * k, v["y"] / v["k"] * k
            v["k"] = k
        else:
            x, y = x / k * v["k"], y / k * v["k"]
        v["x"] += (x - v["x"]) * 0.2
        v["y"] += (y - v["y"]) * 0.2

    def _adapt_room(self) -> None:
        """Spread the layout to the canvas: on a big or wide window the graph
        uses the room instead of sitting small in the middle (auto view only)."""
        if not self._auto_view or self.drag is not None:
            return
        w, h = self._size()
        if w < 50:
            return
        if self.layout.adapt_to(w - 2 * FIT_MARGIN, h - 2 * FIT_MARGIN, room_k(h)):
            self._kick()

    def _on_resize(self, _e=None) -> None:
        self.render()
        if self._auto_view:
            # One re-fit once the resize settles, not one per Configure event.
            if getattr(self, "_resize_job", None):
                self.win.after_cancel(self._resize_job)
            self._resize_job = self.win.after(250, self._refit_after_resize)

    def _refit_after_resize(self) -> None:
        self._resize_job = None
        if self._auto_view:
            self.fit()

    def _zoom(self, sx, sy, factor) -> None:
        self._auto_view = False
        wx, wy = self.to_world(sx, sy)
        w, h = self._size()
        k = min(3.0, max(0.35, self.view["k"] * factor))
        self.view.update(k=k, x=sx - w / 2 - wx * k, y=sy - h / 2 - wy * k)
        self.render()

    def _neighbors(self, key: str) -> set[str]:
        out = {key}
        for link in self.layout.visible_links():
            if link.a == key:
                out.add(link.b)
            elif link.b == key:
                out.add(link.a)
        return out

    def _cfg(self, item: int, **cfg) -> None:
        """itemconfigure only the options that changed since the last frame:
        every call costs a Tk round trip, and text items are re-laid-out."""
        last = self._drawn.setdefault(item, {})
        diff = {k: v for k, v in cfg.items() if last.get(k) is not v and last.get(k) != v}
        if diff:
            self.canvas.itemconfigure(item, **diff)
            last.update(diff)

    def _move(self, item: int, *xy: float) -> None:
        """coords only when the item moved at least half a pixel. Tk repaints the
        area of every moved item, and the alpha-blended bubbles are the costly part."""
        last = self._drawn.setdefault(item, {})
        old = last.get("xy")
        if old is None or max(abs(p - q) for p, q in zip(old, xy)) >= 0.5:
            self.canvas.coords(item, *xy)
            last["xy"] = xy

    def _forget(self, item: int) -> None:
        self.canvas.delete(item)
        self._drawn.pop(item, None)

    def render(self) -> None:
        c = self.canvas
        if c.winfo_width() < 20:
            return
        w, h = self._size()
        v = self.view
        k, ox, oy = v["k"], w / 2 + v["x"], h / 2 + v["y"]
        focus = self.hover or self.selected
        if focus not in self.layout.nodes:     # e.g. the "why was it slow?" page
            focus = None
        near = self._neighbors(focus) if focus else None
        nodes = self.layout.nodes
        links = self.layout.visible_links()
        # Line items are reused frame to frame (moved, restyled only on change)
        # instead of being deleted and re-created 30 times a second.
        lines = self._lines
        while len(lines) < len(links):
            lines.append(c.create_line(0, 0, 0, 0, tags="link"))
            c.tag_lower(lines[-1])
        while len(lines) > len(links):
            self._forget(lines.pop())
        for item, link in zip(lines, links):
            a, b = nodes[link.a], nodes[link.b]
            x1, y1 = ox + a.x * k, oy + a.y * k
            x2, y2 = ox + b.x * k, oy + b.y * k
            on = near is not None and focus in (link.a, link.b)
            if link.type == "res":
                rgb = RES_COLORS.get(b.data.get("id", ""), (120, 120, 120))
                alpha, width = 0.30, 0.6 + link.w * 2.4
            elif link.type == "event":
                rgb, alpha, width = (251, 70, 76), 0.6, 1
            elif link.type == "tree":
                rgb, alpha, width = (110, 110, 110), 0.6, 1
            else:
                rgb, alpha, width = (90, 90, 90), 0.6, 1
            if near is not None:
                alpha = 0.95 if on else 0.07
            self._move(item, x1, y1, x2, y2)
            self._cfg(item, fill=blend(rgb, alpha), width=max(1, width * k ** 0.5),
                      dash=(4, 4) if link.type in ("event", "ghost") else ())

        seen = set()
        for n in self.layout.visible_nodes():
            seen.add(n.key)
            x, y = ox + n.x * k, oy + n.y * k
            dim = near is not None and n.key not in near
            is_focus = n.key == focus
            kw = {"dim": dim, "focus": is_focus}
            if n.kind == "prog":
                g = n.data["group"]
                kw.update(cpu=g.cpu, count=g.count, is_self=g.is_self)
            elif n.kind == "res":
                kw.update(frac=min(1.0, n.data["value"] / 100), res=n.data["id"])
            img = self.sprites.get(n.kind, n.r * k, k, **kw)
            it = self.items.get(n.key)
            if it is None:
                it = self.items[n.key] = {
                    "img": c.create_image(x, y, tags=("node",)),
                    "label": c.create_text(x, y, text="", fill=FG_SOFT, font=(FONT, 9), anchor="n",
                                           tags=("node",)),
                    "value": c.create_text(x, y, text="", fill=FG, font=(FONT, 8, "bold"), tags=("node",)),
                }
            self._move(it["img"], x, y)
            self._cfg(it["img"], image=img, state="normal")
            it["photo"] = img        # keep a reference for Tk
            # value inside resource hexagons, ghost icon
            if n.kind == "res":
                col = hex_of(RES_COLORS.get(n.data["id"], (200, 200, 200)))
                self._cfg(it["value"], text=n.data["text"], state="normal",
                          fill=blend(tuple(int(col[i:i + 2], 16) for i in (1, 3, 5)), 0.22 if dim else 1),
                          font=(FONT, max(7, int(9 * min(1.4, k))), "bold"))
                self._move(it["value"], x, y)
            elif n.kind == "ghost":
                self._cfg(it["value"], text="👻", state="normal", font=(FONT, max(7, int(n.r * k * 0.8))))
                self._move(it["value"], x, y)
            else:
                self._cfg(it["value"], state="hidden")
            show_label = (n.kind == "res" or n.r >= 20 or k > 1.15 or is_focus or (near and n.key in near)
                          or n.kind in ("event", "claude"))
            if show_label and n.label:
                extra = pad_of(n) - n.r
                self._move(it["label"], x, y + (n.r + extra) * k + 4)
                fill = "#ffffff" if is_focus else ("#ff8a8a" if n.kind == "event" else FG_SOFT)
                self._cfg(it["label"], text=n.label, state="normal",
                          fill=blend((0x6f, 0x6f, 0x6f), 0.4) if dim else fill,
                          font=(FONT, max(8, int(9 * min(1.3, k))), "bold" if n.kind == "res" else "normal"))
            else:
                self._cfg(it["label"], state="hidden")
        for key in [k_ for k_ in self.items if k_ not in seen]:
            for item in ("img", "label", "value"):
                if key not in self.layout.nodes:
                    self._forget(self.items[key][item])
                else:
                    self._cfg(self.items[key][item], state="hidden")
            if key not in self.layout.nodes:
                del self.items[key]
        c.tag_raise("tip")

    # ================================================================ animation
    def _kick(self) -> None:
        if not self._animating and not self.layout.asleep:
            self._animating = True
            self.win.after(FRAME_MS, self._animate)

    def _animate(self) -> None:
        if not self.win.winfo_exists():
            return
        if self.layout.asleep and self.drag is None:
            self._animating = False
            self._adapt_room()           # may reheat and restart the animation
            self._follow()
            self.render()
            return
        started = time.perf_counter()
        self.layout.step()
        self.layout.step()
        self._follow()
        self.render()
        # Keep a steady frame rate: the next frame is due FRAME_MS after this
        # one started, not FRAME_MS after it finished.
        spent = int((time.perf_counter() - started) * 1000)
        self.win.after(max(1, FRAME_MS - spent), self._animate)

    def shake(self) -> None:
        import random
        for n in self.layout.nodes.values():
            n.vx += random.uniform(-15, 15)
            n.vy += random.uniform(-15, 15)
        self.layout.alpha = 1.0
        self._kick()

    def _apply_filters(self) -> None:
        self.layout.hidden_kinds = {k for k in ("event", "ghost") if not self.filters[k].get()}
        self.layout.hidden_links = {k for k in ("res", "tree") if not self.filters[k].get()}
        self.layout.heat(0.4)
        self._kick()

    # ================================================================ interaction
    def node_at(self, sx: float, sy: float):
        wx, wy = self.to_world(sx, sy)
        best, bd = None, 1e9
        for n in self.layout.visible_nodes():
            d = ((n.x - wx) ** 2 + (n.y - wy) ** 2) ** 0.5
            if d < pad_of(n) - 2 and d < bd:
                best, bd = n, d
        return best

    def _on_press(self, e) -> None:
        n = self.node_at(e.x, e.y)
        self.drag = {"node": n, "x": e.x, "y": e.y, "vx": self.view["x"], "vy": self.view["y"], "moved": False}
        if n is not None:
            n.pinned = True
            self.layout.heat(0.3)
            self._kick()
        self.canvas.configure(cursor="fleur")

    def _on_drag(self, e) -> None:
        d = self.drag
        if d is None:
            return
        if abs(e.x - d["x"]) + abs(e.y - d["y"]) > 4:
            d["moved"] = True
        if d["node"] is not None:
            d["node"].x, d["node"].y = self.to_world(e.x, e.y)
            self.layout.heat(0.25)
            self._hide_tip()
            self._kick()
        else:
            if d["moved"]:
                self._auto_view = False      # the user is placing the view now
            self.view["x"] = d["vx"] + e.x - d["x"]
            self.view["y"] = d["vy"] + e.y - d["y"]
            self.render()

    def _on_release(self, e) -> None:
        d, self.drag = self.drag, None
        self.canvas.configure(cursor="")
        if d is None:
            return
        n = d["node"]
        if n is not None:
            n.pinned = False
            if not d["moved"]:
                self.select(n.key)
        elif not d["moved"]:
            self.select(None)
        self._kick()

    def _on_motion(self, e) -> None:
        if self.drag is not None:
            return
        n = self.node_at(e.x, e.y)
        self._set_hover(n.key if n else None)
        if n is not None:
            self._show_tip(n, e.x, e.y)
        else:
            self._hide_tip()

    def _set_hover(self, key) -> None:
        if key != self.hover:
            self.hover = key
            if key is None:
                self._hide_tip()
            self.render()

    def _show_tip(self, n, sx, sy) -> None:
        if n.kind == "prog":
            g = n.data["group"]
            text = f"{g.name}" + (f"  ×{g.count}" if g.count > 1 else "") + \
                   "\n" + tr(f"זיכרון {_fmt_bytes(g.rss)} · מעבד {g.cpu:.1f}%",
                             f"Memory {_fmt_bytes(g.rss)} · CPU {g.cpu:.1f}%")
        elif n.kind == "res":
            text = f"{n.label}  {n.data['text']}" + (f"\n{n.data['sub']}" if n.data.get("sub") else "")
        elif n.kind == "event":
            sus = ", ".join(n.data.get("suspects", [])[:2])
            text = _event_line(n.data) + ("\n" + i18n.bidi(tr(f"חשוד: {sus}", f"Suspect: {sus}")) if sus else "")
        elif n.kind == "ghost":
            text = f"{n.label}\n" + tr(f"לא פעיל {n.data['idle']} · {_fmt_bytes(n.data['rss'])}",
                                       f"Idle {n.data['idle']} · {_fmt_bytes(n.data['rss'])}")
        elif n.kind == "claude":
            text = "✳ Claude\n" + tr("לחץ כדי לשאול מה קורה", "Click to ask what is going on")
        else:
            text = tr("המחשב שלי", "My computer") + "\n" + self._verdict()[1]
        c = self.canvas
        c.itemconfigure(self.tip, text=text, state="normal")
        c.itemconfigure(self.tip_bg, state="normal")
        c.coords(self.tip, sx + 14, sy + 14)
        x0, y0, x1, y1 = c.bbox(self.tip)
        w, h = self._size()
        dx = min(0, w - 8 - x1)
        dy = min(0, h - 8 - y1)
        c.move(self.tip, dx, dy)
        x0, y0, x1, y1 = c.bbox(self.tip)
        c.coords(self.tip_bg, x0 - 7, y0 - 5, x1 + 7, y1 + 5)
        c.tag_raise("tip")
        c.tag_raise(self.tip)

    def _hide_tip(self) -> None:
        self.canvas.itemconfigure("tip", state="hidden")

    def _on_search(self, _e=None) -> None:
        q = self.search.get().strip().lower()
        if not q:
            return
        for n in self.layout.visible_nodes():
            if q in n.label.lower():
                self.focus_node(n.key)
                return

    def focus_node(self, key: str) -> None:
        n = self.layout.nodes.get(key)
        if n is None:
            return
        if n.kind in self.layout.hidden_kinds:
            self.filters[n.kind].set(True)
            self._apply_filters()
        k = self.view["k"]
        self._auto_view = False
        self.view.update(x=-n.x * k, y=-n.y * k)
        self.select(key)

    def select(self, key: str | None) -> None:
        if key == "claude":
            self.ask_claude()
        self.selected = key if key != "pc" else None
        self.show_note(key)
        self._fill_tree()
        self.render()

    def toggle_full(self, on: bool | None = None) -> None:
        self.full = (not self.full) if on is None else on
        try:
            self.win.attributes("-fullscreen", self.full)
        except tk.TclError:
            self.win.state("zoomed" if self.full else "normal")
        self.win.after(120, self.fit)

    def ask_claude(self, extra: dict | None = None, question: str | None = None) -> None:
        try:
            from ai_advisor import build_snapshot
            from claude_panel import open_claude_panel
            from temperature_readings import read_gpu_temp_celsius

            def snapshot():
                groups = [(g.name, g.cpu, g.rss, g.count) for g in self._groups]
                snap = build_snapshot(self._snap, groups, self._events, gpu_temp=read_gpu_temp_celsius())
                snap.update(extra or {})
                return snap
            open_claude_panel(self.win, snapshot, question)
        except Exception:
            log.exception("Could not open the Ask Claude panel")

    def close_target(self, key: str) -> None:
        from alerts import try_terminate
        n = self.layout.nodes.get(key)
        if n is None:
            return
        if n.kind == "ghost":
            pid, name = n.data["pid"], n.data["name"]
        elif n.kind == "prog":
            g = n.data["group"]
            if not g.root_pids:
                return
            pid, name = g.root_pids[0], g.name
        else:
            return
        if try_terminate(pid, name, self.win):
            self.win.after(800, self.refresh)

    # ================================================================ side tree
    def _fill_tree(self) -> None:
        nodes = self.layout.nodes
        sig = (self.selected, tuple((k, n.data.get("text") if n.kind == "res" else
                                     round(n.data["group"].rss / 2 ** 20) if n.kind == "prog" else 0)
                                    for k, n in nodes.items()))
        if sig == getattr(self, "_tree_sig", None):
            return
        self._tree_sig = sig
        t = self.tree
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.tag_configure("folder", foreground=FG_SOFT, font=(FONT, 10), justify=self.justify, spacing1=8)
        t.tag_configure("file", foreground=FG_SOFT, justify=self.justify,
                        lmargin1=18 if self.side == "left" else 0, rmargin=18 if self.side == "right" else 0)
        t.tag_configure("on", background=ACTIVE, foreground="#ffffff")
        t.tag_configure("meta", foreground=FG_FAINT, font=(MONO, 8))

        def file(key, text, meta=""):
            tags = ("file", "k:" + key) + (("on",) if key == self.selected else ())
            t.insert("end", "📄 " + text + ("   " if meta else ""), tags)
            if meta:
                t.insert("end", meta, tags + ("meta",))
            t.insert("end", "\n", tags)
            t.tag_bind("k:" + key, "<Button-1>",
                       lambda _e, k=key: self.open_slow() if k == "slow" else self.focus_node(k))
            t.tag_bind("k:" + key, "<Enter>", lambda _e: t.configure(cursor="hand2"))
            t.tag_bind("k:" + key, "<Leave>", lambda _e: t.configure(cursor="arrow"))

        nodes = self.layout.nodes
        t.insert("end", "▾ 📂 " + tr("המחשב", "Computer") + "\n", "folder")
        file("pc", tr("עכשיו", "Now"))
        file("slow", tr("למה המחשב היה איטי?", "Why was it slow?"))
        for n in nodes.values():
            if n.kind == "res":
                file(n.key, n.label, n.data["text"])
        progs = sorted((n for n in nodes.values() if n.kind == "prog"),
                       key=lambda n: n.data["group"].rss, reverse=True)
        t.insert("end", "▾ 📂 " + tr("תוכנות", "Programs") + "\n", "folder")
        for n in progs:
            file(n.key, n.label, _fmt_bytes(n.data["group"].rss))
        evs = [n for n in nodes.values() if n.kind == "event"]
        if evs:
            t.insert("end", "▾ 📂 " + tr("אירועים", "Events") + "\n", "folder")
            for n in evs:
                file(n.key, _event_line(n.data))
        ghosts = [n for n in nodes.values() if n.kind == "ghost"]
        if ghosts:
            t.insert("end", "▾ 📂 " + tr("נטושים", "Abandoned") + "\n", "folder")
            for n in ghosts:
                file(n.key, n.label, _fmt_bytes(n.data["rss"]))
        t.insert("end", "▾ 📂 Claude\n", "folder")
        file("claude", tr("שאל את Claude", "Ask Claude"))
        t.configure(state="disabled")

    # ================================================================ notes
    def _note_tags(self) -> None:
        n, j = self.note, self.justify
        n.tag_configure("h1", font=(FONT, 19, "bold"), foreground=FG, justify=j, spacing3=2)
        n.tag_configure("sub", font=(FONT, 9), foreground=FG_FAINT, justify=j, spacing3=10)
        n.tag_configure("h2", font=(FONT, 11, "bold"), foreground=FG, justify=j, spacing1=12, spacing3=4)
        n.tag_configure("p", foreground=FG_SOFT, justify=j, spacing3=6)
        n.tag_configure("pk", foreground=FG_FAINT, justify=j)
        n.tag_configure("pv", foreground=FG, font=(MONO, 9), justify=j)
        n.tag_configure("link", foreground=ACCENT, justify=j)
        n.tag_configure("tag", foreground="#b89cff", background=ACCENT_SOFT, font=(FONT, 9))
        for kind, bg, fg, bar in (("info", ACCENT_SOFT, "#c3a9ff", ACCENT), ("warn", WARN_SOFT, "#ff8a8a", WARN),
                                  ("ok", OK_SOFT, "#7ee29b", OK)):
            n.tag_configure("c_" + kind, background=bg, foreground=FG_SOFT, justify=j,
                            lmargin1=10, lmargin2=10, rmargin=10, spacing1=2, spacing3=2)
            n.tag_configure("ct_" + kind, background=bg, foreground=fg, font=(FONT, 10, "bold"), justify=j,
                            lmargin1=10, lmargin2=10, rmargin=10, spacing1=8)
        n.tag_configure("rule", font=(FONT, 2), background=LINE)

    def _w(self, text, *tags):
        self.note.insert("end", text, tags)

    def _link(self, text, key):
        tag = f"l:{key}:{self.note.index('end')}"
        self._w("[[" + text + "]]", "link", tag)
        self.note.tag_bind(tag, "<Button-1>", lambda _e: self.focus_node(key))
        self.note.tag_bind(tag, "<Enter>", lambda _e: self.note.configure(cursor="hand2"))
        self.note.tag_bind(tag, "<Leave>", lambda _e: self.note.configure(cursor="arrow"))

    def _callout(self, kind, title, body=""):
        self._w(title + "\n", "ct_" + kind)
        self._w((body or " ") + "\n", "c_" + kind)
        self._w("\n", "p")

    def _props(self, rows):
        for k, v in rows:
            self._w(k + "   ", "pk")
            self._w(v + "\n", "pv")
        self._w("\n", "rule")
        self._w("\n", "p")

    def _button(self, text, cmd, color=FG, bg=HOVER):
        b = tk.Label(self.note, text=text, fg=color, bg=bg, font=(FONT, 9, "bold"), padx=10, pady=4,
                     cursor="hand2")
        b.bind("<Button-1>", lambda _e: cmd())
        self.note.window_create("end", window=b, padx=3, pady=6)

    def _spark(self, metric: str) -> None:
        """Last hour of a metric from the history file, as a small line."""
        try:
            from bisect import bisect_left

            from live_chart import _IncrementalSeriesReader, _window
            if not hasattr(self, "_reader"):
                self._reader = _IncrementalSeriesReader(self.history_dir / "metrics.csv")
            s = self._reader.read()
            if len(s) < 2:
                return
            start = s.t[-1] - 1 / 24
            t, cols = s.view(bisect_left(s.t, start))
            t, cols, _n = _window(t, cols, start, max_points=280)
        except Exception:
            log.debug("No sparkline", exc_info=True)
            return
        w, h = 290, 54
        cv = tk.Canvas(self.note, width=w, height=h, bg=BG, highlightthickness=0)
        rgb = RES_COLORS.get(metric if metric != "ram" else "ram", (150, 150, 150))
        v = cols["cpu" if metric == "cpu" else "ram"]
        if len(t) >= 2 and t[-1] > t[0]:
            xs = (t - t[0]) / (t[-1] - t[0]) * (w - 4) + 2
            ys = h - 4 - v / 100 * (h - 8)
            pts = [c for xy in zip(xs.tolist(), ys.tolist()) for c in xy]
            cv.create_line(2, h - 4, w - 2, h - 4, fill=LINE)
            cv.create_polygon(2, h - 4, *pts, w - 2, h - 4, fill=blend(rgb, 0.15), outline="")
            cv.create_line(*pts, fill=hex_of(rgb), width=1.5)
        self._w(tr("השעה האחרונה\n", "Last hour\n"), "pk")
        self.note.window_create("end", window=cv)
        self._w("\n", "p")

    def show_note(self, key: str | None) -> None:
        note = self.note
        note.configure(state="normal")
        note.delete("1.0", "end")
        n = self.layout.nodes.get(key) if key else None
        try:
            if key == "slow":
                self._note_slow()
            elif n is None or n.kind == "pc":
                self._note_now()
            elif n.kind == "prog":
                self._note_prog(n)
            elif n.kind == "res":
                self._note_res(n)
            elif n.kind == "event":
                self._note_event(n)
            elif n.kind == "ghost":
                self._note_ghost(n)
            elif n.kind == "claude":
                self._note_claude()
        except Exception:
            log.exception("Note rendering failed")
        note.configure(state="disabled")

    def _backlinks(self, key):
        nodes = self.layout.nodes
        seen = set()
        for link in self.layout.links:
            other = link.b if link.a == key else link.a if link.b == key else None
            if other and other != "pc" and other not in seen and other in nodes:
                seen.add(other)
                self._w("• ", "p")
                self._link(nodes[other].label or tr("המחשב", "Computer"), other)
                self._w("\n", "p")

    def _note_now(self):
        snap = self._snap
        from live_chart import GREEN
        color, text, _ = self._verdict()
        kind = "ok" if color == GREEN else "warn"
        self._w(tr("עכשיו", "Now") + "\n", "h1")
        self._w(datetime.now().strftime("%d.%m.%Y · %H:%M") + "\n", "sub")
        body = tr(f"מעבד {snap.cpu_percent:.0f}% · זיכרון {snap.ram_percent:.0f}%",
                  f"CPU {snap.cpu_percent:.0f}% · Memory {snap.ram_percent:.0f}%")
        if snap.temp_celsius is not None:
            body += f" · {snap.temp_celsius:.0f}°"
        self._callout(kind, ("ⓘ " if kind == "ok" else "⚠ ") + text, body)
        self._w(tr("הכי כבדים", "Heaviest") + "\n", "h2")
        progs = sorted((n for n in self.layout.nodes.values() if n.kind == "prog" and not n.data["group"].is_self),
                       key=lambda n: n.data["group"].rss, reverse=True)[:5]
        for n in progs:
            g = n.data["group"]
            self._w("• ", "p")
            self._link(g.name, n.key)
            self._w(f"  {_fmt_bytes(g.rss)} · " + tr(f"מעבד {g.cpu:.1f}%", f"CPU {g.cpu:.1f}%") + "\n", "p")
        evs = [n for n in self.layout.nodes.values() if n.kind == "event"]
        self._w(tr("אירועים היום", "Events today") + "\n", "h2")
        if evs:
            for n in evs:
                self._w("• ", "p")
                self._link(_event_line(n.data), n.key)
                self._w("\n", "p")
        else:
            self._w(tr("לא היו אירועי עומס היום.\n", "No load events today.\n"), "p")
        self._w(tr("איך לקרוא את הגרף", "Reading the graph") + "\n", "h2")
        self._w(tr("כל בועה היא משהו במחשב. בועה אפורה היא תוכנה, וככל שהיא גדולה היא תופסת יותר זיכרון. "
                   "הטבעת סביבה מראה כמה מעבד היא צורכת עכשיו, והנקודות הן התהליכים שלה. משושה הוא משאב, "
                   "יהלום אדום הוא אירוע עומס, ורוח היא תהליך שננטש. אפשר לגרור כל בועה.\n",
                   "Every bubble is something on the computer. A grey bubble is a program; the bigger it is, "
                   "the more memory it holds. Its ring shows CPU use right now and the dots are its "
                   "processes. A hexagon is a resource, a red diamond a load event, a ghost an abandoned "
                   "process. Drag any bubble.\n"), "p")
        self._button("✳ " + tr("שאל את Claude מה קורה", "Ask Claude what is going on"), self.ask_claude,
                     color=CLAUDE, bg="#2d211c")
        self._button("⏱ " + tr("למה המחשב היה איטי?", "Why was it slow?"), lambda: self.open_slow())

    def _note_prog(self, n):
        g = n.data["group"]
        self._w(g.name + "\n", "h1")
        self._w(tr("תוכנה", "Program") + "\n", "sub")
        self._props([
            ("▣ " + tr("תהליכים", "Processes"), str(g.count)),
            ("▤ " + tr("זיכרון", "Memory"), _fmt_bytes(g.rss)),
            ("◔ " + tr("מעבד", "CPU"), f"{g.cpu:.1f}%"),
            ("↳ " + tr("נפתח מ", "Started by"), g.parent_name or "Windows"),
        ])
        if g.is_self:
            self._callout("info", "ⓘ " + tr("זה PulseDeck", "This is PulseDeck"),
                          tr("כמה הכלי עצמו עולה למחשב, בשקיפות.", "What the tool itself costs, openly."))
        elif g.cpu >= 10 or g.rss >= 2 * 1024 ** 3:
            body = " ".join(x for x in (tr("תופס הרבה זיכרון.", "Holds a lot of memory.") if g.rss >= 2 * 1024 ** 3 else "",
                                         tr("עובד חזק על המעבד.", "Working the CPU hard.") if g.cpu >= 10 else "") if x)
            self._callout("warn", "⚠ " + tr("מהכבדים במחשב עכשיו", "Among the heaviest right now"), body)
        about = ABOUT.get(g.name) or ABOUT.get(g.name.lower())
        self._w(tr("מה זה", "What it is") + "\n", "h2")
        self._w((tr(*about) if about else tr("תוכנה שרצה במחשב.", "A program running on this computer.")) + "\n", "p")
        self._w(tr("מקושר אל", "Linked to") + "\n", "h2")
        self._backlinks(n.key)
        self._w("\n", "p")
        self._button("✳ " + tr(f"שאל את Claude על {g.name}", f"Ask Claude about {g.name}"), self.ask_claude,
                     color=CLAUDE, bg="#2d211c")
        if not g.is_self and g.name.lower() not in _PROTECTED_CLOSE:
            self._button(tr(f"סגור את {g.name}…", f"Close {g.name}…"), lambda: self.close_target(n.key),
                         color="#ff8a8a", bg=WARN_SOFT)

    def _note_res(self, n):
        d = n.data
        self._w(d["label"] + "\n", "h1")
        self._w(tr("משאב של המחשב", "A computer resource") + "\n", "sub")
        self._props([("◔ " + tr("עכשיו", "Now"), d["text"])] +
                    ([("ⓘ " + tr("פרטים", "Details"), d["sub"])] if d.get("sub") else []))
        if d["id"] in ("cpu", "ram"):
            self._spark(d["id"])
        users = [(self.layout.nodes[link.a], link.w) for link in self.layout.links
                 if link.type == "res" and link.b == n.key and link.a in self.layout.nodes]
        users.sort(key=lambda u: u[1], reverse=True)
        self._w(tr("מי משתמש בו", "Who uses it") + "\n", "h2")
        if not users:
            self._w(tr("אין כרגע.\n", "Nobody right now.\n"), "p")
        for u, _w in users[:8]:
            g = u.data["group"]
            self._w("• ", "p")
            self._link(g.name, u.key)
            self._w(f"  {g.cpu:.1f}%\n" if d["id"] == "cpu" else f"  {_fmt_bytes(g.rss)}\n", "p")

    def _note_event(self, n):
        d = n.data
        self._w(_event_line(d) + "\n", "h1")
        self._w(tr("אירוע עומס · היום", "Load event · today") + "\n", "sub")
        raw = d.get("raw", {})
        # The icon stays outside the embedding: Windows draws it in a fallback
        # font, as a separate piece, and an embedding split across two pieces
        # is lost - like every other callout, it sits at the left edge.
        self._callout("warn", "⚠ " + "\n".join(i18n.bidi(p) for p in _event_reason(d["reason"])),
                      f"CPU {raw.get('cpu_before', 0):.0f}% → {raw.get('cpu_after', 0):.0f}%   ·   "
                      f"RAM {raw.get('ram_before', 0):.0f}% → {raw.get('ram_after', 0):.0f}%")
        sus = d.get("suspects", [])
        if sus:
            self._w(tr("חשודים", "Suspects") + "\n", "h2")
            for name in sus[:4]:
                key = "prog:" + name.removesuffix(".exe")
                self._w("• ", "p")
                if key in self.layout.nodes:
                    self._link(name.removesuffix(".exe"), key)
                else:
                    self._w(name, "p")
                self._w("\n", "p")
        busy = self._busy_at(d["time"])
        if busy and busy["cpu"]:
            # From the per-program history: who actually used the CPU around
            # that minute (the spike report only knew who held memory).
            self._w(tr("מי עבד על המעבד באותו רגע", "Who was using the CPU then") + "\n", "h2")
            for item in busy["cpu"][:5]:
                key = "prog:" + item["name"]
                self._w("• ", "p")
                if key in self.layout.nodes:
                    self._link(item["name"], key)
                else:
                    self._w(item["name"], "p")
                self._w(f"  {item['cpu']:.1f}%\n", "p")
        self._button("✳ " + tr("שאל את Claude מה קרה", "Ask Claude what happened"), self.ask_claude,
                     color=CLAUDE, bg="#2d211c")

    def _busy_at(self, hhmmss: str) -> dict | None:
        """Who was busy around today's HH:MM:SS, from process_history."""
        try:
            from process_history import get_default_history
            h, m, sec = (int(x) for x in hhmmss.split(":"))
            t = datetime.now().replace(hour=h, minute=m, second=sec, microsecond=0).timestamp()
            return get_default_history().around(t)
        except Exception:
            log.debug("No process history for %s", hhmmss, exc_info=True)
            return None

    def _note_ghost(self, n):
        d = n.data
        self._w(d["name"] + "\n", "h1")
        self._w(tr("תהליך נטוש", "Abandoned process") + "\n", "sub")
        self._props([("◷ " + tr("לא פעיל", "Idle"), d["idle"]), ("▤ " + tr("זיכרון", "Memory"), _fmt_bytes(d["rss"])),
                     ("↳ " + tr("נפתח מ", "Started by"), d.get("parent") or "?"), ("PID", str(d["pid"]))])
        self._callout("info", "ⓘ " + tr("למה הוא כאן", "Why it is here"),
                      d.get("detail") or tr("התוכנה שפתחה אותו כבר לא צריכה אותו.",
                                            "The program that started it no longer needs it."))
        self._button(tr("סגור את התהליך…", "Close the process…"), lambda: self.close_target(n.key),
                     color="#ff8a8a", bg=WARN_SOFT)

    def _note_claude(self):
        self._w("✳ Claude\n", "h1")
        self._w(tr("יועץ, רק כשלוחצים", "An adviser, only when you ask") + "\n", "sub")
        self._w(tr("Claude מקבל שמות של תוכנות ומספרים, בלי שורות פקודה ובלי קבצים, ומסביר במילים פשוטות "
                   "מה קורה ומה כדאי לעשות.\n",
                   "Claude gets program names and numbers, no command lines or files, and explains in "
                   "plain words what is going on and what to do.\n"), "p")
        self._button("✳ " + tr("שאל מה קורה עכשיו", "Ask what is going on"), self.ask_claude,
                     color=CLAUDE, bg="#2d211c")

    # ================================================================ why was it slow?
    _SLOW_RANGES = ("hour", "today", "yesterday")

    def open_slow(self, rng: str | None = None) -> None:
        if rng:
            self._slow_range = rng
        self.selected = "slow"
        self.show_note("slow")
        self._fill_tree()
        self.render()

    def _slow_window(self, rng: str) -> tuple[datetime, datetime, str]:
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if rng == "hour":
            return now - timedelta(hours=1), now, tr("בשעה האחרונה", "in the last hour")
        if rng == "yesterday":
            return midnight - timedelta(days=1), midnight, tr("אתמול", "yesterday")
        return midnight, now, tr("היום", "today")

    def _slow_data(self, t0: datetime, t1: datetime):
        """(t days, cpu, ram) numpy copies for [t0, t1] from the CSV history."""
        import numpy as np
        from bisect import bisect_left

        from live_chart import _IncrementalSeriesReader, _wall_days
        if not hasattr(self, "_reader"):
            self._reader = _IncrementalSeriesReader(self.history_dir / "metrics.csv")
        s = self._reader.read()
        lo, hi = bisect_left(s.t, _wall_days(t0)), bisect_left(s.t, _wall_days(t1))
        t, cols = s.view(lo)
        n = max(0, hi - lo)
        return np.array(t[:n]), np.array(cols["cpu"][:n]), np.array(cols["ram"][:n])

    def _note_slow(self):
        from process_history import get_default_history
        from slowdown import days_to_datetime, describe, find_episodes

        rng = getattr(self, "_slow_range", "today")
        t0, t1, label = self._slow_window(rng)
        self._w(tr("למה המחשב היה איטי?", "Why was it slow?") + "\n", "h1")
        self._w(tr("לפי ההיסטוריה שנשמרת במחשב", "From the history kept on this computer") + "\n", "sub")
        for key, text in (("hour", tr("השעה האחרונה", "Last hour")), ("today", tr("היום", "Today")),
                          ("yesterday", tr("אתמול", "Yesterday"))):
            on = key == rng
            self._button(text, lambda k=key: self.open_slow(k), color="#ffffff" if on else FG_SOFT,
                         bg=ACTIVE if on else HOVER)
        self._w("\n", "p")
        try:
            t, cpu, ram = self._slow_data(t0, t1)
        except Exception:
            log.debug("No CPU history", exc_info=True)
            t = cpu = ram = []
        if len(t) < 2:
            self._callout("info", "ⓘ " + tr("אין עדיין היסטוריה לתקופה הזו", "No history for this period yet"),
                          tr("ההיסטוריה נאספת רק כש-PulseDeck רץ עם רישום היסטוריה.",
                             "History is collected only while PulseDeck runs with history logging on."))
            return
        episodes = find_episodes(t, cpu, ram)
        self._slow_chart(t, cpu, episodes)
        history = get_default_history()
        lines = []
        for ep in reversed(episodes[-8:]):
            a = days_to_datetime(ep["start"]).timestamp() - 30
            b = days_to_datetime(ep["end"]).timestamp() + 30
            lines.append(describe(ep, history.who_was_busy(a, b, limit=3)))
        if not episodes:
            self._callout("ok", "ⓘ " + tr(f"לא היו רגעים איטיים {label}", f"No slow moments {label}"),
                          tr("המעבד לא נשאר מעל 70% והזיכרון לא מעל 85% יותר מחצי דקה.",
                             "CPU never stayed above 70% nor memory above 85% for more than half a minute."))
        else:
            n = len(episodes)
            self._callout("warn", "⚠ " + tr(f"{n} רגעים איטיים {label}", f"{n} slow moment(s) {label}"),
                          tr("החדשים קודם. השמות הם מי שעבד הכי קשה באותן דקות.",
                             "Newest first. The names are who worked hardest during those minutes."))
            for line in lines:
                self._w("• " + line + "\n", "p")
        self._w("\n", "p")
        self._button("✳ " + tr("שאל את Claude למה", "Ask Claude why"),
                     lambda: self.ask_claude({"slow_moments": lines, "period": label},
                                             tr(f"למה המחשב היה איטי {label}? מה כדאי לעשות?",
                                                f"Why was my computer slow {label}? What should I do?")),
                     color=CLAUDE, bg="#2d211c")

    def _slow_chart(self, t, cpu, episodes) -> None:
        """CPU over the period, slow stretches shaded red."""
        import numpy as np
        w, h = 290, 70
        cv = tk.Canvas(self.note, width=w, height=h, bg=BG, highlightthickness=0)
        t0, t1 = float(t[0]), float(t[-1])
        span = (t1 - t0) or 1e-9
        for ep in episodes:
            x0 = 2 + (ep["start"] - t0) / span * (w - 4)
            x1 = max(x0 + 2, 2 + (ep["end"] - t0) / span * (w - 4))
            cv.create_rectangle(x0, 2, x1, h - 4, fill=WARN_SOFT, outline="")
        step = max(1, len(t) // (w - 4))
        tt, cc = t[::step], np.nan_to_num(cpu[::step])
        xs = 2 + (tt - t0) / span * (w - 4)
        ys = h - 4 - cc / 100 * (h - 8)
        pts = [c for xy in zip(xs.tolist(), ys.tolist()) for c in xy]
        cv.create_line(2, h - 4, w - 2, h - 4, fill=LINE)
        if len(pts) >= 4:
            cv.create_line(*pts, fill=hex_of(RES_COLORS["cpu"]), width=1.2)
        self._w(tr("מעבד לאורך התקופה (אדום = רגע איטי)\n", "CPU over the period (red = slow moment)\n"), "pk")
        self.note.window_create("end", window=cv)
        self._w("\n", "p")
