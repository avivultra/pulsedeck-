"""Tk panel for the Ghost Sweeper.

Kept out of `ghost_sweeper.py` on purpose: the sweeper must stay importable
(and testable) on a machine with no display, and nothing in the scanning path
should depend on tkinter.

Every card is verbose by design. The user asked to be able to decide from the
card alone — which process this is, whose child it was, how long it has been
orphaned, and whether it finished or got stuck — without opening Task Manager.
"""

from __future__ import annotations

import logging
import time

from ghost_sweeper import (
    REASON_LABELS,
    VERDICT_COLORS,
    VERDICT_LABELS,
    GhostProcess,
    get_default_sweeper,
    humanize_bytes,
    humanize_duration,
    persist_ignore,
)

log = logging.getLogger(__name__)

# Theme — matches alerts.py / janitor.py / server_scanner.py
BG, PANEL, PANEL_HI = "#0d1117", "#161b22", "#1c2230"
BORDER, FG, DIM = "#30363d", "#e6edf3", "#7d8590"
BAD, MUTED_BTN = "#ff5c6c", "#2d333b"
GHOST = "#a78bfa"          # the sweeper's signature violet


def _fmt_clock(unix_time: float) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(unix_time))
    except (ValueError, OSError):
        return "—"


def _shorten(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def describe_parent(g: GhostProcess) -> str:
    """One line answering 'whose child was this?'."""
    if g.parent_alive:
        return f"רץ תחת {g.parent_name} (PID {g.parent_pid}) — ההורה עדיין חי"
    name = g.parent_name if g.parent_name and g.parent_name != "?" else None
    when = humanize_duration(g.orphaned_for_seconds)
    prefix = "לפחות " if g.orphan_time_is_lower_bound else ""
    if name:
        return f"נוצר ע\"י {name} (PID {g.parent_pid}) — שנסגר לפני {prefix}{when}"
    return (f"ההורה (PID {g.parent_pid}) נסגר לפני {prefix}{when} — "
            f"הוא כבר לא היה קיים כשהמוניטור עלה, אז שמו לא ידוע")


def describe_activity(g: GhostProcess) -> str:
    """One line answering 'did it finish, or is it stuck?'."""
    prefix = "לפחות " if g.idle_time_is_lower_bound else ""
    idle = humanize_duration(g.idle_for_seconds)
    cpu_life = f"{g.cpu_percent_lifetime:.2f}%"
    total = humanize_duration(g.cpu_seconds_total)
    return (f"לא עשה כלום כבר {prefix}{idle}  ·  "
            f"בכל חייו צרך {total} של CPU ({cpu_life} מליבה)")


def describe_resources(g: GhostProcess) -> str:
    bits = [f"זיכרון {humanize_bytes(g.rss_bytes)}"]
    if abs(g.rss_delta_bytes) >= 1024 * 1024:
        sign = "+" if g.rss_delta_bytes > 0 else "−"
        bits.append(f"שינוי {sign}{humanize_bytes(abs(g.rss_delta_bytes))} מאז המעקב")
    bits.append(f"{g.num_threads} threads")
    if g.listening_ports:
        ports = ", ".join(str(p) for p in g.listening_ports[:5])
        if len(g.listening_ports) > 5:
            ports += f" (+{len(g.listening_ports) - 5})"
        bits.append(f"מאזין ל-{ports}")
        bits.append(f"{g.established_connections} חיבורים פעילים")
    return "  ·  ".join(bits)


def open_ghost_panel(parent) -> object:
    """Open the sweeper's report window. Returns the Toplevel."""
    import tkinter as tk
    from tkinter import messagebox

    from alerts import try_terminate   # confirmation + protected-process guard

    sweeper = get_default_sweeper()

    win = tk.Toplevel(parent) if parent is not None else tk.Tk()
    win.title("רוחות רפאים — תהליכים שננטשו")
    win.geometry("760x620")
    win.configure(bg=BG)
    try:
        win.attributes("-topmost", True)
    except tk.TclError:
        pass

    # ---- header ----
    hdr = tk.Frame(win, bg=PANEL, padx=18, pady=12)
    hdr.pack(fill="x")
    tk.Label(hdr, text="👻 תהליכים שננטשו", bg=PANEL, fg=GHOST,
             font=("Segoe UI", 15, "bold"), anchor="e").pack(fill="x")
    subtitle_var = tk.StringVar(value="סורק…")
    tk.Label(hdr, textvariable=subtitle_var, bg=PANEL, fg=DIM,
             font=("Segoe UI", 9), anchor="e").pack(fill="x", pady=(3, 0))

    # ---- scrollable body ----
    holder = tk.Frame(win, bg=BG)
    holder.pack(fill="both", expand=True)
    canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
    scrollbar = tk.Scrollbar(holder, orient="vertical", command=canvas.yview)
    body = tk.Frame(canvas, bg=BG)
    body.bind("<Configure>",
              lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=body, anchor="nw", width=716)
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=8)
    scrollbar.pack(side="right", fill="y")

    def _on_wheel(event: tk.Event) -> None:
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    canvas.bind_all("<MouseWheel>", _on_wheel)

    def _unbind_wheel() -> None:
        try:
            canvas.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass

    # ---- actions ----

    def _close_ghost(g: GhostProcess) -> None:
        # try_terminate asks for confirmation and refuses protected processes.
        if try_terminate(g.pid, g.name, win):
            sweeper.trigger_rescan()
            _refresh()

    def _ignore_instance(g: GhostProcess) -> None:
        sweeper.ignore_instance(g.instance_key)
        persist_ignore(instance_key=g.instance_key)
        sweeper.trigger_rescan()
        _refresh()

    def _ignore_name(g: GhostProcess) -> None:
        if not messagebox.askyesno(
            "התעלמות קבועה",
            f"להתעלם מעכשיו מכל תהליך בשם {g.name}?\n\n"
            f"אפשר לבטל בקובץ config.json תחת sweeper.ignored_names.",
            parent=win,
        ):
            return
        sweeper.ignore_name(g.name)
        persist_ignore(name=g.name)
        sweeper.trigger_rescan()
        _refresh()

    # ---- rendering ----

    def _row(container, label: str, value: str, *, mono: bool = False,
             fg: str = FG) -> None:
        if not value:
            return
        line = tk.Frame(container, bg=PANEL_HI)
        line.pack(fill="x", pady=1)
        tk.Label(line, text=label, bg=PANEL_HI, fg=DIM,
                 font=("Segoe UI", 8), width=10, anchor="e").pack(side="right")
        font = ("Cascadia Mono", 8) if mono else ("Segoe UI", 9)
        tk.Label(line, text=value, bg=PANEL_HI, fg=fg, font=font,
                 anchor="e", justify="right", wraplength=600
                 ).pack(side="right", fill="x", expand=True, padx=(0, 8))

    def _card(g: GhostProcess, sibling_count: int) -> None:
        colour = VERDICT_COLORS.get(g.verdict, GHOST)

        outer = tk.Frame(body, bg=BORDER)
        outer.pack(fill="x", pady=5, padx=4)
        inner = tk.Frame(outer, bg=PANEL_HI)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(inner, bg=colour, width=4).pack(side="left", fill="y")
        content = tk.Frame(inner, bg=PANEL_HI, padx=14, pady=10)
        content.pack(side="left", fill="both", expand=True)

        # Title line: name + PID on the right, verdict chip on the left
        top = tk.Frame(content, bg=PANEL_HI)
        top.pack(fill="x")
        tk.Label(top, text=VERDICT_LABELS.get(g.verdict, g.verdict),
                 bg=colour, fg=BG, font=("Segoe UI", 8, "bold"),
                 padx=7, pady=1).pack(side="left")
        tk.Label(top, text=f"{g.name}   ·   PID {g.pid}", bg=PANEL_HI, fg=FG,
                 font=("Segoe UI", 12, "bold"), anchor="e"
                 ).pack(side="right", fill="x", expand=True)

        # Why it was flagged
        flags = "  ".join(f"[{REASON_LABELS.get(r, r)}]" for r in g.reasons)
        tk.Label(content, text=flags, bg=PANEL_HI, fg=colour,
                 font=("Segoe UI", 8), anchor="e").pack(fill="x", pady=(3, 6))

        # The verdict sentence — the thing the user actually reads
        tk.Label(content, text=g.verdict_detail, bg=PANEL_HI, fg=FG,
                 font=("Segoe UI", 9), anchor="e", justify="right",
                 wraplength=620).pack(fill="x", pady=(0, 7))

        _row(content, "שושלת", describe_parent(g))
        if sibling_count:
            _row(content, "", f"עוד {sibling_count} תהליכים ננטשו מאותו הורה",
                 fg=DIM)
        _row(content, "פעילות", describe_activity(g))
        _row(content, "משאבים", describe_resources(g))
        _row(content, "הופעל", f"לפני {humanize_duration(g.age_seconds)} "
                               f"(בשעה {_fmt_clock(g.create_time)})")
        _row(content, "תיקייה", g.cwd or "— (אין גישה)", mono=True)
        _row(content, "פקודה", _shorten(g.cmdline, 200) or "— (אין גישה)",
             mono=True)
        if g.first_scan:
            _row(content, "", "זמנים מסומנים כ\"לפחות\" — התהליך כבר רץ "
                              "כשהמוניטור עלה, אז אלו הערכות תחתונות.", fg=DIM)

        # Actions
        actions = tk.Frame(content, bg=PANEL_HI)
        actions.pack(fill="x", pady=(9, 0))
        tk.Button(actions, text="✕  סגור תהליך", bg=PANEL_HI, fg=BAD,
                  font=("Segoe UI", 9, "bold"), relief="flat", bd=0,
                  activebackground=BAD, activeforeground="white",
                  cursor="hand2", padx=10, pady=3,
                  command=lambda gg=g: _close_ghost(gg)).pack(side="left")
        tk.Button(actions, text="הסתר הפעם", bg=MUTED_BTN, fg=DIM,
                  font=("Segoe UI", 8), relief="flat", bd=0,
                  activebackground=BORDER, activeforeground=FG,
                  cursor="hand2", padx=9, pady=3,
                  command=lambda gg=g: _ignore_instance(gg)
                  ).pack(side="left", padx=(8, 0))
        tk.Button(actions, text=f"התעלם תמיד מ-{g.name}", bg=MUTED_BTN, fg=DIM,
                  font=("Segoe UI", 8), relief="flat", bd=0,
                  activebackground=BORDER, activeforeground=FG,
                  cursor="hand2", padx=9, pady=3,
                  command=lambda gg=g: _ignore_name(gg)
                  ).pack(side="left", padx=(6, 0))

    def _refresh() -> None:
        for child in body.winfo_children():
            child.destroy()
        ghosts = sweeper.get_ghosts()

        last = sweeper.last_scan_time()
        when = f"נסרק ב-{_fmt_clock(last)}" if last else "טרם נסרק"
        subtitle_var.set(
            f"{len(ghosts)} תהליכים חשודים  ·  {when}  ·  "
            f"שום דבר לא נסגר בלי אישור שלך"
        )

        if not ghosts:
            tk.Label(body, text="✓ לא נמצאו תהליכים נטושים",
                     bg=BG, fg="#3fb950", font=("Segoe UI", 12), pady=30
                     ).pack(fill="x")
            tk.Label(body, text="המטאטא ימשיך לסרוק ברקע וידווח כשמשהו יישאר תלוי.",
                     bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(fill="x")
            return

        # How many other ghosts were orphaned by the same dead parent — a strong
        # hint that one crashed agent left a whole family behind.
        by_parent: dict[int, int] = {}
        for g in ghosts:
            if not g.parent_alive:
                by_parent[g.parent_pid] = by_parent.get(g.parent_pid, 0) + 1

        for g in ghosts:
            siblings = 0
            if not g.parent_alive:
                siblings = max(0, by_parent.get(g.parent_pid, 1) - 1)
            _card(g, siblings)

    # ---- footer ----
    ftr = tk.Frame(win, bg=PANEL, padx=14, pady=10)
    ftr.pack(fill="x", side="bottom")

    def _rescan() -> None:
        subtitle_var.set("סורק…")
        win.update_idletasks()
        sweeper.trigger_rescan()
        _refresh()

    tk.Button(ftr, text="🔄 סרוק עכשיו", bg=GHOST, fg=BG,
              font=("Segoe UI", 10, "bold"), relief="flat", bd=0,
              activebackground="#8b6fe8", activeforeground=BG,
              cursor="hand2", padx=14, pady=6, command=_rescan).pack(side="left")

    def _close_window() -> None:
        _unbind_wheel()
        win.destroy()

    tk.Button(ftr, text="סגור", bg=PANEL_HI, fg=FG,
              font=("Segoe UI", 9), relief="flat", bd=0, cursor="hand2",
              padx=12, pady=6, command=_close_window).pack(side="right")
    win.protocol("WM_DELETE_WINDOW", _close_window)

    _refresh()
    return win
