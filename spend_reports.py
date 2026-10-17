"""Telegram reports on what "Ask Claude" costs.

- An alert when this month's spending passes 50 %, 80 % and 100 % of the
  monthly cap (claude_spend), each once a month, right after the answer that
  crossed the line — or when a question is refused at the cap.
- A weekly summary on Sunday from 09:00 (the week Sunday–Saturday before it)
  and a monthly one on the 1st from 09:00 (the month before). One missed
  while the PC was off goes out at the next start; a period with no
  questions sends nothing.

Numbers only: never a question, an answer or a program name. Between reports
nothing runs — the thread sleeps until the next one is due (at most 6 hours,
so a sleeping laptop or a changed clock cannot make it miss one).

history/telegram_reports.json remembers what was sent:
{"alerts": {"2026-10": [50]}, "weekly": "2026-10-11", "monthly": "2026-09"}
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import claude_spend
from app_paths import HISTORY_DIR
from claude_spend import month_name, usd
from i18n import tr

log = logging.getLogger(__name__)

STATE_PATH = HISTORY_DIR / "telegram_reports.json"
THRESHOLDS = (50, 80, 100)
REPORT_HOUR = 9
STARTUP_DELAY_SEC = 90          # the first check waits until PulseDeck has settled
MAX_SLEEP_SEC = 6 * 3600


def telegram_settings() -> tuple[str, int] | None:
    """(bot token, chat id) when Telegram is connected, else None."""
    import config
    import secrets_store
    try:
        chat_id = (config.load_config().get("telegram") or {}).get("chat_id")
    except Exception:
        log.exception("Could not read the Telegram settings")
        return None
    if not isinstance(chat_id, int):
        return None
    token = secrets_store.load(secrets_store.TELEGRAM_TOKEN)
    return (token, chat_id) if token else None


def _send_default(text: str) -> bool:
    import telegram_notify
    found = telegram_settings()
    if found is None:
        return False
    try:
        telegram_notify.send(found[0], found[1], text)
        return True
    except telegram_notify.TelegramError as e:
        log.warning("Telegram report not sent: %s", e)
        return False


def last_sunday(today: date) -> date:
    return today - timedelta(days=(today.weekday() + 1) % 7)     # Monday is 0, Sunday 6


def _month_id(d: date) -> str:
    return d.isoformat()[:7]


def _previous_month(d: date) -> date:
    """The 1st of the month before `d`'s."""
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def _questions(n: int) -> str:
    return tr("שאלה אחת", "1 question") if n == 1 else tr(f"{n} שאלות", f"{n} questions")


def connected_text() -> str:
    """The test message sent right after connecting."""
    cap = usd(claude_spend.configured_cap())
    return tr("✅ החיבור הצליח: כאן יגיעו עדכונים על ההוצאות של Claude ב-PulseDeck.\n"
              f"התראה כשעוברים 50%, 80% ו-100% מהתקרה החודשית ({cap}), וסיכום ביום ראשון וב-1 בחודש.\n"
              "נשלחים רק מספרים.",
              "✅ Connected: PulseDeck's updates on Claude spending will arrive here.\n"
              f"An alert past 50%, 80% and 100% of the monthly cap ({cap}), and a summary on Sunday "
              "and on the 1st.\nNumbers only.")


def _footer() -> str:
    return tr("הערכה של PulseDeck. הסכום המדויק ב-Console של Anthropic.",
              "PulseDeck's estimate. The exact amount is in Anthropic's Console.")


class Reporter:
    def __init__(self, ledger: claude_spend.Ledger, *, send: Callable[[str], bool] = _send_default,
                 connected: Callable[[], bool] = lambda: telegram_settings() is not None,
                 state_path: Path | None = None, now: Callable[[], float] = time.time,
                 background: bool = True) -> None:
        self.ledger = ledger
        self.send = send
        self.connected = connected
        self.path = Path(state_path or STATE_PATH)
        self.now = now
        self.background = background        # alerts on their own thread (tests: inline)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ state

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not save %s", self.path)

    def _today(self) -> date:
        return date.fromtimestamp(self.now())

    def mark_connected(self) -> None:
        """Telegram was just connected: the first summaries are the next ones
        due, not the ones already past; alerts already passed stay quiet."""
        today = datetime.fromtimestamp(self.now())
        sunday = last_sunday(today.date())
        if today < datetime.combine(sunday, datetime.min.time()).replace(hour=REPORT_HOUR):
            sunday -= timedelta(days=7)
        first = today.date().replace(day=1)
        if today < datetime.combine(first, datetime.min.time()).replace(hour=REPORT_HOUR):
            first = _previous_month(first)
        pct = self._percent()
        with self._lock:
            data = self._load()
            data["weekly"] = sunday.isoformat()
            data["monthly"] = _month_id(_previous_month(first))
            data["alerts"] = {_month_id(today.date()): [t for t in THRESHOLDS if t < 100 and pct >= t]}
            self._save(data)

    # ------------------------------------------------------------ alerts

    def _percent(self) -> float:
        cap = self.ledger.cap()
        return 100.0 * self.ledger.month_usd() / cap if cap > 0 else 100.0

    def alert_text(self, event: str) -> tuple[int, str] | None:
        """(threshold, message) for the highest line passed and not yet
        reported this month, or None."""
        pct = 100.0 if event == "cap" else self._percent()
        month = _month_id(self._today())
        with self._lock:
            sent = set(self._load().get("alerts", {}).get(month, []))
        due = [t for t in THRESHOLDS if pct >= t and t not in sent]
        if not due:
            return None
        line = max(due)
        spent, cap = self.ledger.month_usd(), self.ledger.cap()
        if line == 100:
            today = self._today()
            nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
            return line, tr(
                "⛔ הוצאות Claude: הגעת לתקרה החודשית\n"
                f"החודש: {usd(spent)} מתוך {usd(cap)}.\n"
                f"השאלות ל-Claude ב-PulseDeck נעצרו עד 1 ב{month_name(nxt.month)}.",
                "⛔ Claude spending: the monthly cap is reached\n"
                f"This month: {usd(spent)} of {usd(cap)}.\n"
                f"PulseDeck's questions to Claude are paused until {month_name(nxt.month)} 1.")
        questions, _ = self.ledger.totals(self._today().replace(day=1), self._today())
        left = max(0.0, cap - spent)
        more = ""
        n = int(left / (spent / questions)) if questions and spent > 0 else 0
        if n >= 1:
            more = " (" + tr("בערך ", "about ") + _questions(n) + ")"
        return line, tr(
            f"⚠️ הוצאות Claude: עברת {line}% מהתקרה החודשית\n"
            f"החודש עד עכשיו: {usd(spent)} מתוך {usd(cap)}.\n"
            f"נשארו {usd(left)}{more}.",
            f"⚠️ Claude spending: past {line}% of the monthly cap\n"
            f"This month so far: {usd(spent)} of {usd(cap)}.\n"
            f"{usd(left)} left{more}.")

    def on_spend(self, event: str) -> None:
        """Ledger listener: runs on the ask-claude thread, sends off it."""
        if self.background:
            threading.Thread(target=self._alert, args=(event,), name="telegram-alert", daemon=True).start()
        else:
            self._alert(event)

    def _alert(self, event: str) -> None:
        try:
            if not self.connected():
                return
            found = self.alert_text(event)
            if found is None:
                return
            line, text = found
            if self.send(text):
                month = _month_id(self._today())
                with self._lock:
                    data = self._load()
                    alerts = data.setdefault("alerts", {})
                    alerts = {month: alerts.get(month, [])}     # older months are not needed
                    alerts[month] = sorted(set(alerts[month]) | {t for t in THRESHOLDS if t <= line})
                    data["alerts"] = alerts
                    self._save(data)
        except Exception:
            log.exception("Telegram spending alert failed")

    # ------------------------------------------------------------ summaries

    def due(self) -> list[tuple[str, str, str]]:
        """(kind, period key, message) of every summary due now and not sent.
        A period with no questions is due with an empty message: marked as
        done, never sent."""
        now = datetime.fromtimestamp(self.now())
        today = now.date()
        with self._lock:
            data = self._load()
        out = []
        sunday = last_sunday(today)
        if now < datetime.combine(sunday, datetime.min.time()).replace(hour=REPORT_HOUR):
            sunday -= timedelta(days=7)
        if data.get("weekly", "") < sunday.isoformat():
            first, last = sunday - timedelta(days=7), sunday - timedelta(days=1)
            questions, spent = self.ledger.totals(first, last)
            text = ""
            if questions:
                month_spent, cap = self.ledger.month_usd(), self.ledger.cap()
                span = f"{first.day}.{first.month}–{last.day}.{last.month}"
                text = tr(
                    "📊 סיכום שבועי: Claude ב-PulseDeck\n"
                    f"השבוע ({span}): {_questions(questions)}, {usd(spent)}.\n"
                    f"החודש עד עכשיו: {usd(month_spent)} מתוך {usd(cap)}, "
                    f"נשארו {usd(max(0.0, cap - month_spent))}.\n" + _footer(),
                    "📊 Weekly summary: Claude in PulseDeck\n"
                    f"This week ({span}): {_questions(questions)}, {usd(spent)}.\n"
                    f"This month so far: {usd(month_spent)} of {usd(cap)}, "
                    f"{usd(max(0.0, cap - month_spent))} left.\n" + _footer())
            out.append(("weekly", sunday.isoformat(), text))
        first = today.replace(day=1)
        if now < datetime.combine(first, datetime.min.time()).replace(hour=REPORT_HOUR):
            first = _previous_month(first)
        month = _previous_month(first)
        if data.get("monthly", "") < _month_id(month):
            last = first - timedelta(days=1)
            questions, spent = self.ledger.totals(month, last)
            text = ""
            if questions:
                text = tr(
                    f"📅 סיכום חודש {month_name(month.month)}: Claude ב-PulseDeck\n"
                    f"סך הכול {_questions(questions)}, {usd(spent)} מתוך {usd(self.ledger.cap())}.\n" + _footer(),
                    f"📅 {month_name(month.month)} summary: Claude in PulseDeck\n"
                    f"{_questions(questions)} in all, {usd(spent)} of {usd(self.ledger.cap())}.\n" + _footer())
            out.append(("monthly", _month_id(month), text))
        return out

    def run_due(self) -> None:
        reports = self.due()
        if not reports or not self.connected():
            return
        for kind, key, text in reports:
            if text and not self.send(text):
                continue                        # no internet: try again at the next wake-up
            with self._lock:
                data = self._load()
                data[kind] = key
                self._save(data)

    def seconds_until_next(self) -> float:
        now = datetime.fromtimestamp(self.now())
        sunday = datetime.combine(last_sunday(now.date()), datetime.min.time()).replace(hour=REPORT_HOUR)
        while sunday <= now:
            sunday += timedelta(days=7)
        first = datetime.combine(now.date().replace(day=1), datetime.min.time()).replace(hour=REPORT_HOUR)
        if first <= now:
            first = (first + timedelta(days=32)).replace(day=1)
        wait = (min(sunday, first) - now).total_seconds() + 5
        return max(60.0, min(wait, MAX_SLEEP_SEC))

    # ------------------------------------------------------------ thread

    def start(self) -> None:
        if self._thread is not None:
            return
        self.ledger.listeners.append(self.on_spend)
        self._thread = threading.Thread(target=self._loop, name="telegram-reports", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(STARTUP_DELAY_SEC):
            return
        while True:
            try:
                self.run_due()
            except Exception:
                log.exception("Telegram summary failed")
            if self._stop.wait(self.seconds_until_next()):
                return


_reporter: Reporter | None = None


def default_reporter() -> Reporter:
    global _reporter
    if _reporter is None:
        _reporter = Reporter(claude_spend.default_ledger())
    return _reporter
