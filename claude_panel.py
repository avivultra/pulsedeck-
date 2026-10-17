"""The "Ask Claude" window: shows Claude's take on the current snapshot.

Kept separate from any one main window so whichever design hosts the button
only has to pass a function that builds the snapshot. The request runs on a
worker thread; the Tk thread polls a queue, so the UI never freezes while
Claude thinks.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from typing import Callable

import i18n
from i18n import tr

log = logging.getLogger(__name__)

BG = "#09090b"
PANEL = "#18181b"
FG = "#fafafa"
DIM = "#a1a1aa"
ACCENT = "#d97757"
FONT = "Segoe UI"


_ACTION_LABELS = {
    "lower": lambda n: tr(f"הורד עדיפות ל-{n}", f"Lower {n}'s priority"),
    "restore": lambda n: tr(f"החזר עדיפות ל-{n}", f"Restore {n}'s priority"),
    "close": lambda n: tr(f"סגור את {n}", f"Close {n}"),
    "force": lambda n: tr(f"סגור את {n} בכוח", f"Force-close {n}"),
}


def _confirm_text(action: str, n: str) -> tuple[str, str, str, bool]:
    """(title, text, OK label, dangerous) of the dialog before an action.
    Hebrew lines start with a Hebrew word, so they read right to left."""
    if action == "lower":
        return (tr(f"להוריד עדיפות ל-{n}?", f"Lower {n}'s priority?"),
                tr(f"התוכנה {n} תמשיך לעבוד, ורק תפנה מקום לתוכנות אחרות כשהמעבד עמוס.\n"
                   "אפשר להחזיר בכל רגע, והעדיפות חוזרת לבד כשהיא נפתחת מחדש.",
                   f"{n} keeps working; it only gives way to other programs when the CPU is busy.\n"
                   "You can undo it any time, and it resets when the program restarts."),
                tr("הורד עדיפות", "Lower"), False)
    if action == "restore":
        return (tr(f"להחזיר את העדיפות של {n}?", f"Restore {n}'s priority?"),
                tr("העדיפות תחזור למה שהייתה לפני ש-PulseDeck הוריד אותה.",
                   "Its priority goes back to what it was before PulseDeck lowered it."),
                tr("החזר", "Restore"), False)
    if action == "close":
        return (tr(f"לסגור את {n}?", f"Close {n}?"),
                tr("החלונות שלה יקבלו בקשה להיסגר, כמו לחיצה על X.\n"
                   "אם יש עבודה שלא נשמרה, התוכנה תשאל אותך לפני שהיא נסגרת.",
                   "Its windows are asked to close, like clicking X.\n"
                   "If there is unsaved work, the program asks you before it closes."),
                tr("סגור", "Close"), True)
    return (tr(f"לסגור את {n} בכוח?", f"Force-close {n}?"),
            tr("היא לא נסגרה כרגיל.\nסגירה בכוח עוצרת אותה מיד, ועבודה שלא נשמרה תאבד.",
               "It did not close normally.\nA forced close stops it at once; unsaved work is lost."),
            tr("סגור בכוח", "Force close"), True)


def _plain(text: str) -> str:
    """Claude's answer without Markdown marks: the window shows plain text,
    where "**bold**" would keep its asterisks."""
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.replace("**", "").replace("__", "").replace("`", "").rstrip()
        bare = line.lstrip()
        if bare.startswith("#"):
            line = bare.lstrip("#").strip()
        elif bare[:2] in ("- ", "* "):
            line = "• " + bare[2:]
        out.append(line)
    return "\n".join(out).strip()


def _has_hebrew(text: str) -> bool:
    return any("֐" <= ch <= "׿" for ch in text)


def _lay_out(text: str, font: tuple, width: int) -> str:
    """Every Hebrew paragraph broken into lines that fit `width`, each line
    embedded right to left (dark_menu._rtl_lines). Tk's own wrapping is not
    enough on Windows: each drawn line is laid out left to right, so a
    wrapped Hebrew line loses its order and its first letters. A paragraph
    that starts with an English name ("PowerShell פותח...") is still Hebrew.
    Other paragraphs are left to Tk."""
    if not i18n.is_rtl():
        return text
    from dark_menu import _rtl_lines
    out: list[str] = []
    for para in text.split("\n"):
        out.extend(_rtl_lines(para, font, width) if _has_hebrew(para) else [para])
    return "\n".join(out)


def open_claude_panel(parent: tk.Misc, snapshot_fn: Callable[[], dict],
                      first_question: str | None = None) -> tk.Toplevel:
    """Open the panel and immediately ask `first_question` (default: "what is
    going on right now?")."""
    import ai_advisor

    win = tk.Toplevel(parent)
    win.title(tr("שאל את Claude", "Ask Claude"))
    win.geometry("520x480")
    win.configure(bg=BG)
    anchor, justify = i18n.ANCHOR(), i18n.JUSTIFY()

    head = tk.Frame(win, bg=BG, padx=16, pady=12)
    head.pack(fill="x")
    tk.Label(head, text="✳ Claude", bg=BG, fg=ACCENT, font=(FONT, 13, "bold"),
             anchor=anchor).pack(fill="x")
    tk.Label(head, text=tr("נשלחים רק שמות תוכנות ומספרים — בלי שורות פקודה או קבצים.",
                           "Only program names and numbers are sent — no command lines or files."),
             bg=BG, fg=DIM, font=(FONT, 8), anchor=anchor).pack(fill="x")

    body = tk.Text(win, bg=PANEL, fg=FG, font=(FONT, 10), wrap="word", relief="flat",
                   padx=14, pady=12, height=14, insertbackground=FG)
    body.tag_configure("dir", justify=justify)
    body.tag_configure("dim", foreground=DIM, justify=justify)
    body.pack(fill="both", expand=True, padx=16)
    usage_var = tk.StringVar()
    usage_label = tk.Label(win, textvariable=usage_var, bg=BG, fg=DIM, font=(FONT, 8), anchor=anchor)
    usage_label.pack(fill="x", padx=16, pady=(4, 0))
    # Claude's suggested actions: one button each, every one confirmed first.
    acts = tk.Frame(win, bg=BG)
    acts.pack(fill="x", padx=16, pady=(8, 0), before=usage_label)

    def _show_usage() -> None:
        try:
            import claude_spend
            usage_var.set(i18n.bidi(ai_advisor.default_budget().summary() + " · "
                                    + claude_spend.default_ledger().summary()))
        except Exception:
            log.debug("Usage summary unavailable", exc_info=True)
    _show_usage()

    ask_row = tk.Frame(win, bg=BG, padx=16, pady=12)
    ask_row.pack(fill="x")
    entry = tk.Entry(ask_row, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                     font=(FONT, 10), justify=justify)
    send = tk.Button(ask_row, text=tr("שאל", "Ask"), bg=ACCENT, fg="#ffffff", relief="flat",
                     activebackground=ACCENT, font=(FONT, 10, "bold"), padx=14, cursor="hand2")
    send.pack(side=i18n.SIDE_END())
    entry.pack(side=i18n.SIDE(), fill="x", expand=True, ipady=5, padx=(0, 8) if
               i18n.SIDE() == "left" else (8, 0))

    results: queue.Queue = queue.Queue()
    busy = {"on": False}
    shown = {"text": "", "tag": "dir", "width": 0}

    def _render() -> None:
        # The width Tk wraps at: the widget minus its padding and border,
        # less a margin so a measured line never wraps a second time.
        width = body.winfo_width()
        shown["width"] = width
        if width <= 1:                      # not on screen yet
            width = 520 - 2 * 16
        width -= 2 * 14 + 2 * int(body.cget("borderwidth")) + 16
        body.configure(state="normal")
        body.delete("1.0", "end")
        body.insert("1.0", _lay_out(shown["text"], (FONT, 10), width), shown["tag"])
        body.configure(state="disabled")

    def _show(text: str, tag: str = "dir") -> None:
        shown.update(text=_plain(text), tag=tag)
        _render()

    body.bind("<Configure>", lambda e: e.width != shown["width"] and _render())

    def _ask(question: str | None = None) -> None:
        if busy["on"]:
            return
        busy["on"] = True
        send.configure(state="disabled")
        _show(tr("Claude חושב…", "Claude is thinking…"), "dim")
        try:
            snapshot = snapshot_fn()
        except Exception:
            log.exception("Could not build the snapshot for Claude")
            snapshot = {}

        def _work() -> None:
            try:
                import load_actions
                snapshot.setdefault("load_last_minute", load_actions.for_claude(load_actions.heavy_programs()[0]))
            except Exception:
                log.exception("Could not see who loads the computer")
            actions: list = []
            try:
                results.put(("ok", ai_advisor.ask_claude(
                    snapshot, question, language=i18n.language(), actions_out=actions), actions))
            except ai_advisor.AdvisorError as e:
                results.put(("err", str(e), []))
            except Exception:
                log.exception("Ask Claude failed")
                results.put(("err", tr("משהו השתבש. הפרטים ב-history/monitor.log.",
                                       "Something went wrong. Details in history/monitor.log."), []))

        threading.Thread(target=_work, name="ask-claude", daemon=True).start()
        win.after(150, _poll)

    def _poll() -> None:
        if not win.winfo_exists():
            return
        try:
            kind, text, actions = results.get_nowait()
        except queue.Empty:
            win.after(150, _poll)
            return
        busy["on"] = False
        _show_usage()
        send.configure(state="normal")
        _show(text, "dir" if kind == "ok" else "dim")
        _show_actions(actions)

    def _show_actions(actions: list[dict]) -> None:
        for w in acts.winfo_children():
            w.destroy()
        for a in actions:
            row = tk.Frame(acts, bg=BG)
            row.pack(fill="x", pady=(0, 6))
            btn = tk.Label(row, text=i18n.bidi(_ACTION_LABELS[a["action"]](a["program"])), bg="#2d211c",
                           fg=ACCENT, font=(FONT, 9, "bold"), padx=10, pady=4, cursor="hand2")
            btn.pack(anchor=anchor)
            if a.get("why"):
                for line in _lines(a["why"]):
                    tk.Label(row, text=line, bg=BG, fg=DIM, font=(FONT, 8), anchor=anchor).pack(fill="x")
            status = tk.Label(row, text="", bg=BG, fg=FG, font=(FONT, 8), anchor=anchor, justify=justify)
            status.pack(fill="x")
            btn.bind("<Button-1>", lambda _e, a=a, b=btn, s=status: _ask_first(a["action"], a["program"], b, s))

    def _lines(text: str) -> list[str]:
        if i18n.is_rtl():
            from dark_menu import _rtl_lines
            return _rtl_lines(text, (FONT, 8), 470)
        return [text]

    def _ask_first(action: str, name: str, btn: tk.Label, status: tk.Label) -> None:
        """Nothing runs before the user says yes, here, for this one program."""
        from dark_menu import confirm
        title, text, ok, danger = _confirm_text(action, name)
        confirm(win, title, text, ok, lambda yes: yes and _run(action, name, btn, status), danger=danger)

    def _run(action: str, name: str, btn: tk.Label, status: tk.Label) -> None:
        import load_actions
        fn = {"lower": load_actions.lower, "restore": load_actions.restore,
              "close": load_actions.close, "force": load_actions.force_close}[action]
        btn.unbind("<Button-1>")
        btn.configure(text=tr("רגע…", "Working…"), cursor="arrow")
        done: queue.Queue = queue.Queue()

        def _work() -> None:
            try:
                done.put(fn(name))
            except Exception:
                log.exception("%s %s failed", action, name)
                done.put(None)

        def _wait() -> None:
            if not win.winfo_exists():
                return
            try:
                result = done.get_nowait()
            except queue.Empty:
                win.after(150, _wait)
                return
            status.configure(text="\n".join(_lines(result.text() if result else tr(
                "משהו השתבש. הפרטים ב-history/monitor.log.", "Something went wrong. Details in history/monitor.log."))))
            if result is not None and result.can_force:
                btn.configure(text=i18n.bidi(tr(f"סגור את {name} בכוח…", f"Force-close {name}…")),
                              fg="#ffb3b6", bg="#4a1f22", cursor="hand2")
                btn.bind("<Button-1>", lambda _e: _ask_first("force", name, btn, status))
            else:
                btn.configure(text="✓ " + i18n.bidi(_ACTION_LABELS[action](name)), fg=DIM, bg=PANEL)

        threading.Thread(target=_work, name=f"load-{action}", daemon=True).start()
        win.after(150, _wait)

    def _on_send(_e=None) -> None:
        q = entry.get().strip()
        entry.delete(0, "end")
        _ask(q or None)

    send.configure(command=_on_send)
    entry.bind("<Return>", _on_send)

    # No key yet: a button to the setup window. Once a key is saved there,
    # this window asks its first question by itself.
    waiting = {"key": not ai_advisor.is_configured()}

    def _key_changed() -> None:
        if not waiting["key"] or not win.winfo_exists() or not ai_advisor.is_configured():
            return
        waiting["key"] = False
        for w in acts.winfo_children():
            w.destroy()
        send.configure(state="normal")
        _ask(first_question)

    def _open_setup(_e=None) -> None:
        from claude_setup import open_setup
        open_setup(win, on_change=_key_changed)

    if not waiting["key"]:
        _ask(first_question)
    else:
        _show(ai_advisor.missing_key_text() + tr("\n\nהמפתח נשאר רק במחשב שלך.",
                                                 "\n\nThe key stays on your computer."), "dim")
        send.configure(state="disabled")
        key_btn = tk.Label(acts, text=i18n.bidi("🔑 " + tr("חבר מפתח", "Connect a key")), bg=ACCENT, fg="#ffffff",
                           font=(FONT, 10, "bold"), padx=14, pady=4, cursor="hand2")
        key_btn.pack(anchor=anchor)
        key_btn.bind("<Button-1>", _open_setup)
    return win
