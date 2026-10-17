"""PulseDeck's own popup menu and message box, in the vault's dark palette.

Why not tk.Menu / tkinter.messagebox: on Windows both are the system's own
controls -- white, and Tk cannot recolour them. These are borderless
Toplevels built from Labels, so they match the strip and the vault window
and read right-to-left in Hebrew.

The menu closes on Esc, on a click anywhere outside it, and when one of its
items runs. A switch or a stepper row keeps it open, so the change shows and
can be repeated. Up/Down and Enter work too.
"""

from __future__ import annotations

import logging
import os
import tkinter as tk
from typing import Callable, Sequence

import i18n

log = logging.getLogger(__name__)

# The vault window's palette (vault_window.py), so all three look like one app.
BG = "#262626"
EDGE = "#3d3d3d"
HOVER = "#333333"
FG = "#dadada"
FG_SOFT = "#b3b3b3"
FG_FAINT = "#7a7a7a"
ACCENT = "#a882ff"
ACCENT_SOFT = "#2e2640"
WARN = "#fb464c"
SWITCH_OFF = "#4a4a4a"

FAMILY = "Segoe UI" if os.name == "nt" else "DejaVu Sans"
MIN_WIDTH = 270


def place(x: int, y: int, w: int, h: int, area: tuple[int, int, int, int],
          rtl: bool) -> tuple[int, int]:
    """Top-left corner for a w x h popup opened at the pointer (x, y), kept
    inside `area` (left, top, right, bottom). Hebrew opens to the left of the
    pointer and English to the right, each flipping when there is no room;
    near the bottom edge (the strip sits on the taskbar) it opens upwards."""
    left, top, right, bottom = area
    px = x - w if rtl else x
    if px < left:
        px = x
    elif px + w > right:
        px = x - w
    py = y if y + h <= bottom else y - h
    return max(left, min(px, right - w)), max(top, min(py, bottom - h))


def work_area_at(widget: tk.Misc, x: int, y: int) -> tuple[int, int, int, int]:
    """The usable part (minus the taskbar) of the monitor showing (x, y)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

            user32 = ctypes.WinDLL("user32")          # own copy: argtypes stay local
            user32.MonitorFromPoint.restype = wintypes.HMONITOR
            user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
            user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
            monitor = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)   # nearest
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(info)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.rcWork
                return r.left, r.top, r.right, r.bottom
        except Exception:
            log.debug("Monitor work area unavailable", exc_info=True)
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()


# Every icon the menus use. The first time Tk draws a symbol it searches the
# installed fonts for it (~20 ms each); warm_up() pays that once, while the
# user is not waiting, instead of on the first right-click (~0.3-0.7 s).
ICONS = "✳📊🔔🖧🧹👻📁📂📌🌍⟲🔑🚀🖥🚪⏸✕🔇"


def warm_up(widget: tk.Misc) -> None:
    try:
        tk.Label(widget, text=ICONS + "Aa", font=_font(10)).destroy()
    except tk.TclError:
        pass


def _reads_rtl(text: str) -> bool:
    """True when the first letter is Hebrew; text without letters follows the UI."""
    for ch in text:
        if "֐" <= ch <= "׿":
            return True
        if ch.isalpha():
            return False
    return i18n.is_rtl()


def _font(size: int, bold: bool = False) -> tuple:
    return (FAMILY, size, "bold") if bold else (FAMILY, size)


class _Row:
    """One row the pointer and the keyboard can land on."""

    def __init__(self, frame: tk.Frame, widgets: list[tk.Widget], activate: Callable[[], None],
                 keys: dict[str, Callable[[], None]] | None = None) -> None:
        self.frame = frame
        self.widgets = widgets              # recoloured on hover (frame included)
        self.activate = activate
        self.keys = keys or {}


class DarkMenu:
    def __init__(self, parent: tk.Misc, min_width: int = MIN_WIDTH) -> None:
        self.parent = parent
        self.min_width = min_width
        self.rtl = i18n.is_rtl()
        self.win = tk.Toplevel(parent)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.configure(bg=EDGE)                       # the 1 px frame
        self.body = tk.Frame(self.win, bg=BG, padx=5, pady=5)
        self.body.pack(fill="both", expand=True, padx=1, pady=1)
        self.rows: list[_Row] = []
        self.active: int | None = None
        self.closed = False

    # ------------------------------------------------------------ building
    def add_header(self, title: str, detail: str = "") -> None:
        f = tk.Frame(self.body, bg=BG, padx=8, pady=5)
        tk.Label(f, text=title, bg=BG, fg=FG, font=_font(10, True)).pack(side=i18n.SIDE())
        if detail:
            tk.Label(f, text=detail, bg=BG, fg=FG_FAINT, font=_font(8)).pack(side=i18n.SIDE_END())
        f.pack(fill="x")
        self.add_separator()

    def add_group(self, label: str) -> None:
        tk.Label(self.body, text=label, bg=BG, fg=FG_FAINT, font=_font(8),
                 anchor=i18n.ANCHOR(), padx=8).pack(fill="x", pady=(5, 1))

    def add_separator(self) -> None:
        tk.Frame(self.body, bg=EDGE, height=1).pack(fill="x", padx=4, pady=4)

    def _row(self, label: str, icon: str, fg: str = FG, icon_fg: str = ACCENT) -> tuple[tk.Frame, list[tk.Widget]]:
        f = tk.Frame(self.body, bg=BG, padx=6, pady=4, cursor="hand2")
        ic = tk.Label(f, text=icon, bg=BG, fg=icon_fg, font=_font(10), width=2)
        tx = tk.Label(f, text=label, bg=BG, fg=fg, font=_font(10), anchor=i18n.ANCHOR())
        ic.pack(side=i18n.SIDE())
        tx.pack(side=i18n.SIDE(), padx=(4, 4))
        f.pack(fill="x")
        return f, [f, ic, tx]

    def _register(self, frame: tk.Frame, widgets: list[tk.Widget], activate: Callable[[], None],
                  clickable: Sequence[tk.Widget], keys: dict | None = None) -> _Row:
        row = _Row(frame, widgets, activate, keys)
        index = len(self.rows)
        self.rows.append(row)
        for w in widgets:
            w.bind("<Enter>", lambda _e, i=index: self._highlight(i), add="+")
        for w in clickable:
            w.bind("<ButtonRelease-1>", lambda _e: row.activate())
        return row

    def add_command(self, label: str, command: Callable[[], object], icon: str = "",
                    danger: bool = False) -> None:
        f, widgets = self._row(label, icon, fg=WARN if danger else FG,
                               icon_fg=WARN if danger else ACCENT)
        self._register(f, widgets, lambda: self._run(command), widgets)

    def add_switch(self, label: str, value: bool, command: Callable[[], object],
                   icon: str = "") -> None:
        """A toggle that stays open: `command` flips the real setting."""
        f, widgets = self._row(label, icon)
        sw = tk.Canvas(f, width=30, height=16, bg=BG, highlightthickness=0, cursor="hand2")
        sw.pack(side=i18n.SIDE_END(), padx=(2, 2))
        state = {"on": bool(value)}

        def draw() -> None:
            sw.delete("all")
            on = state["on"]
            track = ACCENT if on else SWITCH_OFF
            sw.create_oval(1, 1, 15, 15, fill=track, outline=track)
            sw.create_oval(15, 1, 29, 15, fill=track, outline=track)
            sw.create_rectangle(8, 1, 22, 15, fill=track, outline=track)
            # "On" sits at the reading end: right in English, left in Hebrew.
            knob_right = on != self.rtl
            x0 = 16 if knob_right else 3
            sw.create_oval(x0, 3, x0 + 11, 14, fill="#ffffff" if on else FG_SOFT, outline="")

        def toggle() -> None:
            state["on"] = not state["on"]
            draw()
            self._call(command)

        draw()
        self._register(f, widgets + [sw], toggle, widgets + [sw])

    def add_stepper(self, label: str, on_minus: Callable[[], object], on_plus: Callable[[], object],
                    value_text: Callable[[], str], icon: str = "") -> None:
        """[−] value [+] that stays open; -/+ keys work on the highlighted row."""
        f, widgets = self._row(label, icon)
        box = tk.Frame(f, bg=BG)
        box.pack(side=i18n.SIDE_END())
        value = tk.Label(box, text=value_text(), bg=BG, fg=FG_SOFT, font=_font(9), width=5)

        def step(fn: Callable[[], object]) -> None:
            self._call(fn)
            value.configure(text=value_text())

        buttons = []
        for text, fn in (("−", on_minus), (None, None), ("+", on_plus)):
            if text is None:
                value.pack(side="left")
                continue
            b = tk.Label(box, text=text, bg=BG, fg=FG, font=_font(10, True), width=2,
                         cursor="hand2", highlightthickness=1, highlightbackground=EDGE)
            b.pack(side="left")
            b.bind("<ButtonRelease-1>", lambda _e, fn=fn: step(fn))
            b.bind("<Enter>", lambda _e, b=b: b.configure(fg=ACCENT), add="+")
            b.bind("<Leave>", lambda _e, b=b: b.configure(fg=FG))
            buttons.append(b)
        keys = {"minus": lambda: step(on_minus), "plus": lambda: step(on_plus),
                "equal": lambda: step(on_plus), "KP_Subtract": lambda: step(on_minus),
                "KP_Add": lambda: step(on_plus)}
        self._register(f, widgets + [box, value] + buttons, lambda: None, [], keys)

    def add_choice(self, label: str, options: Sequence[tuple[str, str]], current: str,
                   command: Callable[[str], object], icon: str = "") -> None:
        """Segmented buttons; picking one runs `command(code)` and closes the menu."""
        f, widgets = self._row(label, icon)
        box = tk.Frame(f, bg=EDGE, padx=1, pady=1)
        box.pack(side=i18n.SIDE_END())
        segs = []
        for code, text in options:
            on = code == current
            s = tk.Label(box, text=text, bg=ACCENT_SOFT if on else BG, fg="#e6e0ff" if on else FG_SOFT,
                         font=_font(8), padx=6, pady=1, cursor="hand2")
            s.pack(side=i18n.SIDE())
            s.bind("<ButtonRelease-1>", lambda _e, c=code: self._run(lambda: command(c)))
            segs.append(s)
        self._register(f, widgets + [box] + segs, lambda: None, [])

    # ------------------------------------------------------------ running
    def popup(self, x: int, y: int) -> None:
        self.win.update_idletasks()
        w = max(self.min_width, self.win.winfo_reqwidth())
        h = self.win.winfo_reqheight()
        px, py = place(x, y, w, h, work_area_at(self.win, x, y), self.rtl)
        self.win.geometry(f"{w}x{h}+{px}+{py}")
        self.win.deiconify()
        self.win.attributes("-topmost", True)
        self.win.lift()
        self.win.focus_force()
        self.win.bind("<FocusOut>", lambda _e: self.win.after(60, self._close_if_unfocused))
        self.bind_keys()

    def bind_keys(self) -> None:
        self.win.bind("<Escape>", lambda _e: self.close())
        self.win.bind("<Down>", lambda _e: self._move(+1))
        self.win.bind("<Up>", lambda _e: self._move(-1))
        for key in ("<Return>", "<KP_Enter>", "<space>"):
            self.win.bind(key, lambda _e: self._activate_highlighted())
        self.win.bind("<Key>", self._row_key, add="+")
        self.win.bind("<Leave>", lambda _e: self._pointer_left())

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def _close_if_unfocused(self) -> None:
        if self.closed:
            return
        try:
            focus = self.win.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if focus is None or not str(focus).startswith(str(self.win)):
            self.close()

    def _call(self, fn: Callable[[], object]) -> None:
        try:
            fn()
        except Exception:
            log.exception("Menu action failed")

    def _run(self, command: Callable[[], object]) -> None:
        """Close first, then run: a window it opens must not open under the menu."""
        self.close()
        self.parent.after_idle(lambda: self._call(command))

    def _highlight(self, index: int | None) -> None:
        if self.closed or index == self.active:
            return
        for i in (self.active, index):
            if i is None:
                continue
            bg = HOVER if i == index else BG
            for w in self.rows[i].widgets:
                try:
                    w.configure(bg=bg)
                except tk.TclError:
                    pass
        self.active = index

    def _move(self, delta: int) -> None:
        if not self.rows:
            return
        if self.active is None:
            self._highlight(0 if delta > 0 else len(self.rows) - 1)
        else:
            self._highlight((self.active + delta) % len(self.rows))

    def _activate_highlighted(self) -> None:
        if self.active is not None:
            self.rows[self.active].activate()

    def _row_key(self, event: tk.Event) -> None:
        if self.active is not None:
            fn = self.rows[self.active].keys.get(event.keysym)
            if fn is not None:
                fn()

    def _pointer_left(self) -> None:
        if self.closed:
            return
        x, y = self.win.winfo_pointerxy()
        inside = self.win.winfo_containing(x, y)
        if inside is None or not str(inside).startswith(str(self.win)):
            self._highlight(None)


TEXT_WIDTH = 340


def _rtl_lines(text: str, font: tuple, width: int) -> list[str]:
    """A Hebrew line broken into lines that fit `width`, each embedded
    right-to-left. Tk would wrap it itself, but Windows lays every Tk line
    out left to right: a Latin word or a number then lands on the wrong side,
    and an embedding (i18n.bidi) holds for one drawn line only."""
    from tkinter import font as tkfont
    measure = tkfont.Font(font=font).measure
    out, cur = [], ""
    for word in text.split(" "):
        test = f"{cur} {word}" if cur else word
        if cur and measure(test) > width:
            out.append(cur)
            cur = word
        else:
            cur = test
    out.append(cur)
    return [i18n.bidi(line) for line in out]


_BUTTON_COLORS = {"main": (ACCENT_SOFT, "#e6e0ff"), "danger": ("#4a1f22", "#ffb3b6"), "plain": (HOVER, FG_SOFT)}


def _dialog(parent: tk.Misc, title: str, text: str, warning: bool, buttons: Sequence[tuple[str, bool, str]],
            on_close: Callable[[bool], None] | None, enter: bool = True) -> tk.Toplevel:
    """The shared dark box: title, text, then buttons (label, answer, one of
    _BUTTON_COLORS). Esc or a click elsewhere answers False; Enter answers
    True when `enter`."""
    win = tk.Toplevel(parent)
    win.withdraw()
    win.overrideredirect(True)
    win.configure(bg=EDGE)
    body = tk.Frame(win, bg=BG, padx=16, pady=12)
    body.pack(fill="both", expand=True, padx=1, pady=1)
    head_rtl = _reads_rtl(title)
    tk.Label(body, text=i18n.bidi(title) if head_rtl else title, bg=BG, fg=WARN if warning else ACCENT,
             font=_font(10, True), anchor="e" if head_rtl else "w").pack(fill="x")
    # One label per line: Windows lays out a whole label in the direction of
    # its first letter, so an English line under a Hebrew one would get its
    # full stop on the wrong side (".starts").
    lines = tk.Frame(body, bg=BG)
    lines.pack(fill="x", pady=(6, 10))
    for line in text.split("\n"):
        if _reads_rtl(line) and i18n.is_rtl():
            for part in _rtl_lines(line, _font(10), TEXT_WIDTH):
                tk.Label(lines, text=part, bg=BG, fg=FG, font=_font(10), anchor="e").pack(fill="x")
        else:
            tk.Label(lines, text=line, bg=BG, fg=FG, font=_font(10), justify="left",
                     anchor="w", wraplength=TEXT_WIDTH).pack(fill="x")
    closed = {"v": False}

    def close(answer: bool = False) -> None:
        if closed["v"]:
            return
        closed["v"] = True
        try:
            win.destroy()
        except tk.TclError:
            pass
        if on_close is not None:
            try:
                on_close(answer)
            except Exception:
                log.exception("Dialog callback failed")

    row = tk.Frame(body, bg=BG)
    row.pack(fill="x")
    for label, answer, style in buttons:
        bg, fg = _BUTTON_COLORS[style]
        b = tk.Label(row, text=label, font=_font(9, True), padx=14, pady=3, cursor="hand2", bg=bg, fg=fg)
        b.pack(side=i18n.SIDE_END(), padx=(6, 0) if i18n.SIDE_END() == "right" else (0, 6))
        b.bind("<ButtonRelease-1>", lambda _e, a=answer: close(a))

    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    try:
        pw, ph = parent.winfo_width(), parent.winfo_height()
        cx = parent.winfo_rootx() + pw // 2
        top = parent.winfo_rooty()
    except tk.TclError:
        pw, ph = 0, 0
        cx, top = parent.winfo_screenwidth() // 2, parent.winfo_screenheight() // 2
    area = work_area_at(win, cx, top)
    x = max(area[0], min(cx - w // 2, area[2] - w))
    if ph >= 2 * h:                                  # a window: centred on it
        y = top + (ph - h) // 3
    else:                                            # the strip: just above it
        y = top - h - 10 if top - h - 10 >= area[1] else top + 10
    win.geometry(f"{w}x{h}+{x}+{max(area[1], min(y, area[3] - h))}")
    win.deiconify()
    win.attributes("-topmost", True)
    win.lift()
    win.focus_force()
    win.bind("<Escape>", lambda _e: close(False))
    if enter:
        for key in ("<Return>", "<KP_Enter>"):
            win.bind(key, lambda _e: close(True))

    def maybe_close() -> None:
        try:
            focus = win.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if focus is None:
            close(False)

    win.bind("<FocusOut>", lambda _e: win.after(60, maybe_close) if not closed["v"] else None)
    return win


def notice(parent: tk.Misc, title: str, text: str, warning: bool = False) -> tk.Toplevel:
    """A small dark message box near `parent`, with an OK button. Closes on
    OK, Enter, Esc or a click elsewhere."""
    return _dialog(parent, title, text, warning, [(i18n.tr("הבנתי", "OK"), True, "main")], None)


def confirm(parent: tk.Misc, title: str, text: str, ok: str, on_answer: Callable[[bool], None],
            danger: bool = False) -> tk.Toplevel:
    """Ask before acting: `on_answer(True)` only after a click on `ok`.
    Cancel, Esc and a click elsewhere answer False; Enter confirms only when
    the action is not `danger`. Returns at once - the answer comes later."""
    return _dialog(parent, title, text, danger,
                   [(ok, True, "danger" if danger else "main"), (i18n.tr("ביטול", "Cancel"), False, "plain")],
                   on_answer, enter=not danger)
