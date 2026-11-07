"""Every few hours, *offer* a cleanup; never perform one on its own.

A Tk timer on the strip wakes up every `interval_hours` (default 3), finds
what "Close quiet Claude chats" and "Clean all" would list (the same rules,
quiet_sessions.find and load_actions.leftovers) off the Tk thread, and when
anything is there shows a small card in the corner of the screen:

    🧹 5 things could be closed · 1,420 MB
    [Close all]  [Choose from the list]  [Not now]

Nothing closes until the user presses a button. "Close all" proves each one
idle again first (quiet_sessions.verify, 3 s) and closes only what still is.
No Claude, no network, no cost: it is a local scan every few hours.
"""
from __future__ import annotations

import logging
from typing import Callable

from i18n import ANCHOR, JUSTIFY, SIDE, SIDE_END, tr

log = logging.getLogger(__name__)

MIB = 1024 * 1024
DEFAULT_HOURS = 3
FIRST_CHECK_SEC = 30 * 60          # not at start-up: let the machine settle first
LRM = "‎"                     # keeps "1,470 MB" one piece inside Hebrew text
LIST_LINES = 5                     # rows shown on the card itself
RESULT_LINGER_MS = 20_000          # the outcome stays up this long, then the card goes


# ---------------------------------------------------------------- settings

def settings() -> tuple[bool, float]:
    """(enabled, interval_hours) from config.json."""
    try:
        import config
        section = config.load_config().get("cleanup_offer", {})
        hours = float(section.get("interval_hours", DEFAULT_HOURS))
        return bool(section.get("enabled", True)), max(0.25, hours)
    except Exception:
        log.debug("cleanup offer settings unavailable", exc_info=True)
        return True, float(DEFAULT_HOURS)


def is_enabled() -> bool:
    return settings()[0]


def set_enabled(on: bool) -> None:
    import config
    cfg = config.load_config()
    cfg.setdefault("cleanup_offer", {})["enabled"] = bool(on)
    config.save_config(cfg)


# ---------------------------------------------------------------- what to offer

def _default_quiet():
    import quiet_sessions
    return quiet_sessions.find()


def _default_leftovers():
    import load_actions
    return load_actions.leftovers()


def find_offer(find_quiet: Callable = _default_quiet,
               find_leftovers: Callable = _default_leftovers) -> list:
    """Quiet Claude chats, their terminals, finished ghosts and idle hogs,
    each process once, heaviest first within its kind. Blocking: call it
    off the Tk thread. Either search failing leaves the other's results."""
    found: list = []
    for search in (find_quiet, find_leftovers):
        try:
            result = search()
        except Exception:
            log.exception("Cleanup offer: a search failed")
            continue
        if isinstance(result, tuple):          # quiet_sessions.find: (sessions, terminals)
            for part in result:
                found.extend(part)
        else:
            found.extend(result)
    seen: set[int] = set()
    unique = []
    for t in found:
        pids = {t.pid, *t.member_pids}
        if pids & seen:
            continue
        seen |= pids
        unique.append(t)
    return unique


def sections(targets: list) -> list[tuple[str, list]]:
    """The checklist's sections, in the order the strip's two buttons use."""
    by_kind = {k: [t for t in targets if t.kind == k] for k in ("claude", "terminal", "ghost", "idle")}
    other = [t for t in targets if t.kind not in by_kind]
    return [(tr("💬 שיחות Claude שקטות", "💬 Quiet Claude chats"), by_kind["claude"]),
            (tr("🖥 מסופים שנשארו", "🖥 Leftover terminals"), by_kind["terminal"]),
            (tr("👻 תהליכים נטושים שסיימו", "👻 Abandoned processes that finished"), by_kind["ghost"]),
            (tr("💤 תופסים זיכרון ולא פעילים", "💤 Idle and holding memory"), by_kind["idle"] + other)]


def label(t) -> str:
    """One card row: a conversation by its title, anything else by its name."""
    if t.kind in ("claude", "terminal"):
        title = t.detail.partition("  ·  ")[0].strip()
        name = title or t.name
        if t.kind == "terminal":
            name = tr(f"מסוף · {name}", f"terminal · {name}")
    else:
        name = t.name
    return f"{name}  ·  {_mb(t.rss_bytes)}"


def _mb(rss_bytes: int) -> str:
    """'1,470 MB' that stays together: in a Hebrew line, without the mark,
    the unit drifts to the far end, away from its number."""
    return f"{LRM}{rss_bytes // MIB:,} MB"


def headline(targets: list) -> str:
    mb = _mb(sum(t.rss_bytes for t in targets))
    n = len(targets)
    if n == 1:
        return tr(f"🧹 יש דבר אחד שאפשר לסגור · {mb}",
                  f"🧹 1 thing could be closed · {mb}")
    return tr(f"🧹 יש {n} דברים שאפשר לסגור · {mb}",
              f"🧹 {n} things could be closed · {mb}")


def outcome_text(result) -> str:
    """The card's last word after "Close all"."""
    if isinstance(result, BaseException):
        return tr(f"הניקוי נכשל: {result}", f"Cleanup failed: {result}")
    closed, skipped = result
    done = [t for t, o in closed if o in ("closed", "already_gone")]
    failed = [t for t, o in closed if o not in ("closed", "already_gone")]
    mb = _mb(sum(t.rss_bytes for t in done))
    lines = [tr(f"✓ נסגרו {len(done)} · שוחררו בערך {mb}",
                f"✓ Closed {len(done)} · about {mb} freed")]
    if skipped:
        lines.append(tr(f"⏸ {len(skipped)} התעוררו בינתיים ונשארו פתוחים",
                        f"⏸ {len(skipped)} woke up meanwhile and stay open"))
    if failed:
        lines.append(tr(f"✕ {len(failed)} לא נסגרו (מוגנים או בלי הרשאה)",
                        f"✕ {len(failed)} could not be closed (protected or no access)"))
    return "\n".join(lines)


def _default_verify(targets):
    import quiet_sessions
    return quiet_sessions.verify(targets)


def _default_close(targets):
    from idle_cleanup import close_targets
    return close_targets(targets)


# ---------------------------------------------------------------- the card

class OfferCard:
    """The corner card. It only *asks*; `close_all` runs on the user's click."""

    def __init__(self, root, targets: list, *, verify=_default_verify, closer=_default_close,
                 on_gone: Callable[[], object] = lambda: None,
                 area: tuple[int, int, int, int] | None = None) -> None:
        import tkinter as tk

        from ghost_panel import BAD, BG, BORDER, CLEAN, DIM, FG, PANEL, PANEL_HI

        self.root, self.targets = root, list(targets)
        self._verify, self._closer, self._on_gone = verify, closer, on_gone
        self.gone = False

        win = self.win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        win.configure(bg=BORDER)
        frame = tk.Frame(win, bg=BG, padx=14, pady=10)
        frame.pack(fill="both", expand=True, padx=1, pady=1)

        top = tk.Frame(frame, bg=BG)
        top.pack(fill="x")
        x = tk.Label(top, text="✕", bg=BG, fg=DIM, font=("Segoe UI", 10), cursor="hand2")
        x.pack(side=SIDE_END())
        x.bind("<Button-1>", lambda _e: self.dismiss())
        tk.Label(top, text=headline(self.targets), bg=BG, fg=CLEAN,
                 font=("Segoe UI", 11, "bold"), anchor=ANCHOR()).pack(side=SIDE(), fill="x")

        rows = tk.Frame(frame, bg=BG)
        rows.pack(fill="x", pady=(6, 4))
        for t in self.targets[:LIST_LINES]:
            tk.Label(rows, text="• " + label(t), bg=BG, fg=FG, font=("Segoe UI", 9),
                     anchor=ANCHOR(), justify=JUSTIFY(), wraplength=330).pack(fill="x")
        more = len(self.targets) - LIST_LINES
        if more > 0:
            tk.Label(rows, text=tr(f"ועוד {more}…", f"and {more} more…"), bg=BG, fg=DIM,
                     font=("Segoe UI", 9), anchor=ANCHOR()).pack(fill="x")

        self.status = tk.StringVar(value=tr(
            "שום דבר לא נסגר בלי לחיצה שלך. לפני הסגירה כל אחד נבדק שוב במשך 3 שניות, "
            "ומה שהתעורר נשאר פתוח.",
            "Nothing closes without your click. Each one is checked again first (3 seconds), "
            "and whatever woke up stays open."))
        tk.Label(frame, textvariable=self.status, bg=BG, fg=DIM, font=("Segoe UI", 8),
                 anchor=ANCHOR(), justify=JUSTIFY(), wraplength=340).pack(fill="x")

        btns = self.buttons = tk.Frame(frame, bg=BG)
        btns.pack(fill="x", pady=(8, 0))

        def button(text, command, bg, fg="white", bold=False):
            b = tk.Button(btns, text=text, command=command, bg=bg, fg=fg, relief="flat", bd=0,
                          font=("Segoe UI", 9, "bold" if bold else "normal"),
                          cursor="hand2", padx=10, pady=4, activebackground=bg, activeforeground=fg)
            b.pack(side=SIDE(), padx=(0, 6) if SIDE() == "left" else (6, 0))
            return b

        self.close_btn = button(tr("סגור הכל", "Close all"), self.close_all, BAD, bold=True)
        self.choose_btn = button(tr("בחר מהרשימה", "Choose from the list"), self.choose, PANEL_HI, FG)
        self.later_btn = button(tr("לא עכשיו", "Not now"), self.dismiss, PANEL, DIM)

        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        if area is None:
            area = _screen_area(root)
        left, top_y, right, bottom = area          # bottom-right, above the taskbar
        win.geometry(f"+{max(left, right - w - 16)}+{max(top_y, bottom - h - 16)}")
        win.deiconify()

    # -- the user's choices --------------------------------------------------

    def close_all(self) -> None:
        """The user pressed "Close all": verify, then close what is still idle."""
        from ghost_panel import _run_in_background

        for b in (self.close_btn, self.choose_btn, self.later_btn):
            b.config(state="disabled")
        self.status.set(tr("מוודא שכל אחד עדיין לא פעיל, 3 שניות…",
                           "Making sure each one is still idle (3 seconds)…"))
        targets = list(self.targets)

        def work():
            ok, skipped = self._verify(targets)
            return self._closer(ok), skipped

        _run_in_background(self.win, work, self._show_outcome)

    def _show_outcome(self, result) -> None:
        if self.gone:
            return
        self.status.set(outcome_text(result))
        self.close_btn.pack_forget()
        self.choose_btn.pack_forget()
        self.later_btn.config(state="normal", text=tr("סגור", "Close"))
        self.win.after(RESULT_LINGER_MS, self.dismiss)

    def choose(self) -> None:
        """Open the full checklist (ticks, folders) with the same items."""
        from ghost_panel import _open_checklist
        targets = list(self.targets)
        self.dismiss()
        _open_checklist(
            self.root,
            title=tr("🧹 אפשר לסגור — בחר מה", "🧹 Could be closed — choose"),
            intro=tr("מה שמצאה הבדיקה התקופתית. בטל סימון ממה שתרצה להשאיר; לפני הסגירה כל "
                     "אחד נבדק שוב, ומה שהתעורר בינתיים — לא ייסגר.",
                     "What the periodic check found. Untick anything you want to keep; each one "
                     "is checked again before closing, and whatever woke up meanwhile stays."),
            sections=sections(targets), notes=(),
            empty=tr("✓ אין כרגע מה לסגור", "✓ Nothing to close right now"),
            verify=self._verify, closer=self._closer, on_finished=lambda: None)

    def dismiss(self) -> None:
        if self.gone:
            return
        self.gone = True
        try:
            self.win.destroy()
        except Exception:
            pass
        self._on_gone()


def _screen_area(root) -> tuple[int, int, int, int]:
    try:
        from dock_strip import _work_area
        area = _work_area()
        if area:
            return area
    except Exception:
        log.debug("work area unavailable", exc_info=True)
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - 48


# ---------------------------------------------------------------- the timer

class OfferTimer:
    """Wakes every `interval_hours`, searches off the Tk thread, and shows a
    card when there is something. One card at a time; a switched-off offer
    keeps its timer (one config read every few hours) so switching it back
    on needs no restart."""

    def __init__(self, root, *, find=find_offer, card=OfferCard,
                 first_delay_sec: float = FIRST_CHECK_SEC) -> None:
        self.root, self._find, self._card = root, find, card
        self.current = None
        self._job = root.after(int(first_delay_sec * 1000), self.check)

    def _schedule(self) -> None:
        hours = settings()[1]
        self._job = self.root.after(int(hours * 3600 * 1000), self.check)

    def check(self) -> None:
        self._schedule()
        if not is_enabled() or (self.current is not None and not self.current.gone):
            return
        from ghost_panel import _run_in_background
        _run_in_background(self.root, self._find, self._found)

    def _found(self, result) -> None:
        if isinstance(result, BaseException) or not result:
            return
        if self.current is not None and not self.current.gone:
            return
        self.current = self._card(self.root, result)

    def stop(self) -> None:
        try:
            self.root.after_cancel(self._job)
        except Exception:
            pass


def start(root) -> OfferTimer:
    return OfferTimer(root)
