"""Tk pop-up alerting on CPU/RAM spikes, with safe per-process kill action."""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import messagebox
from typing import Callable

import psutil

from process_monitor import ProcessInfo, get_default_sampler

log = logging.getLogger(__name__)

# Modern tech-flavored palette
BG       = "#0d1117"   # deep near-black
PANEL    = "#161b22"   # card surface
PANEL_HI = "#1c2230"   # raised card
BORDER   = "#30363d"   # subtle border
FG       = "#e6edf3"
DIM      = "#7d8590"
ACCENT   = "#58e1ff"   # cyan-electric — CPU column
ORANGE   = "#ff9966"   # orange-coral — RAM column
WARN     = "#f0c674"   # amber
BAD      = "#ff5c6c"   # neon-red

# Status dot colors
DOT_ACTIVE     = "#3fb950"   # green
DOT_RECENT     = "#d29922"   # amber
DOT_BACKGROUND = "#6e7681"   # gray
DOT_PROTECTED  = "#bb8eff"   # purple

MONO_FAMILY = "Cascadia Mono"   # falls back gracefully on systems without it

PROTECTED_NAMES: frozenset[str] = frozenset({
    "svchost.exe", "services.exe", "csrss.exe", "smss.exe", "winlogon.exe",
    "lsass.exe", "wininit.exe", "system", "idle", "registry", "dwm.exe",
    "fontdrvhost.exe", "memory compression", "secure system",
    "systemd", "init", "kernel_task", "launchd",
})


def _is_protected(pid: int, name: str) -> bool:
    """Return True if this PID/name should never be killed (system or self)."""
    if pid == os.getpid():
        return True
    return (name or "").strip().lower() in PROTECTED_NAMES


def format_relative_he(seconds_ago: float | None) -> str:
    if seconds_ago is None:
        return "ברקע / לא נצפתה פעילות"
    s = max(0.0, seconds_ago)
    if s < 5:
        return "פעיל עכשיו"
    if s < 60:
        return f"פעיל לפני {int(s)} שניות"
    if s < 3600:
        return f"פעיל לפני {int(s / 60)} דקות"
    if s < 86400:
        return f"פעיל לפני {int(s / 3600)} שעות"
    return f"פעיל לפני {int(s / 86400)} ימים"


def format_uptime_he(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d = s // 86400
    h = (s % 86400) // 3600
    return f"{d}d {h}h" if h else f"{d}d"


def _fmt_mib(rss: int) -> str:
    return f"{rss / (1024 * 1024):.0f} MiB"


@dataclass(frozen=True)
class AlertEvent:
    reason: str
    trigger: str  # "cpu" | "ram" | "both"
    cpu_before: float
    cpu_after: float
    ram_before: float
    ram_after: float
    top_cpu: list[ProcessInfo] = field(default_factory=list)
    top_rss: list[ProcessInfo] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


def try_terminate(pid: int, name: str, parent: tk.Misc | None) -> bool:
    if _is_protected(pid, name):
        messagebox.showerror(
            "תהליך מוגן",
            f"לא ניתן להרוג את {name} (PID {pid}) — תהליך מערכת קריטי או המוניטור עצמו.",
            parent=parent,
        )
        return False
    if not messagebox.askyesno(
        "אישור הרג תהליך",
        f"להרוג את {name} (PID {pid})?\n\nשים לב: עבודה לא שמורה תאבד.",
        parent=parent,
    ):
        return False
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            log.warning("Process %s (PID %d) ignored terminate; sending kill", name, pid)
            proc.kill()
            proc.wait(timeout=3)
        log.info("Killed process %s (PID %d)", name, pid)
        return True
    except psutil.NoSuchProcess:
        messagebox.showinfo("התהליך כבר אינו רץ",
                            f"{name} (PID {pid}) הסתיים בעצמו.", parent=parent)
        return True
    except (psutil.AccessDenied, OSError) as exc:
        log.exception("Failed to kill %s (PID %d)", name, pid)
        messagebox.showerror(
            "כשל בהרג תהליך",
            f"לא הצלחתי להרוג את {name} (PID {pid}).\n{exc}\n\nאולי דרושות הרשאות מנהל.",
            parent=parent,
        )
        return False


def _open_task_manager() -> None:
    try:
        if os.name == "nt":
            subprocess.Popen(["taskmgr.exe"], shell=False)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", "-a", "Activity Monitor"])
        else:
            subprocess.Popen(["gnome-system-monitor"])
    except OSError:
        log.exception("Could not open Task Manager")


def _status_dot(proc: ProcessInfo) -> tuple[str, str]:
    """Return (color, label) for the activity dot."""
    if _is_protected(proc.pid, proc.name):
        return DOT_PROTECTED, "מוגן"
    s = proc.last_active_seconds_ago
    if s is None:
        return DOT_BACKGROUND, "ברקע"
    if s < 5:
        return DOT_ACTIVE, "פעיל"
    if s < 120:
        return DOT_RECENT, "פעיל לאחרונה"
    return DOT_BACKGROUND, "ללא פעילות"


def _make_proc_card(parent: tk.Misc, proc: ProcessInfo, value_text: str,
                    accent_color: str, win: tk.Misc) -> tk.Frame:
    """Modern tech-styled card: left accent stripe + status dot + mono numbers."""
    # Outer card (subtle border via 1px frame)
    outer = tk.Frame(parent, bg=BORDER)
    inner = tk.Frame(outer, bg=PANEL_HI)
    inner.pack(fill="both", expand=True, padx=1, pady=1)

    # Left accent stripe (column color)
    stripe = tk.Frame(inner, bg=accent_color, width=3)
    stripe.pack(side="left", fill="y")

    content = tk.Frame(inner, bg=PANEL_HI, padx=12, pady=9)
    content.pack(side="left", fill="both", expand=True)

    # Top row: status dot + name (right) ↔ big mono value (left)
    top = tk.Frame(content, bg=PANEL_HI)
    top.pack(fill="x")

    dot_color, dot_label = _status_dot(proc)

    # Big mono value (left, dominant)
    val = tk.Label(top, text=value_text, bg=PANEL_HI, fg=accent_color,
                   font=(MONO_FAMILY, 14, "bold"))
    val.pack(side="left")

    # Name on the right (hebrew alignment)
    name_frame = tk.Frame(top, bg=PANEL_HI)
    name_frame.pack(side="right", fill="x", expand=True)
    tk.Label(name_frame, text=proc.name, bg=PANEL_HI, fg=FG,
             font=("Segoe UI", 11, "bold"), anchor="e").pack(side="right")
    tk.Label(name_frame, text="●", bg=PANEL_HI, fg=dot_color,
             font=("Segoe UI", 12)).pack(side="right", padx=(0, 6))

    # Meta row: PID · status · uptime
    meta_text = (f"PID {proc.pid}  ·  {dot_label}  ·  "
                 f"{format_relative_he(proc.last_active_seconds_ago)}  ·  "
                 f"רץ {format_uptime_he(proc.process_uptime_seconds)}")
    tk.Label(content, text=meta_text, bg=PANEL_HI, fg=DIM,
             font=("Segoe UI", 9), anchor="e").pack(fill="x", pady=(3, 0))

    # Action row
    action = tk.Frame(content, bg=PANEL_HI)
    action.pack(fill="x", pady=(6, 0))
    if _is_protected(proc.pid, proc.name):
        tk.Label(action, text="🔒 מוגן", bg=PANEL_HI, fg=DOT_PROTECTED,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
    else:
        btn = tk.Button(action, text="✕  Kill",
                        bg=PANEL_HI, fg=BAD,
                        font=("Segoe UI", 9, "bold"),
                        relief="flat", bd=0, padx=10, pady=3,
                        activebackground=BAD, activeforeground="white",
                        cursor="hand2",
                        command=lambda: try_terminate(proc.pid, proc.name, win))
        btn.pack(side="left")
    return outer


def _build_alert_window(event: AlertEvent, parent: tk.Misc | None) -> tk.Toplevel | tk.Tk:
    if parent is None:
        win: tk.Toplevel | tk.Tk = tk.Tk()
    else:
        win = tk.Toplevel(parent)
    win.title("התראת עומס מערכת")
    win.geometry("760x520")
    win.configure(bg=BG)
    try:
        win.attributes("-topmost", True)
    except tk.TclError:
        pass

    # Top accent strip (cyan→orange gradient simulation via two adjacent frames)
    strip = tk.Frame(win, bg=BG, height=3)
    strip.pack(fill="x")
    tk.Frame(strip, bg=ACCENT).place(relx=0, rely=0, relwidth=0.5, relheight=1)
    tk.Frame(strip, bg=ORANGE).place(relx=0.5, rely=0, relwidth=0.5, relheight=1)

    # Header
    hdr = tk.Frame(win, bg=PANEL, padx=20, pady=16)
    hdr.pack(fill="x")
    title_row = tk.Frame(hdr, bg=PANEL)
    title_row.pack(fill="x")
    tk.Label(title_row, text="◢◤", bg=PANEL, fg=BAD,
             font=("Consolas", 14, "bold")).pack(side="left", padx=(0, 8))
    tk.Label(title_row, text="זוהה עומס במחשב", bg=PANEL, fg=FG,
             font=("Segoe UI", 17, "bold"), anchor="e").pack(side="right", fill="x", expand=True)

    # Reason — sub-line
    tk.Label(hdr, text=event.reason, bg=PANEL, fg=DIM,
             font=("Segoe UI", 10), anchor="e", wraplength=720,
             justify="right").pack(fill="x", pady=(4, 8))

    # Big metrics strip — CPU and RAM side by side
    metrics = tk.Frame(hdr, bg=PANEL)
    metrics.pack(fill="x")
    metrics.columnconfigure(0, weight=1, uniform="m")
    metrics.columnconfigure(1, weight=1, uniform="m")

    def _metric_block(parent, label: str, before: float, after: float, color: str, col: int):
        block = tk.Frame(parent, bg=PANEL)
        tk.Label(block, text=label, bg=PANEL, fg=DIM,
                 font=("Segoe UI", 9), anchor="e").pack(fill="x")
        line = tk.Frame(block, bg=PANEL)
        line.pack(fill="x")
        tk.Label(line, text=f"{after:.0f}%", bg=PANEL, fg=color,
                 font=(MONO_FAMILY, 22, "bold")).pack(side="right")
        tk.Label(line, text=f"  ← {before:.0f}%", bg=PANEL, fg=DIM,
                 font=(MONO_FAMILY, 11)).pack(side="right")
        block.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 12, 0))

    _metric_block(metrics, "CPU", event.cpu_before, event.cpu_after, ACCENT, 0)
    _metric_block(metrics, "RAM", event.ram_before, event.ram_after, ORANGE, 1)

    # Body — two columns of cards
    body = tk.Frame(win, bg=BG, padx=16, pady=12)
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=1, uniform="col")
    body.columnconfigure(1, weight=1, uniform="col")

    def _column_header(parent, label: str, count: int, color: str) -> tk.Frame:
        hdr_frame = tk.Frame(parent, bg=BG)
        tk.Label(hdr_frame, text=f"{count}", bg=BG, fg=color,
                 font=(MONO_FAMILY, 10, "bold")).pack(side="left")
        tk.Label(hdr_frame, text=label, bg=BG, fg=color,
                 font=("Segoe UI", 11, "bold"), anchor="e").pack(side="right", fill="x", expand=True)
        # Underline divider
        return hdr_frame

    cpu_col = tk.Frame(body, bg=BG)
    _column_header(cpu_col, "מובילים ב-CPU", len(event.top_cpu), ACCENT).pack(fill="x")
    tk.Frame(cpu_col, bg=ACCENT, height=1).pack(fill="x", pady=(2, 8))
    for p in event.top_cpu:
        card = _make_proc_card(cpu_col, p, f"{p.cpu_percent:.0f}%", ACCENT, win)
        card.pack(fill="x", pady=3)
    cpu_col.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

    ram_col = tk.Frame(body, bg=BG)
    _column_header(ram_col, "מובילים ב-RAM", len(event.top_rss), ORANGE).pack(fill="x")
    tk.Frame(ram_col, bg=ORANGE, height=1).pack(fill="x", pady=(2, 8))
    for p in event.top_rss:
        card = _make_proc_card(ram_col, p, _fmt_mib(p.rss_bytes), ORANGE, win)
        card.pack(fill="x", pady=3)
    ram_col.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

    # Footer
    ftr = tk.Frame(win, bg=PANEL, padx=16, pady=12)
    ftr.pack(fill="x", side="bottom")
    btn_style = dict(font=("Segoe UI", 9, "bold"), relief="flat", bd=0,
                     padx=14, pady=6, cursor="hand2")
    tk.Button(ftr, text="פתח Task Manager", command=_open_task_manager,
              bg=PANEL_HI, fg=FG, activebackground=ACCENT, activeforeground=BG,
              **btn_style).pack(side="left")
    tk.Button(ftr, text="סגור", command=win.destroy,
              bg=PANEL_HI, fg=FG, activebackground=BAD, activeforeground="white",
              **btn_style).pack(side="right")
    return win


def show_alert_window(event: AlertEvent, parent: tk.Misc | None = None) -> None:
    """Open the full alert popup. Must be called on the Tk main thread."""
    try:
        win = _build_alert_window(event, parent)
    except tk.TclError:
        log.exception("Failed to build alert window")
        return
    if parent is None:
        try:
            win.mainloop()
        except tk.TclError:
            pass


# ---------- Toast (gentle bottom-right notification) ----------

TOAST_WIDTH      = 360
TOAST_HEIGHT     = 130
TOAST_DURATION_MS = 8000   # auto-dismiss after this long
TOAST_MARGIN_X   = 16
TOAST_MARGIN_Y   = 16


def _toast_summary(event: AlertEvent) -> tuple[str, str, str]:
    """Returns (title_line, value_line, color) — short text for the toast body."""
    if event.trigger == "cpu":
        title = "עומס CPU זוהה"
        value = f"{event.cpu_after:.0f}% (היה {event.cpu_before:.0f}%)"
        color = ACCENT
    elif event.trigger == "ram":
        title = "עומס זיכרון זוהה"
        value = f"{event.ram_after:.0f}% (היה {event.ram_before:.0f}%)"
        color = ORANGE
    else:
        title = "עומס מערכת זוהה"
        value = f"CPU {event.cpu_after:.0f}%  ·  RAM {event.ram_after:.0f}%"
        color = BAD
    return title, value, color


def _build_toast(event: AlertEvent, parent: tk.Misc | None,
                 on_open_full: Callable[[], None],
                 on_snooze: Callable[[float], None] | None = None,
                 ) -> tk.Toplevel | tk.Tk:
    """Small notification window pinned bottom-right. Click → open full alert."""
    if parent is None:
        win: tk.Toplevel | tk.Tk = tk.Tk()
    else:
        win = tk.Toplevel(parent)
    win.overrideredirect(True)
    win.configure(bg=BG)
    try:
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.0)  # start transparent for fade-in
    except tk.TclError:
        pass

    title_text, value_text, accent_color = _toast_summary(event)

    # Outer 1px border
    border = tk.Frame(win, bg=BORDER)
    border.pack(fill="both", expand=True)
    inner = tk.Frame(border, bg=PANEL)
    inner.pack(fill="both", expand=True, padx=1, pady=1)

    # Left accent stripe
    stripe = tk.Frame(inner, bg=accent_color, width=4)
    stripe.pack(side="left", fill="y")

    content = tk.Frame(inner, bg=PANEL, padx=14, pady=10)
    content.pack(side="left", fill="both", expand=True)

    # Top row: close button (left) + title (right)
    top_row = tk.Frame(content, bg=PANEL)
    top_row.pack(fill="x")

    close_btn = tk.Label(top_row, text="✕", bg=PANEL, fg=DIM,
                          font=("Segoe UI", 9, "bold"), cursor="hand2")
    close_btn.pack(side="left")

    title_lbl = tk.Label(top_row, text=title_text, bg=PANEL, fg=FG,
                         font=("Segoe UI", 11, "bold"), anchor="e")
    title_lbl.pack(side="right", fill="x", expand=True)

    # Value (mono)
    tk.Label(content, text=value_text, bg=PANEL, fg=accent_color,
             font=(MONO_FAMILY, 14, "bold"), anchor="e").pack(fill="x", pady=(4, 2))

    # Hint (also mentions snooze via right-click for discoverability)
    hint_text = "לחץ לפתיחת פרטים והרג תהליך  ·  קליק ימני להשהיה"
    tk.Label(content, text=hint_text,
             bg=PANEL, fg=DIM, font=("Segoe UI", 9), anchor="e").pack(fill="x")

    # Position: bottom-right above taskbar
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    final_x = sw - TOAST_WIDTH - TOAST_MARGIN_X
    final_y = sh - TOAST_HEIGHT - TOAST_MARGIN_Y - 50  # leave room for taskbar
    win.geometry(f"{TOAST_WIDTH}x{TOAST_HEIGHT}+{final_x}+{final_y}")

    # Fade-in
    def _fade_in(step: int = 0) -> None:
        try:
            alpha = min(0.96, step / 10)
            win.attributes("-alpha", alpha)
            if alpha < 0.96:
                win.after(20, _fade_in, step + 1)
        except tk.TclError:
            pass

    _fade_in()

    dismissed = {"v": False}

    def _dismiss() -> None:
        if dismissed["v"]:
            return
        dismissed["v"] = True
        try:
            win.destroy()
        except tk.TclError:
            pass

    def _on_click(_event: tk.Event) -> None:
        if dismissed["v"]:
            return
        dismissed["v"] = True
        try:
            win.destroy()
        except tk.TclError:
            pass
        try:
            on_open_full()
        except Exception:
            log.exception("Failed to open full alert window from toast")

    # Bind click on everything except the close button
    for w in (inner, content, stripe, top_row, title_lbl):
        w.bind("<Button-1>", _on_click)
    # Also bind the value/hint labels
    for child in content.winfo_children():
        child.bind("<Button-1>", _on_click)

    close_btn.bind("<Button-1>", lambda _e: _dismiss())

    # Right-click context menu — Snooze options
    if on_snooze is not None:
        def _show_snooze_menu(event: tk.Event) -> None:
            menu = tk.Menu(win, tearoff=0)
            menu.add_command(label="השהה התראות ל-15 דקות",
                             command=lambda: (on_snooze(15 * 60), _dismiss()))
            menu.add_command(label="השהה התראות ל-30 דקות",
                             command=lambda: (on_snooze(30 * 60), _dismiss()))
            menu.add_command(label="השהה התראות לשעה",
                             command=lambda: (on_snooze(60 * 60), _dismiss()))
            menu.add_separator()
            menu.add_command(label="סגור", command=_dismiss)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        # Bind on the toast body and labels
        for w in (inner, content, stripe, top_row, title_lbl):
            w.bind("<Button-3>", _show_snooze_menu)
        for child in content.winfo_children():
            child.bind("<Button-3>", _show_snooze_menu)

    # Auto-dismiss
    win.after(TOAST_DURATION_MS, _dismiss)
    return win


def show_toast(event: AlertEvent, parent: tk.Misc | None = None,
               dispatcher: "AlertDispatcher | None" = None) -> None:
    """Show the gentle toast notification. Must be called on the Tk main thread.

    If `dispatcher` is provided, right-click on the toast offers snooze options.
    """
    def _open_full() -> None:
        show_alert_window(event, parent)

    on_snooze = dispatcher.snooze if dispatcher is not None else None

    try:
        win = _build_toast(event, parent, _open_full, on_snooze=on_snooze)
    except tk.TclError:
        log.exception("Failed to build toast")
        return
    if parent is None:
        try:
            win.mainloop()
        except tk.TclError:
            pass


def make_alert_callback(dispatcher: "AlertDispatcher",
                        root_provider: Callable[[], tk.Misc | None] | None = None,
                        *, top_n: int = 5):
    """Build a callback compatible with HistoryLogger.alert_callback."""
    def _cb(prev_snap, curr_snap, reason: str, trigger: str) -> None:
        try:
            sampler = get_default_sampler()
            event = AlertEvent(
                reason=reason, trigger=trigger,
                cpu_before=prev_snap.cpu_percent, cpu_after=curr_snap.cpu_percent,
                ram_before=prev_snap.ram_percent, ram_after=curr_snap.ram_percent,
                top_cpu=sampler.top_by_cpu(top_n),
                top_rss=sampler.top_by_rss(top_n),
            )
            root = root_provider() if root_provider is not None else None
            dispatcher.fire(event, root=root)
        except Exception:
            log.exception("Alert callback raised")
    return _cb


def _is_top_mostly_muted(event: "AlertEvent", muted_lower: set[str]) -> bool:
    """If 3+ of the top CPU/RAM processes are muted, suppress the alert.

    The user has explicitly said this app is "expected to be loud" — only
    silence when the dominant cause is a process the user already accepted.
    """
    if not muted_lower:
        return False
    top_names = [p.name.strip().lower() for p in (event.top_cpu + event.top_rss)]
    if not top_names:
        return False
    muted_count = sum(1 for n in top_names if n in muted_lower)
    return muted_count >= 3


def _play_alert_sound() -> None:
    """Non-intrusive system 'asterisk' beep, Windows-only. Silent on failure."""
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        # Not Windows, or sound device unavailable — fine, this is decorative
        pass


class AlertDispatcher:
    def __init__(self, cooldown_seconds: float = 300.0,
                 muted_processes: list[str] | None = None,
                 sound_enabled: bool = False) -> None:
        self.cooldown_seconds = float(cooldown_seconds)
        self._last_fired_at = 0.0
        self._lock = threading.Lock()
        # Snooze: extends the next gate by this many seconds beyond cooldown.
        self._snooze_until = 0.0
        # Mute list (case-insensitive)
        self._muted = {n.strip().lower() for n in (muted_processes or [])}
        self.sound_enabled = bool(sound_enabled)

    def snooze(self, seconds: float) -> None:
        """Push the next allowed fire time forward by `seconds`."""
        self._snooze_until = time.time() + max(0.0, float(seconds))
        log.info("Alerts snoozed for %.0f seconds", seconds)

    def add_mute(self, process_name: str) -> None:
        self._muted.add(process_name.strip().lower())

    def remove_mute(self, process_name: str) -> None:
        self._muted.discard(process_name.strip().lower())

    @property
    def muted_processes(self) -> list[str]:
        return sorted(self._muted)

    def _gate(self) -> bool:
        now = time.time()
        with self._lock:
            if now < self._snooze_until:
                return False
            if now - self._last_fired_at < self.cooldown_seconds:
                return False
            self._last_fired_at = now
            return True

    def fire(self, event: AlertEvent, root: tk.Misc | None = None) -> bool:
        # Mute check happens BEFORE gate so a muted spike doesn't burn cooldown.
        if _is_top_mostly_muted(event, self._muted):
            log.debug("Alert suppressed (top processes muted): %s", event.reason)
            return False

        if not self._gate():
            # Demoted to DEBUG: at WARNING/INFO this fires on every detected
            # oscillation and floods monitor.log without giving the user any
            # actionable signal (the cooldown is intentional UX).
            log.debug("Alert suppressed (cooldown/snooze): %s", event.reason)
            return False
        log.info("Alert firing (toast): %s", event.reason)
        if self.sound_enabled:
            _play_alert_sound()

        # Bump sampler activity so subsequent ticks run at the faster rate,
        # giving the toast / full alert window the freshest process data.
        try:
            get_default_sampler().notify_activity()
        except Exception:
            log.exception("Failed to notify sampler of alert activity")
        if root is not None:
            try:
                root.after(0, lambda: show_toast(event, root, dispatcher=self))
            except tk.TclError:
                log.exception("Failed to schedule toast via root.after")
                return False
        else:
            t = threading.Thread(target=show_toast,
                                 args=(event, None),
                                 kwargs={"dispatcher": self},
                                 name="alert-toast", daemon=True)
            t.start()
        return True
