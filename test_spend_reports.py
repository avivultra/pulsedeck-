"""Tests for the Telegram spending reports and the Telegram client.
No network: urlopen and the sender are fakes."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import claude_spend
import spend_reports
import telegram_notify

TOKEN = "123456789:AAH" + "x" * 32


class _Clock:
    def __init__(self, *args):
        self.t = datetime(*args).timestamp()

    def __call__(self):
        return self.t

    def set(self, *args):
        self.t = datetime(*args).timestamp()


def _setup(*when, cap=5.0):
    clock = _Clock(*when)
    tmp = Path(tempfile.mkdtemp())
    ledger = claude_spend.Ledger(tmp / "spend.json", cap=lambda: cap, now=clock)
    sent: list[str] = []
    rep = spend_reports.Reporter(ledger, send=lambda text: sent.append(text) or True, connected=lambda: True,
                                 state_path=tmp / "reports.json", now=clock, background=False)
    ledger.listeners.append(rep.on_spend)
    return clock, ledger, rep, sent


def _spend(ledger, amount):
    ledger.settle(ledger.reserve(amount), amount)


class AlertTest(unittest.TestCase):
    def test_each_line_once_a_month(self):
        clock, ledger, rep, sent = _setup(2026, 10, 5, 12)
        _spend(ledger, 1.0)                              # 20 %
        self.assertEqual(sent, [])
        _spend(ledger, 1.6)                              # 52 %
        self.assertEqual(len(sent), 1)
        self.assertIn("50%", sent[0])
        self.assertIn("$2.60 מתוך $5", sent[0])
        _spend(ledger, 0.1)                              # still past 50 only
        self.assertEqual(len(sent), 1)
        _spend(ledger, 1.5)                              # 84 %
        self.assertIn("80%", sent[-1])
        self.assertEqual(len(sent), 2)
        clock.set(2026, 11, 2, 12)                       # a new month: quiet again
        _spend(ledger, 0.5)
        self.assertEqual(len(sent), 2)

    def test_a_jump_past_two_lines_sends_one_message(self):
        _clock, ledger, _rep, sent = _setup(2026, 10, 5, 12)
        _spend(ledger, 4.5)                              # 90 %
        self.assertEqual(len(sent), 1)
        self.assertIn("80%", sent[0])
        self.assertIn("נשארו $0.50", sent[0])

    def test_a_refused_question_sends_the_cap_alert_once(self):
        _clock, ledger, _rep, sent = _setup(2026, 10, 5, 12, cap=1.0)
        _spend(ledger, 0.9)
        sent.clear()
        for _ in range(2):
            with self.assertRaises(claude_spend.CapReached):
                ledger.reserve(0.2)
        self.assertEqual(len(sent), 1)
        self.assertIn("הגעת לתקרה", sent[0])
        self.assertIn("נובמבר", sent[0])

    def test_nothing_without_telegram_and_a_failed_send_is_retried(self):
        _clock, ledger, rep, sent = _setup(2026, 10, 5, 12)
        rep.connected = lambda: False
        _spend(ledger, 3.0)
        self.assertEqual(sent, [])
        rep.connected = lambda: True
        rep.send = lambda text: False                    # no internet
        _spend(ledger, 0.01)
        rep.send = lambda text: sent.append(text) or True
        _spend(ledger, 0.01)
        self.assertEqual(len(sent), 1)

    def test_numbers_only(self):
        _clock, ledger, _rep, sent = _setup(2026, 10, 5, 12)
        _spend(ledger, 4.5)
        text = sent[0]
        for word in ("chrome", "snapshot", "Question"):
            self.assertNotIn(word, text)


class SummaryTest(unittest.TestCase):
    def test_weekly_on_sunday_from_nine(self):
        clock, ledger, rep, sent = _setup(2026, 10, 14, 12)       # a Wednesday
        rep.mark_connected()
        _spend(ledger, 0.40)
        _spend(ledger, 0.20)
        clock.set(2026, 10, 18, 8, 59)                            # Sunday, before 09:00
        rep.run_due()
        self.assertEqual(sent, [])
        clock.set(2026, 10, 18, 9, 0)
        rep.run_due()
        self.assertEqual(len(sent), 1)
        self.assertIn("סיכום שבועי", sent[0])
        self.assertIn("11.10–17.10", sent[0])
        self.assertIn("2 שאלות, $0.60", sent[0])
        self.assertIn("נשארו $4.40", sent[0])
        rep.run_due()                                             # once
        self.assertEqual(len(sent), 1)

    def test_a_missed_week_goes_out_at_the_next_start(self):
        clock, ledger, rep, sent = _setup(2026, 10, 14, 12)
        rep.mark_connected()
        _spend(ledger, 0.10)
        clock.set(2026, 10, 20, 7, 30)                            # the PC was off on Sunday
        rep.run_due()
        self.assertEqual(len(sent), 1)

    def test_an_empty_week_sends_nothing(self):
        clock, _ledger, rep, sent = _setup(2026, 10, 14, 12)
        rep.mark_connected()
        clock.set(2026, 10, 18, 10)
        rep.run_due()
        self.assertEqual(sent, [])
        self.assertEqual(json.loads(rep.path.read_text(encoding="utf-8"))["weekly"], "2026-10-18")

    def test_monthly_on_the_first(self):
        clock, ledger, rep, sent = _setup(2026, 10, 20, 12)       # a Tuesday
        rep.mark_connected()
        clock.set(2026, 10, 28, 12)
        _spend(ledger, 1.25)
        clock.set(2026, 11, 1, 9, 30)                             # a Sunday: both summaries
        rep.run_due()
        self.assertEqual(len(sent), 2)
        monthly = sent[1]
        self.assertIn("אוקטובר", monthly)
        self.assertIn("שאלה אחת, $1.25 מתוך $5", monthly)
        self.assertIn("Console", monthly)

    def test_connecting_does_not_send_what_is_already_past(self):
        clock, ledger, rep, sent = _setup(2026, 10, 18, 12)       # Sunday, after 09:00
        rep.connected = lambda: False
        _spend(ledger, 2.6)                                       # 52 %, before connecting
        rep.connected = lambda: True
        rep.mark_connected()
        rep.run_due()
        _spend(ledger, 0.01)
        self.assertEqual(sent, [])

    def test_the_thread_sleeps_until_the_next_report(self):
        clock, _ledger, rep, _sent = _setup(2026, 10, 18, 10)     # Sunday 10:00
        self.assertEqual(rep.seconds_until_next(), spend_reports.MAX_SLEEP_SEC)
        clock.set(2026, 11, 1, 5)                                 # the 1st, and a Sunday
        self.assertAlmostEqual(rep.seconds_until_next(), 4 * 3600 + 5)
        clock.set(2026, 11, 1, 8, 59)
        self.assertEqual(rep.seconds_until_next(), 65)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _ok(result):
    return _Resp(json.dumps({"ok": True, "result": result}).encode())


class TelegramClientTest(unittest.TestCase):
    def test_a_bad_token_never_reaches_the_network(self):
        with mock.patch.object(telegram_notify, "urlopen") as urlopen:
            for bad in ("", "abc", "123:short", "123456789:AAH" + "x" * 32 + "/../x"):
                with self.subTest(bad=bad), self.assertRaises(telegram_notify.TelegramError):
                    telegram_notify.send(bad, 1, "hi")
        urlopen.assert_not_called()

    def test_send_is_plain_text(self):
        with mock.patch.object(telegram_notify, "urlopen", return_value=_ok({})) as urlopen:
            telegram_notify.send(TOKEN, 42, "<b>x</b>")
        req = urlopen.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(body["chat_id"], 42)
        self.assertEqual(body["text"], "<b>x</b>")
        self.assertNotIn("parse_mode", body)
        self.assertTrue(req.full_url.endswith("/sendMessage"))

    def test_the_latest_private_chat_is_found(self):
        updates = [{"message": {"chat": {"id": 1, "type": "private", "first_name": "Old"}}},
                   {"message": {"chat": {"id": -5, "type": "group", "title": "G"}}},
                   {"message": {"chat": {"id": 7, "type": "private", "first_name": "Aviv",
                                         "username": "aviv"}}},
                   {"edited_message": {}}]
        with mock.patch.object(telegram_notify, "urlopen", return_value=_ok(updates)):
            self.assertEqual(telegram_notify.find_chat(TOKEN), (7, "Aviv (@aviv)"))
        with mock.patch.object(telegram_notify, "urlopen", return_value=_ok([])):
            self.assertIsNone(telegram_notify.find_chat(TOKEN))

    def test_errors_never_carry_the_token(self):
        url = telegram_notify.API.format(token=TOKEN, method="getMe")
        cases = [HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b'{"description":"Unauthorized"}')),
                 HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"{}")),
                 URLError(f"failed {url}")]
        for err in cases:
            with self.subTest(err=type(err).__name__), \
                 mock.patch.object(telegram_notify, "urlopen", side_effect=err), \
                 self.assertLogs("telegram_notify", "WARNING") as logs:
                with self.assertRaises(telegram_notify.TelegramError) as ctx:
                    telegram_notify.bot_name(TOKEN)
            self.assertNotIn(TOKEN, str(ctx.exception))
            self.assertIsNone(ctx.exception.__cause__)
            self.assertNotIn(TOKEN, "\n".join(logs.output))

    def test_the_connected_message_names_the_cap(self):
        with mock.patch.object(claude_spend, "configured_cap", return_value=5.0):
            text = spend_reports.connected_text()
        self.assertIn("$5", text)
        self.assertIn("50%, 80% ו-100%", text)


if __name__ == "__main__":
    unittest.main()
