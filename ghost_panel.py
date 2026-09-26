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
    VERDICT_COLORS,
    GhostProcess,
    get_default_sweeper,
    humanize_bytes,
    humanize_duration,
    persist_ignore,
    redact_cmdline,
    reason_label,
    secrets_are_redacted,
    set_secrets_redacted,
    verdict_label,
)
from i18n import ANCHOR, JUSTIFY, SIDE, SIDE_END, tr

log = logging.getLogger(__name__)

# Theme — matches alerts.py / janitor.py / server_scanner.py
BG, PANEL, PANEL_HI = "#0d1117", "#161b22", "#1c2230"
BORDER, FG, DIM = "#30363d", "#e6edf3", "#7d8590"
BAD, MUTED_BTN = "#ff5c6c", "#2d333b"
GHOST = "#a78bfa"          # the sweeper's signature violet
CLEAN = "#3fb950"          # "clean all" green
OK_FG, WARN_FG = "#3fb950", "#f0c674"


def _run_in_background(win, work, on_done) -> None:
    """Run `work()` on a thread; deliver its result (or exception) to
    `on_done` on the Tk thread. Tk must only be touched from the thread that
    created it, so the hand-off is a polled slot, not a call from the worker."""
    import threading

    slot: list = []

    def _worker() -> None:
        try:
            slot.append(work())
        except BaseException as exc:          # delivered to the UI, not lost
            log.exception("Background task failed")
            slot.append(exc)

    threading.Thread(target=_worker, name="ghost-panel-task", daemon=True).start()

    def _poll() -> None:
        if slot:
            try:
                on_done(slot[0])
            except Exception:
                # Typically the window was closed while the task ran.
                log.debug("Background task result arrived after its window closed",
                          exc_info=True)
            return
        try:
            win.after(100, _poll)
        except Exception:
            pass                                # window closed meanwhile

    win.after(100, _poll)


def _open_clean_dialog(parent, ghosts, hogs, note: str, claude_note: str,
                       *, on_finished) -> None:
    """Confirm window for 'clean all': every item is listed and can be
    unticked; nothing closes until the user presses the button."""
    import tkinter as tk

    from idle_cleanup import MIB, close_targets, verify_idle

    dlg = tk.Toplevel(parent)
    dlg.title(tr("נקה הכל — רק מה שלא פעיל", "Clean all — idle only"))
    dlg.geometry("640x560")
    dlg.configure(bg=BG)
    try:
        dlg.attributes("-topmost", True)
    except tk.TclError:
        pass
    dlg.transient(parent)

    hdr = tk.Frame(dlg, bg=PANEL, padx=16, pady=10)
    hdr.pack(fill="x")
    tk.Label(hdr, text=tr("🧹 נקה הכל — רק מה שלא פעיל", "🧹 Clean all — idle only"),
             bg=PANEL, fg=CLEAN,
             font=("Segoe UI", 13, "bold"), anchor=ANCHOR()).pack(fill="x")
    tk.Label(hdr, text=tr("מה שפעיל לא מופיע כאן בכלל. לפני הסגירה כל תהליך נבדק "
                          "שוב, ומה שהתעורר בינתיים — לא ייסגר.",
                          "Anything active is not listed here at all. Each process is "
                          "checked again before closing, and whatever woke up meanwhile "
                          "will not be closed."),
             bg=PANEL, fg=DIM, font=("Segoe UI", 9), anchor=ANCHOR(),
             justify=JUSTIFY(), wraplength=600).pack(fill="x", pady=(3, 0))

    holder = tk.Frame(dlg, bg=BG)
    holder.pack(fill="both", expand=True)
    canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
    bar = tk.Scrollbar(holder, orient="vertical", command=canvas.yview)
    body = tk.Frame(canvas, bg=BG)
    body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=body, anchor="nw", width=600)
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=6)
    bar.pack(side="right", fill="y")

    checks: list[tuple[tk.BooleanVar, object]] = []
    summary_var = tk.StringVar()

    def _update_summary() -> None:
        chosen = [t for v, t in checks if v.get()]
        mb = sum(t.rss_bytes for t in chosen) // MIB
        summary_var.set(tr(f"נבחרו {len(chosen)}  ·  ישתחררו כ-{mb:,} MB",
                           f"{len(chosen)} selected  ·  about {mb:,} MB will be freed"))
        go_btn.config(state="normal" if chosen else "disabled",
                      text=tr(f"סגור את המסומנים ({len(chosen)})",
                              f"Close selected ({len(chosen)})"))

    def _section(title: str, items) -> None:
        if not items:
            return
        tk.Label(body, text=title, bg=BG, fg=FG, font=("Segoe UI", 10, "bold"),
                 anchor=ANCHOR()).pack(fill="x", pady=(8, 2), padx=6)
        for t in items:
            var = tk.BooleanVar(value=True)
            checks.append((var, t))
            row = tk.Frame(body, bg=PANEL_HI, padx=10, pady=5)
            row.pack(fill="x", pady=2, padx=4)
            tk.Checkbutton(row, variable=var, command=_update_summary,
                           bg=PANEL_HI, activebackground=PANEL_HI,
                           selectcolor=PANEL, relief="flat", bd=0,
                           highlightthickness=0).pack(side=SIDE())
            tk.Label(row, text=f"{t.name}  ·  {t.rss_bytes // MIB:,} MB  ·  PID {t.pid}",
                     bg=PANEL_HI, fg=FG, font=("Segoe UI", 10, "bold"),
                     anchor=ANCHOR()).pack(side="top", fill="x")
            tk.Label(row, text=t.detail, bg=PANEL_HI, fg=DIM, font=("Segoe UI", 8),
                     anchor=ANCHOR(), justify=JUSTIFY(), wraplength=540
                     ).pack(side="top", fill="x")

    _section(tr("👻 תהליכים נטושים שסיימו", "👻 Abandoned processes that finished"),
             ghosts)
    _section(tr("💤 תופסים זיכרון ולא פעילים", "💤 Idle and holding memory"), hogs)
    if not ghosts and not hogs:
        tk.Label(body, text=tr("✓ אין כרגע שום דבר לא פעיל שכדאי לסגור",
                               "✓ Nothing idle worth closing right now"),
                 bg=BG, fg=OK_FG, font=("Segoe UI", 11), pady=24).pack(fill="x")
    for text in (note, claude_note):
        if text:
            tk.Label(body, text=text, bg=BG, fg=DIM, font=("Segoe UI", 8),
                     anchor=ANCHOR(), justify=JUSTIFY(), wraplength=580
                     ).pack(fill="x", pady=(8, 0), padx=6)

    ftr = tk.Frame(dlg, bg=PANEL, padx=14, pady=10)
    ftr.pack(fill="x", side="bottom")
    tk.Label(ftr, textvariable=summary_var, bg=PANEL, fg=DIM,
             font=("Segoe UI", 9), anchor=ANCHOR()).pack(side="top", fill="x", pady=(0, 6))
    go_btn = tk.Button(ftr, bg=BAD, fg="white", font=("Segoe UI", 10, "bold"),
                       relief="flat", bd=0, cursor="hand2", padx=14, pady=6,
                       activebackground="#d94452", activeforeground="white")
    go_btn.pack(side="left")
    cancel_btn = tk.Button(ftr, text=tr("ביטול", "Cancel"), bg=PANEL_HI, fg=FG,
                           font=("Segoe UI", 9), relief="flat", bd=0,
                           cursor="hand2", padx=12, pady=6, command=dlg.destroy)
    cancel_btn.pack(side="right")

    def _show_results(result) -> None:
        for child in body.winfo_children():
            child.destroy()
        cancel_btn.config(text=tr("סגור", "Close"))
        if isinstance(result, BaseException):
            summary_var.set(tr(f"הניקוי נכשל: {result}", f"Cleanup failed: {result}"))
            return
        closed, skipped = result
        done = [t for t, outcome in closed if outcome in ("closed", "already_gone")]
        failed = [(t, o) for t, o in closed if o not in ("closed", "already_gone")]
        mb = sum(t.rss_bytes for t in done) // MIB
        summary_var.set(tr(f"✓ נסגרו {len(done)}  ·  שוחררו כ-{mb:,} MB",
                           f"✓ Closed {len(done)}  ·  about {mb:,} MB freed"))
        for t in done:
            tk.Label(body, text=tr(f"✓ {t.name} (PID {t.pid}) — נסגר",
                                   f"✓ {t.name} (PID {t.pid}) — closed"),
                     bg=BG, fg=OK_FG, font=("Segoe UI", 9), anchor=ANCHOR()
                     ).pack(fill="x", padx=6)
        for t, why in skipped:
            tk.Label(body, text=tr(f"⏸ {t.name} (PID {t.pid}) — לא נסגר: {why}",
                                   f"⏸ {t.name} (PID {t.pid}) — not closed: {why}"),
                     bg=BG, fg=WARN_FG, font=("Segoe UI", 9), anchor=ANCHOR()
                     ).pack(fill="x", padx=6)
        for t, outcome in failed:
            why = {"denied": tr("אין הרשאה (דרוש מנהל)",
                                "access denied (administrator required)"),
                   "protected": tr("תהליך מוגן", "protected process"),
                   "pid_reused": tr("המספר עבר לתהליך אחר",
                                    "the PID now belongs to another process"),
                   }.get(outcome, outcome)
            tk.Label(body, text=f"✕ {t.name} (PID {t.pid}) — {why}",
                     bg=BG, fg=BAD, font=("Segoe UI", 9), anchor=ANCHOR()
                     ).pack(fill="x", padx=6)
        on_finished()

    def _go() -> None:
        chosen = [t for v, t in checks if v.get()]
        if not chosen:
            return
        go_btn.config(state="disabled", text=tr("בודק…", "Checking…"))
        cancel_btn.config(state="disabled")
        summary_var.set(tr("מוודא שכל תהליך עדיין לא פעיל (3 שניות)…",
                           "Making sure each process is still idle (3 seconds)…"))

        def _work():
            ok, skipped = verify_idle(chosen)
            return close_targets(ok), skipped

        def _done(result) -> None:
            cancel_btn.config(state="normal")
            go_btn.pack_forget()
            _show_results(result)

        _run_in_background(dlg, _work, _done)

    go_btn.config(command=_go)
    _update_summary()


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
        return tr(f"רץ תחת {g.parent_name} (PID {g.parent_pid}) — ההורה עדיין חי",
                  f"Running under {g.parent_name} (PID {g.parent_pid}) — "
                  f"the parent is still alive")
    name = g.parent_name if g.parent_name and g.parent_name != "?" else None
    when = humanize_duration(g.orphaned_for_seconds)
    prefix = tr("לפחות ", "at least ") if g.orphan_time_is_lower_bound else ""
    if name:
        return tr(f"נוצר ע\"י {name} (PID {g.parent_pid}) — שנסגר לפני {prefix}{when}",
                  f"Started by {name} (PID {g.parent_pid}) — which exited "
                  f"{prefix}{when} ago")
    return tr(f"ההורה (PID {g.parent_pid}) נסגר לפני {prefix}{when} — "
              f"הוא כבר לא היה קיים כשהמוניטור עלה, אז שמו לא ידוע",
              f"The parent (PID {g.parent_pid}) exited {prefix}{when} ago — "
              f"it was already gone when the monitor started, so its name is unknown")


def describe_activity(g: GhostProcess) -> str:
    """One line answering 'did it finish, or is it stuck?'."""
    prefix = tr("לפחות ", "at least ") if g.idle_time_is_lower_bound else ""
    idle = humanize_duration(g.idle_for_seconds)
    cpu_life = f"{g.cpu_percent_lifetime:.2f}%"
    total = humanize_duration(g.cpu_seconds_total)
    return tr(f"לא עשה כלום כבר {prefix}{idle}  ·  "
              f"בכל חייו צרך {total} של CPU ({cpu_life} מליבה)",
              f"Idle for {prefix}{idle}  ·  "
              f"used {total} of CPU over its whole life ({cpu_life} of a core)")


def describe_resources(g: GhostProcess) -> str:
    bits = [tr(f"זיכרון {humanize_bytes(g.rss_bytes)}",
               f"Memory {humanize_bytes(g.rss_bytes)}")]
    if abs(g.rss_delta_bytes) >= 1024 * 1024:
        sign = "+" if g.rss_delta_bytes > 0 else "−"
        bits.append(tr(f"שינוי {sign}{humanize_bytes(abs(g.rss_delta_bytes))} מאז המעקב",
                       f"change {sign}{humanize_bytes(abs(g.rss_delta_bytes))} "
                       f"since tracking began"))
    bits.append(f"{g.num_threads} threads")
    if g.listening_ports:
        ports = ", ".join(str(p) for p in g.listening_ports[:5])
        if len(g.listening_ports) > 5:
            ports += f" (+{len(g.listening_ports) - 5})"
        bits.append(tr(f"מאזין ל-{ports}", f"listening on {ports}"))
        bits.append(tr(f"{g.established_connections} חיבורים פעילים",
                       f"{g.established_connections} active connections"))
    return "  ·  ".join(bits)


def open_ghost_panel(parent) -> object:
    """Open the sweeper's report window. Returns the Toplevel."""
    import tkinter as tk
    from tkinter import messagebox

    from alerts import try_terminate   # confirmation + protected-process guard

    sweeper = get_default_sweeper()

    win = tk.Toplevel(parent) if parent is not None else tk.Tk()
    win.title(tr("רוחות רפאים — תהליכים שננטשו", "Ghosts — abandoned processes"))
    win.geometry("760x620")
    win.configure(bg=BG)
    try:
        win.attributes("-topmost", True)
    except tk.TclError:
        pass

    # ---- header ----
    hdr = tk.Frame(win, bg=PANEL, padx=18, pady=12)
    hdr.pack(fill="x")
    tk.Label(hdr, text=tr("👻 תהליכים שננטשו", "👻 Abandoned processes"),
             bg=PANEL, fg=GHOST,
             font=("Segoe UI", 15, "bold"), anchor=ANCHOR()).pack(fill="x")
    subtitle_var = tk.StringVar(value=tr("סורק…", "Scanning…"))
    tk.Label(hdr, textvariable=subtitle_var, bg=PANEL, fg=DIM,
             font=("Segoe UI", 9), anchor=ANCHOR()).pack(fill="x", pady=(3, 0))

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

    # Masking of secrets inside displayed command lines. Read once when the
    # window opens; the checkbox below writes changes straight back to
    # config.json so the choice survives a restart.
    redact_var = tk.BooleanVar(value=secrets_are_redacted())

    # ---- actions ----

    def _close_ghost(g: GhostProcess) -> None:
        # try_terminate asks for confirmation and refuses protected processes.
        if try_terminate(g.pid, g.name, win):
            sweeper.log_user_action(g, "closed")
            sweeper.trigger_rescan()
            _refresh()

    def _ignore_instance(g: GhostProcess) -> None:
        sweeper.ignore_instance(g.instance_key)
        persist_ignore(instance_key=g.instance_key)
        sweeper.log_user_action(g, "hidden once")
        sweeper.trigger_rescan()
        _refresh()

    def _ignore_name(g: GhostProcess) -> None:
        if not messagebox.askyesno(
            tr("התעלמות קבועה", "Always ignore"),
            tr(f"להתעלם מעכשיו מכל תהליך בשם {g.name}?\n\n"
               f"אפשר לבטל בקובץ config.json תחת sweeper.ignored_names.",
               f"Ignore every process named {g.name} from now on?\n\n"
               f"You can undo this in config.json under sweeper.ignored_names."),
            parent=win,
        ):
            return
        sweeper.ignore_name(g.name)
        persist_ignore(name=g.name)
        sweeper.log_user_action(g, "ignored by name")
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
                 font=("Segoe UI", 8), width=10, anchor=ANCHOR()).pack(side=SIDE())
        font = ("Cascadia Mono", 8) if mono else ("Segoe UI", 9)
        tk.Label(line, text=value, bg=PANEL_HI, fg=fg, font=font,
                 anchor=ANCHOR(), justify=JUSTIFY(), wraplength=600
                 ).pack(side=SIDE(), fill="x", expand=True, padx=(0, 8))

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
        tk.Label(top, text=verdict_label(g.verdict),
                 bg=colour, fg=BG, font=("Segoe UI", 8, "bold"),
                 padx=7, pady=1).pack(side=SIDE_END())
        tk.Label(top, text=f"{g.name}   ·   PID {g.pid}", bg=PANEL_HI, fg=FG,
                 font=("Segoe UI", 12, "bold"), anchor=ANCHOR()
                 ).pack(side=SIDE(), fill="x", expand=True)

        # Why it was flagged
        flags = "  ".join(f"[{reason_label(r)}]" for r in g.reasons)
        tk.Label(content, text=flags, bg=PANEL_HI, fg=colour,
                 font=("Segoe UI", 8), anchor=ANCHOR()).pack(fill="x", pady=(3, 6))

        # The verdict sentence — the thing the user actually reads
        tk.Label(content, text=g.verdict_detail, bg=PANEL_HI, fg=FG,
                 font=("Segoe UI", 9), anchor=ANCHOR(), justify=JUSTIFY(),
                 wraplength=620).pack(fill="x", pady=(0, 7))

        no_access = tr("— (אין גישה)", "— (no access)")
        _row(content, tr("שושלת", "Lineage"), describe_parent(g))
        if sibling_count:
            _row(content, "", tr(f"עוד {sibling_count} תהליכים ננטשו מאותו הורה",
                                 f"{sibling_count} more processes were abandoned "
                                 f"by the same parent"),
                 fg=DIM)
        _row(content, tr("פעילות", "Activity"), describe_activity(g))
        _row(content, tr("משאבים", "Resources"), describe_resources(g))
        _row(content, tr("הופעל", "Started"),
             tr(f"לפני {humanize_duration(g.age_seconds)} "
                f"(בשעה {_fmt_clock(g.create_time)})",
                f"{humanize_duration(g.age_seconds)} ago "
                f"(at {_fmt_clock(g.create_time)})"))
        _row(content, tr("תיקייה", "Folder"), g.cwd or no_access, mono=True)
        # Redact before shortening, so a truncated secret can never survive
        # the ellipsis.
        cmd = g.cmdline
        if redact_var.get():
            cmd = redact_cmdline(cmd)
        _row(content, tr("פקודה", "Command"), _shorten(cmd, 200) or no_access,
             mono=True)
        if g.first_scan:
            _row(content, "", tr("זמנים מסומנים כ\"לפחות\" — התהליך כבר רץ "
                                 "כשהמוניטור עלה, אז אלו הערכות תחתונות.",
                                 "Times marked \"at least\" — the process was already "
                                 "running when the monitor started, so these are lower "
                                 "bounds."), fg=DIM)

        # Actions
        actions = tk.Frame(content, bg=PANEL_HI)
        actions.pack(fill="x", pady=(9, 0))
        tk.Button(actions, text=tr("✕  סגור תהליך", "✕  Close process"),
                  bg=PANEL_HI, fg=BAD,
                  font=("Segoe UI", 9, "bold"), relief="flat", bd=0,
                  activebackground=BAD, activeforeground="white",
                  cursor="hand2", padx=10, pady=3,
                  command=lambda gg=g: _close_ghost(gg)).pack(side="left")
        tk.Button(actions, text=tr("הסתר הפעם", "Hide this time"),
                  bg=MUTED_BTN, fg=DIM,
                  font=("Segoe UI", 8), relief="flat", bd=0,
                  activebackground=BORDER, activeforeground=FG,
                  cursor="hand2", padx=9, pady=3,
                  command=lambda gg=g: _ignore_instance(gg)
                  ).pack(side="left", padx=(8, 0))
        tk.Button(actions, text=tr(f"התעלם תמיד מ-{g.name}", f"Always ignore {g.name}"),
                  bg=MUTED_BTN, fg=DIM,
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
        when = (tr(f"נסרק ב-{_fmt_clock(last)}", f"scanned at {_fmt_clock(last)}")
                if last else tr("טרם נסרק", "not scanned yet"))
        subtitle_var.set(tr(
            f"{len(ghosts)} תהליכים חשודים  ·  {when}  ·  "
            f"שום דבר לא נסגר בלי אישור שלך",
            f"{len(ghosts)} suspect processes  ·  {when}  ·  "
            f"nothing is closed without your approval"
        ))

        if not ghosts:
            tk.Label(body, text=tr("✓ לא נמצאו תהליכים נטושים",
                                   "✓ No abandoned processes found"),
                     bg=BG, fg="#3fb950", font=("Segoe UI", 12), pady=30
                     ).pack(fill="x")
            tk.Label(body, text=tr("המטאטא ימשיך לסרוק ברקע וידווח כשמשהו יישאר תלוי.",
                                   "The sweeper keeps scanning in the background and "
                                   "will report when something is left hanging."),
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
        subtitle_var.set(tr("סורק…", "Scanning…"))
        win.update_idletasks()
        sweeper.trigger_rescan()
        _refresh()

    tk.Button(ftr, text=tr("🔄 סרוק עכשיו", "🔄 Scan now"), bg=GHOST, fg=BG,
              font=("Segoe UI", 10, "bold"), relief="flat", bd=0,
              activebackground="#8b6fe8", activeforeground=BG,
              cursor="hand2", padx=14, pady=6, command=_rescan).pack(side="left")

    clean_btn = tk.Button(ftr, text=tr("🧹 נקה הכל", "🧹 Clean all"),
                          bg=PANEL_HI, fg=CLEAN,
                          font=("Segoe UI", 10, "bold"), relief="flat", bd=0,
                          activebackground=CLEAN, activeforeground=BG,
                          cursor="hand2", padx=14, pady=6)
    clean_btn.pack(side="left", padx=(8, 0))

    def _clean_all() -> None:
        clean_btn.config(state="disabled", text=tr("🧹 בודק…", "🧹 Checking…"))

        def _gather():
            from idle_cleanup import (claude_sessions_note, ghost_targets,
                                      idle_hog_targets)
            from process_monitor import peek_default_sampler
            ghosts = ghost_targets(sweeper.get_ghosts())
            taken = {t.pid for t in ghosts}
            hogs, note = idle_hog_targets(peek_default_sampler(), exclude_pids=taken)
            return ghosts, hogs, note, claude_sessions_note(taken)

        def _done(result) -> None:
            clean_btn.config(state="normal", text=tr("🧹 נקה הכל", "🧹 Clean all"))
            if isinstance(result, BaseException):
                messagebox.showerror(tr("נקה הכל", "Clean all"),
                                     tr(f"החיפוש נכשל:\n{result}",
                                        f"The search failed:\n{result}"),
                                     parent=win)
                return
            _open_clean_dialog(win, *result, on_finished=_after_clean)

        _run_in_background(win, _gather, _done)

    def _after_clean() -> None:
        _run_in_background(win, sweeper.trigger_rescan, lambda _r: _refresh())

    clean_btn.config(command=_clean_all)

    def _toggle_redaction() -> None:
        set_secrets_redacted(redact_var.get())
        _refresh()          # no rescan needed: masking is a display concern

    tk.Checkbutton(
        ftr, text=tr("🔒 הסתר סיסמאות וטוקנים בשורת הפקודה",
                     "🔒 Mask passwords and tokens in the command line"),
        variable=redact_var, command=_toggle_redaction,
        bg=PANEL, fg=DIM, font=("Segoe UI", 9),
        activebackground=PANEL, activeforeground=FG,
        selectcolor=PANEL_HI, relief="flat", bd=0,
        highlightthickness=0, cursor="hand2", anchor=ANCHOR(),
    ).pack(side="left", padx=(12, 0))

    def _open_log() -> None:
        try:
            from metric_history import DEFAULT_HISTORY_DIR
            from tray_runner import _open_path
            _open_path(DEFAULT_HISTORY_DIR / "sweeper.log")
        except Exception:
            log.exception("Could not open the sweep log")

    tk.Button(ftr, text=tr("📄 יומן סריקות", "📄 Scan log"), bg=PANEL_HI, fg=DIM,
              font=("Segoe UI", 9), relief="flat", bd=0,
              activebackground=BORDER, activeforeground=FG,
              cursor="hand2", padx=12, pady=6,
              command=_open_log).pack(side="left", padx=(12, 0))

    def _close_window() -> None:
        _unbind_wheel()
        win.destroy()

    tk.Button(ftr, text=tr("סגור", "Close"), bg=PANEL_HI, fg=FG,
              font=("Segoe UI", 9), relief="flat", bd=0, cursor="hand2",
              padx=12, pady=6, command=_close_window).pack(side="right")
    win.protocol("WM_DELETE_WINDOW", _close_window)

    _refresh()
    return win
