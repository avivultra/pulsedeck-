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

    def _show(text: str, tag: str = "dir") -> None:
        body.configure(state="normal")
        body.delete("1.0", "end")
        body.insert("1.0", text, tag)
        body.configure(state="disabled")

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
                results.put(("ok", ai_advisor.ask_claude(
                    snapshot, question, language=i18n.language())))
            except ai_advisor.AdvisorError as e:
                results.put(("err", str(e)))
            except Exception:
                log.exception("Ask Claude failed")
                results.put(("err", tr("משהו השתבש. הפרטים ב-history/monitor.log.",
                                       "Something went wrong. Details in history/monitor.log.")))

        threading.Thread(target=_work, name="ask-claude", daemon=True).start()
        win.after(150, _poll)

    def _poll() -> None:
        if not win.winfo_exists():
            return
        try:
            kind, text = results.get_nowait()
        except queue.Empty:
            win.after(150, _poll)
            return
        busy["on"] = False
        send.configure(state="normal")
        _show(text, "dir" if kind == "ok" else "dim")

    def _on_send(_e=None) -> None:
        q = entry.get().strip()
        entry.delete(0, "end")
        _ask(q or None)

    send.configure(command=_on_send)
    entry.bind("<Return>", _on_send)

    if ai_advisor.is_configured():
        _ask(first_question)
    else:
        _show(tr(
            "כדי לחבר את Claude:\n\n"
            "1. העתק את הקובץ .env.example לקובץ בשם .env (באותה תיקייה).\n"
            "2. הדבק את המפתח שלך אחרי ANTHROPIC_API_KEY=\n"
            "3. פתח שוב את החלון הזה.\n\n"
            "המפתח נשאר רק במחשב שלך, והקובץ .env לא עולה ל-GitHub.",
            "To connect Claude:\n\n"
            "1. Copy .env.example to a file named .env (same folder).\n"
            "2. Paste your key after ANTHROPIC_API_KEY=\n"
            "3. Open this window again.\n\n"
            "The key stays on your computer, and .env is never uploaded to GitHub."), "dim")
        send.configure(state="disabled")
    return win
