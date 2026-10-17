"""The "Claude & Telegram setup" window (strip menu → 🔑, or "Connect a key"
in the Ask Claude window).

The user pastes the Anthropic API key and the Telegram bot token here
himself. Both go to Windows Credential Manager (secrets_store), encrypted
with his login — never to a file. The fields show dots; once a value is
saved its field is emptied, and so is the clipboard if it still holds it.

Everything that needs the network — checking the key, finding the chat,
the test message — runs on a worker thread; the Tk thread polls a queue.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import webbrowser
from typing import Callable

import i18n
from dark_menu import (_BUTTON_COLORS, ACCENT, BG, EDGE, FG, FG_FAINT, FG_SOFT, HOVER, WARN, _font,
                       _reads_rtl, _rtl_lines, confirm)
from i18n import ltr, tr

log = logging.getLogger(__name__)

CONSOLE_URL = "https://platform.claude.com/settings/workspaces"
BOTFATHER_URL = "https://t.me/BotFather"
GOOD = "#7ad89a"
WIDTH = 430                     # text width, in pixels

_current: dict[str, "SetupWindow | None"] = {"win": None}


def open_setup(parent: tk.Misc, on_change: Callable[[], None] | None = None) -> tk.Toplevel:
    """Open the window, or bring the open one forward."""
    old = _current["win"]
    if old is not None and old.alive():
        if on_change is not None:
            old.on_change.append(on_change)
        old.win.deiconify()
        old.win.lift()
        old.win.focus_force()
        return old.win
    w = SetupWindow(parent, [on_change] if on_change else [])
    _current["win"] = w
    return w.win


class SetupWindow:
    def __init__(self, parent: tk.Misc, on_change: list[Callable[[], None]]) -> None:
        self.on_change = on_change
        self.results: queue.Queue = queue.Queue()
        self.busy = False
        win = self.win = tk.Toplevel(parent)
        win.withdraw()
        win.title(tr("חיבור Claude וטלגרם", "Claude & Telegram setup"))
        win.configure(bg=BG)
        win.resizable(False, False)
        body = tk.Frame(win, bg=BG, padx=18, pady=14)
        body.pack(fill="both", expand=True)

        self._lines(body, "🔑 " + tr("חיבור Claude וטלגרם", "Claude & Telegram setup"), ACCENT, 12, True)
        self._lines(body, tr("המפתח והטוקן נשמרים מוצפנים ב-Windows, לא בקובץ, ונשארים רק במחשב הזה.",
                             "The key and the token are stored encrypted by Windows, not in a file, "
                             "and stay on this computer."), FG_FAINT, 8)

        # ---- Anthropic
        self._section(body, tr("מפתח Anthropic", "Anthropic API key"))
        self.key_status = self._lines(body, "")
        self.key_entry = self._secret_entry(body)
        row = self._row(body)
        self._button(row, tr("שמור ובדוק", "Save and check"), self._save_key, "main")
        self._button(row, tr("מחק מפתח", "Delete key"), self._delete_key, "plain")
        self.key_msg = self._lines(body, "")
        self.spend_line = self._lines(body, "", FG_SOFT, 8)
        self._lines(body, tr(
            "תקרה קשיחה: ב-Console צור workspace בשם PulseDeck, הגדר לו Spend limit של $5 לחודש, "
            "וצור את המפתח בתוכו. כך Anthropic עצמה עוצרת ב-$5, גם אם משהו כאן ישתבש.",
            "Hard limit: in the Console, create a workspace named PulseDeck, give it a $5 monthly "
            "spend limit, and create the key inside it. Then Anthropic itself stops at $5, "
            "even if something here goes wrong."), FG_FAINT, 8)
        row = self._row(body)
        self._button(row, tr("פתח את ה-Console", "Open the Console"),
                     lambda: webbrowser.open(CONSOLE_URL), "plain")

        # ---- Telegram
        self._section(body, tr("טלגרם: עדכונים על ההוצאות", "Telegram: spending updates"))
        self.tg_status = self._lines(body, "")
        self._lines(body, tr(
            f"1. בטלגרם, אצל {ltr('@BotFather')}: שלח {ltr('/newbot')} וקבל טוקן.\n"
            "2. הדבק את הטוקן כאן.\n"
            f"3. שלח לבוט החדש שלך {ltr('/start')}.\n"
            "4. לחץ \"חבר ושלח בדיקה\".",
            "1. In Telegram, at @BotFather: send /newbot and get a token.\n"
            "2. Paste the token here.\n"
            "3. Send /start to your new bot.\n"
            "4. Press \"Connect and test\"."), FG_SOFT, 8)
        self.tg_entry = self._secret_entry(body)
        row = self._row(body)
        self._button(row, tr("חבר ושלח בדיקה", "Connect and test"), self._connect, "main")
        self._button(row, tr("נתק", "Disconnect"), self._disconnect, "plain")
        self._button(row, tr(f"פתח את {ltr('@BotFather')}", "Open @BotFather"),
                     lambda: webbrowser.open(BOTFATHER_URL), "plain")
        self.tg_msg = self._lines(body, "")
        self._lines(body, tr(
            "מה נשלח: התראה כשעוברים 50%, 80% ו-100% מהתקרה, וסיכום ביום ראשון וב-1 בחודש. "
            "רק מספרים, אף פעם לא שאלות, תשובות או שמות תוכנות.",
            "What is sent: an alert past 50%, 80% and 100% of the cap, and a summary on Sunday and "
            "on the 1st. Numbers only, never questions, answers or program names."), FG_FAINT, 8)

        tk.Frame(body, bg=EDGE, height=1).pack(fill="x", pady=(12, 8))
        row = self._row(body)
        self._button(row, tr("סגור", "Close"), self.close, "plain")

        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda _e: self.close())
        self.refresh()
        self._place(parent)

    # ------------------------------------------------------------ building

    def _lines(self, parent: tk.Misc, text: str, fg: str = FG, size: int = 9, bold: bool = False) -> tk.Frame:
        frame = tk.Frame(parent, bg=BG)
        frame.pack(fill="x")
        self._fill(frame, text, fg, size, bold)
        return frame

    @staticmethod
    def _fill(frame: tk.Frame, text: str, fg: str = FG, size: int = 9, bold: bool = False) -> None:
        """One label per drawn line, each laid out right to left when it
        starts in Hebrew (see dark_menu._dialog)."""
        for w in frame.winfo_children():
            w.destroy()
        if not text:
            return
        font = _font(size, bold)
        for line in text.split("\n"):
            if _reads_rtl(line) and i18n.is_rtl():
                for part in _rtl_lines(line, font, WIDTH):
                    tk.Label(frame, text=part, bg=BG, fg=fg, font=font, anchor="e").pack(fill="x")
            else:
                tk.Label(frame, text=line, bg=BG, fg=fg, font=font, justify="left", anchor="w",
                         wraplength=WIDTH).pack(fill="x")

    def _section(self, parent: tk.Misc, title: str) -> None:
        tk.Frame(parent, bg=EDGE, height=1).pack(fill="x", pady=(12, 6))
        self._lines(parent, title, ACCENT, 10, True)

    @staticmethod
    def _row(parent: tk.Misc) -> tk.Frame:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(6, 0))
        return row

    def _button(self, row: tk.Frame, text: str, command: Callable[[], None], style: str) -> tk.Label:
        bg, fg = _BUTTON_COLORS[style]
        b = tk.Label(row, text=i18n.bidi(text), font=_font(9, True), padx=12, pady=3, cursor="hand2", bg=bg, fg=fg)
        b.pack(side=i18n.SIDE(), padx=(0, 6) if i18n.SIDE() == "left" else (6, 0))
        b.bind("<ButtonRelease-1>", lambda _e: None if self.busy else command())
        return b

    def _secret_entry(self, parent: tk.Misc) -> tk.Entry:
        row = self._row(parent)
        paste = tk.Label(row, text=i18n.bidi("📋 " + tr("הדבק", "Paste")), font=_font(9, True), padx=10, pady=3,
                         cursor="hand2", bg=HOVER, fg=FG_SOFT)
        paste.pack(side=i18n.SIDE_END(), padx=(6, 0) if i18n.SIDE_END() == "right" else (0, 6))
        entry = tk.Entry(row, show="•", bg=HOVER, fg=FG, insertbackground=FG, relief="flat",
                         font=_font(10), justify="left")
        entry.pack(side=i18n.SIDE(), fill="x", expand=True, ipady=4)
        paste.bind("<ButtonRelease-1>", lambda _e: self._paste(entry))
        # Ctrl+V by key position, so it pastes under a Hebrew keyboard layout too.
        entry.bind("<Control-Key>", lambda e: self._paste(entry) if e.keycode == 86 else None)
        return entry

    def _paste(self, entry: tk.Entry) -> str:
        try:
            text = self.win.clipboard_get()
        except tk.TclError:
            text = ""
        entry.delete(0, "end")
        entry.insert(0, text.strip())
        return "break"

    def _forget(self, entry: tk.Entry, value: str) -> None:
        """Empty the field, and the clipboard if it still holds `value`."""
        entry.delete(0, "end")
        try:
            if self.win.clipboard_get().strip() == value:
                self.win.clipboard_clear()
        except tk.TclError:
            pass

    def _place(self, parent: tk.Misc) -> None:
        win = self.win
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        win.deiconify()
        win.lift()
        win.focus_force()
        self.key_entry.focus_set()

    # ------------------------------------------------------------ state

    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def close(self) -> None:
        if _current["win"] is self:
            _current["win"] = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def _say(self, frame: tk.Frame, text: str, fg: str = FG_SOFT) -> None:
        if self.alive():
            self._fill(frame, text, fg)

    def refresh(self) -> None:
        import ai_advisor
        import claude_spend
        import secrets_store
        source = ai_advisor.key_source()
        if source == "store":
            hint = secrets_store.hint(secrets_store.load(secrets_store.ANTHROPIC_KEY))
            self._fill(self.key_status, tr(f"✓ מפתח שמור מוצפן ב-Windows {ltr(f'({hint})')}",
                                           f"✓ A key is stored, encrypted by Windows ({hint})"), GOOD)
        elif source == "env":
            self._fill(self.key_status, tr("המפתח מגיע עכשיו מקובץ .env או ממשתני הסביבה. "
                                           "מפתח שתשמור כאן יקבל עדיפות.",
                                           "The key now comes from .env or the environment. "
                                           "A key saved here takes priority."), FG_SOFT)
        else:
            self._fill(self.key_status, tr("עדיין אין מפתח.", "No key yet."), FG_SOFT)
        try:
            self._fill(self.spend_line, claude_spend.default_ledger().summary() + "\n"
                       + ai_advisor.default_budget().summary(), FG_SOFT, 8)
        except Exception:
            log.debug("Spending summary unavailable", exc_info=True)

        chat = _telegram_chat()
        token = secrets_store.load(secrets_store.TELEGRAM_TOKEN)
        if token and chat:
            self._fill(self.tg_status, tr(f"✓ מחובר: העדכונים נשלחים אל {ltr(chat[1])}",
                                          f"✓ Connected: updates go to {chat[1]}"), GOOD)
        elif token:
            self._fill(self.tg_status, tr(f"הטוקן שמור, אבל עוד לא נבחר צ'אט. שלח לבוט {ltr('/start')} "
                                          "ולחץ \"חבר ושלח בדיקה\".",
                                          "The token is saved, but no chat yet. Send /start to the bot "
                                          "and press \"Connect and test\"."), FG_SOFT)
        else:
            self._fill(self.tg_status, tr("לא מחובר.", "Not connected."), FG_SOFT)

    def _changed(self) -> None:
        for fn in list(self.on_change):
            try:
                fn()
            except Exception:
                log.exception("Setup change callback failed")

    def _run(self, work: Callable[[], object], done: Callable[[object], None], frame: tk.Frame) -> None:
        """`work` on a worker thread, then `done(result)` on the Tk thread.
        A user-facing error (TelegramError, SecretStoreError) is shown in
        `frame`; anything else is logged."""
        self.busy = True

        def _thread() -> None:
            try:
                self.results.put((True, work()))
            except Exception as e:      # noqa: BLE001 - reported on the Tk thread
                self.results.put((False, e))

        def _poll() -> None:
            if not self.alive():
                return
            try:
                ok, value = self.results.get_nowait()
            except queue.Empty:
                self.win.after(150, _poll)
                return
            self.busy = False
            if ok:
                done(value)
                return
            import secrets_store
            import telegram_notify
            if isinstance(value, (telegram_notify.TelegramError, secrets_store.SecretStoreError)):
                self._say(frame, str(value), WARN)
            else:
                log.error("Setup step failed: %s", type(value).__name__)
                self._say(frame, tr("משהו השתבש. הפרטים ב-history/monitor.log.",
                                    "Something went wrong. Details in history/monitor.log."), WARN)

        threading.Thread(target=_thread, name="claude-setup", daemon=True).start()
        self.win.after(150, _poll)

    # ------------------------------------------------------------ Anthropic

    def _save_key(self) -> None:
        import ai_advisor
        import secrets_store
        try:
            key = secrets_store.clean(self.key_entry.get())
        except secrets_store.SecretStoreError as e:
            self._say(self.key_msg, str(e), WARN)
            return
        self._say(self.key_msg, tr("בודק את המפתח מול Anthropic (בדיקה בלי עלות)…",
                                   "Checking the key with Anthropic (a free check)…"))

        def done(result: object) -> None:
            ok, why = result  # type: ignore[misc]
            if ok is False:
                self._say(self.key_msg, why, WARN)
                return
            try:
                secrets_store.save(secrets_store.ANTHROPIC_KEY, key)
            except secrets_store.SecretStoreError as e:
                self._say(self.key_msg, str(e), WARN)
                return
            self._forget(self.key_entry, key)
            self._say(self.key_msg, tr("✓ המפתח עובד ונשמר.", "✓ The key works and is saved.") if ok else why,
                      GOOD if ok else FG_SOFT)
            self.refresh()
            self._changed()

        self._run(lambda: ai_advisor.check_key(key), done, self.key_msg)

    def _delete_key(self) -> None:
        import secrets_store
        if not secrets_store.load(secrets_store.ANTHROPIC_KEY):
            self._say(self.key_msg, tr("אין כאן מפתח שמור.", "There is no stored key."))
            return

        def really(yes: bool) -> None:
            if not yes or not self.alive():
                return
            secrets_store.delete(secrets_store.ANTHROPIC_KEY)
            self._say(self.key_msg, tr("המפתח נמחק מ-PulseDeck.", "The key was deleted from PulseDeck."))
            self.refresh()
            self._changed()

        confirm(self.win, tr("למחוק את המפתח?", "Delete the key?"),
                tr("PulseDeck ישכח את המפתח. ב-Console הוא נשאר פעיל עד שתבטל אותו שם.",
                   "PulseDeck forgets the key. It stays active in the Console until you revoke it there."),
                tr("מחק", "Delete"), really, danger=True)

    # ------------------------------------------------------------ Telegram

    def _connect(self) -> None:
        import secrets_store
        import telegram_notify
        typed = self.tg_entry.get().strip()
        token = typed or secrets_store.load(secrets_store.TELEGRAM_TOKEN) or ""
        if not token:
            self._say(self.tg_msg, tr(f"הדבק קודם את הטוקן שקיבלת מ-{ltr('@BotFather')}.",
                                      "Paste the token from @BotFather first."), WARN)
            return
        if not telegram_notify.looks_like_token(token):
            self._say(self.tg_msg, tr("זה לא נראה כמו טוקן של בוט. הוא נראה כך: 123456789:AAH…",
                                      "That does not look like a bot token. It looks like 123456789:AAH…"), WARN)
            return
        self._say(self.tg_msg, tr("בודק מול טלגרם…", "Checking with Telegram…"))

        def work() -> tuple[str, tuple[int, str] | None]:
            return telegram_notify.bot_name(token), telegram_notify.find_chat(token)

        def done(result: object) -> None:
            name, chat = result  # type: ignore[misc]
            if typed:
                try:
                    secrets_store.save(secrets_store.TELEGRAM_TOKEN, token)
                except secrets_store.SecretStoreError as e:
                    self._say(self.tg_msg, str(e), WARN)
                    return
                self._forget(self.tg_entry, token)
            self.refresh()
            if chat is None:
                self._say(self.tg_msg, tr(f"הבוט {ltr(name)} תקין. עכשיו שלח לו {ltr('/start')} בטלגרם, "
                                          "וחזור ללחוץ \"חבר ושלח בדיקה\".",
                                          f"The bot {name} works. Now send it /start in Telegram, "
                                          "then press \"Connect and test\" again."), FG_SOFT)
                return
            chat_id, chat_name = chat
            confirm(self.win, tr("לחבר את הצ'אט הזה?", "Connect this chat?"),
                    tr(f"העדכונים יישלחו אל:\n{chat_name}\nזה החשבון שלך בטלגרם?",
                       f"Updates will go to:\n{chat_name}\nIs this your Telegram account?"),
                    tr("כן, חבר", "Yes, connect"),
                    lambda yes: yes and self._finish_connect(token, chat_id, chat_name))

        self._run(work, done, self.tg_msg)

    def _finish_connect(self, token: str, chat_id: int, chat_name: str) -> None:
        import spend_reports
        import telegram_notify
        if not self.alive():
            return
        if not _save_chat(chat_id, chat_name):
            self._say(self.tg_msg, tr("לא הצלחתי לשמור את הצ'אט. הפרטים ב-history/monitor.log.",
                                      "Could not save the chat. Details in history/monitor.log."), WARN)
            return
        spend_reports.default_reporter().mark_connected()
        self.refresh()
        self._say(self.tg_msg, tr("שולח הודעת בדיקה…", "Sending a test message…"))
        self._run(lambda: telegram_notify.send(token, chat_id, spend_reports.connected_text()),
                  lambda _r: self._say(self.tg_msg, tr("✓ נשלחה הודעת בדיקה. בדוק בטלגרם.",
                                                       "✓ A test message was sent. Check Telegram."), GOOD),
                  self.tg_msg)

    def _disconnect(self) -> None:
        import secrets_store

        def really(yes: bool) -> None:
            if not yes or not self.alive():
                return
            secrets_store.delete(secrets_store.TELEGRAM_TOKEN)
            _save_chat(None, None)
            self._say(self.tg_msg, tr("טלגרם נותק. הבוט עצמו נשאר אצלך בטלגרם.",
                                      "Telegram is disconnected. The bot itself stays in your Telegram."))
            self.refresh()

        confirm(self.win, tr("לנתק את טלגרם?", "Disconnect Telegram?"),
                tr("PulseDeck יפסיק לשלוח עדכונים וישכח את הטוקן.",
                   "PulseDeck stops sending updates and forgets the token."),
                tr("נתק", "Disconnect"), really)


def _telegram_chat() -> tuple[int, str] | None:
    import config
    try:
        tg = config.load_config().get("telegram") or {}
    except Exception:
        return None
    chat_id = tg.get("chat_id")
    return (chat_id, str(tg.get("chat_name") or chat_id)) if isinstance(chat_id, int) else None


def _save_chat(chat_id: int | None, chat_name: str | None) -> bool:
    import config
    try:
        cfg = config.load_config()
        cfg["telegram"] = {"chat_id": chat_id, "chat_name": chat_name}
        config.save_config(cfg)
        return True
    except Exception:
        log.exception("Could not save the Telegram chat")
        return False
