"""Tests for the monthly dollar cap on Ask Claude (no network: the client is faked)."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import ai_advisor
import claude_spend
import secrets_store


def _ledger(cap=5.0, now=None):
    return claude_spend.Ledger(Path(tempfile.mkdtemp()) / "spend.json", cap=lambda: cap,
                               now=now or (lambda: datetime(2026, 10, 17, 12).timestamp()))


def _budget():
    return ai_advisor.Budget(Path(tempfile.mkdtemp()) / "usage.json")


def _usage(i=1000, o=500, **kw):
    return NS(input_tokens=i, output_tokens=o, **kw)


class PriceTest(unittest.TestCase):
    def test_list_prices_dated_ids_and_unknown_models(self):
        self.assertEqual(claude_spend.price("claude-opus-5-5"), (4.0, 20.0))
        self.assertEqual(claude_spend.price("claude-opus-5-5-20261001"), (4.0, 20.0))
        self.assertEqual(claude_spend.price("claude-opus-5"), (5.0, 25.0))
        self.assertEqual(claude_spend.price("claude-haiku-5-5"), (0.10, 0.50))
        # A new model, or a variant of a known one, is never priced low.
        for unknown in ("claude-opus-5-7", "claude-opus-5-5-fast", "", None):
            self.assertEqual(claude_spend.price(unknown), claude_spend.DEAREST)

    def test_cost_of_one_answer(self):
        # Opus 5.5: 1,000 in x $4/M + 500 out x $20/M = $0.004 + $0.010
        self.assertAlmostEqual(claude_spend.cost_usd("claude-opus-5-5", _usage()), 0.014)
        cached = _usage(0, 0, cache_creation_input_tokens=1000, cache_read_input_tokens=1000)
        self.assertAlmostEqual(claude_spend.cost_usd("claude-opus-5-5", cached), (2000 + 100) * 4 / 1e6)

    def test_the_worst_case_is_above_any_real_answer(self):
        prompt = "שלום " * 2000              # Hebrew: two bytes a letter
        worst = claude_spend.worst_case_usd(prompt, 2000)
        tokens_in = len(prompt)              # far more than the real count
        self.assertGreater(worst, claude_spend.cost_usd("claude-fable-5-1", _usage(tokens_in, 2000)))

    def test_money_reads_naturally(self):
        self.assertEqual(claude_spend.usd(5.0), "$5")
        self.assertEqual(claude_spend.usd(4.851), "$4.85")
        self.assertEqual(claude_spend.usd(0.03), "$0.03")
        self.assertEqual(claude_spend.usd(0), "$0.00")


class LedgerTest(unittest.TestCase):
    def test_reserve_then_settle_records_the_real_cost(self):
        led = _ledger()
        hold = led.reserve(0.20)
        self.assertAlmostEqual(led.month_usd(), 0.20)        # the worst case, while it runs
        led.settle(hold, 0.03)
        self.assertAlmostEqual(led.month_usd(), 0.03)
        self.assertEqual(led.totals(date(2026, 10, 1), date(2026, 10, 31)), (1, 0.03))
        led.settle(hold, 0.03)                               # a second settle changes nothing
        self.assertAlmostEqual(led.month_usd(), 0.03)

    def test_release_gives_it_back_and_keep_does_not(self):
        led = _ledger()
        led.release(led.reserve(0.20))
        self.assertEqual(led.totals(date(2026, 10, 1), date(2026, 10, 31)), (0, 0.0))
        led.keep(led.reserve(0.20))
        self.assertAlmostEqual(led.month_usd(), 0.20)

    def test_refuses_what_could_pass_the_cap(self):
        led = _ledger(cap=1.0)
        events = []
        led.listeners.append(events.append)
        led.settle(led.reserve(0.85), 0.85)
        with self.assertRaises(claude_spend.CapReached) as ctx:
            led.reserve(0.20)
        self.assertIn("$0.85", str(ctx.exception))
        self.assertIn("נובמבר", str(ctx.exception))          # when it resets
        self.assertEqual(events, ["spent", "cap"])
        led.reserve(0.10)                                    # what still fits is allowed

    def test_a_new_month_starts_from_zero_and_old_days_are_pruned(self):
        t = {"now": datetime(2026, 10, 31, 23).timestamp()}
        led = _ledger(cap=1.0, now=lambda: t["now"])
        led.settle(led.reserve(0.9), 0.9)
        t["now"] = datetime(2026, 11, 1, 0, 30).timestamp()
        self.assertEqual(led.month_usd(), 0.0)
        led.reserve(0.5)
        self.assertAlmostEqual(led.month_usd("2026-10"), 0.9)
        t["now"] = datetime(2027, 3, 1).timestamp()
        led.reserve(0.1)
        self.assertEqual(led.month_usd("2026-10"), 0.0)

    def test_a_hold_settled_after_midnight_lands_on_its_own_day(self):
        t = {"now": datetime(2026, 10, 31, 23, 59, 50).timestamp()}
        led = _ledger(now=lambda: t["now"])
        hold = led.reserve(0.2)
        t["now"] = datetime(2026, 11, 1, 0, 0, 20).timestamp()
        led.settle(hold, 0.03)
        self.assertAlmostEqual(led.month_usd("2026-10"), 0.03)
        self.assertEqual(led.month_usd("2026-11"), 0.0)

    def test_a_broken_file_starts_empty(self):
        led = _ledger()
        led.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(led.month_usd(), 0.0)
        led.reserve(0.1)
        self.assertAlmostEqual(led.month_usd(), 0.1)

    def test_the_cap_comes_from_config(self):
        with mock.patch("config.load_config", return_value={"claude": {"monthly_usd": 3}}):
            self.assertEqual(claude_spend.configured_cap(), 3.0)
        with mock.patch("config.load_config", return_value={"claude": {"monthly_usd": "lots"}}):
            self.assertEqual(claude_spend.configured_cap(), 5.0)
        with mock.patch("config.load_config", return_value={}):
            self.assertEqual(claude_spend.configured_cap(), 5.0)


if __name__ == "__main__":
    unittest.main()
