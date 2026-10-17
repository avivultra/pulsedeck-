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


def _http():
    """The HTTP library the installed anthropic SDK is built on, whose
    Request and Response its errors take: httpx2 from SDK 1.15, httpx
    before. httpx may be absent where the SDK no longer pulls it in."""
    try:
        import httpx2
        return httpx2
    except ImportError:
        import httpx
        return httpx


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


class AskClaudeSpendTest(unittest.TestCase):
    """ask_claude and the ledger together."""

    def setUp(self):
        self.led = _ledger()
        p = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"})
        p.start()
        self.addCleanup(p.stop)

    def _ask(self, create, **kw):
        client = NS(beta=NS(messages=NS(create=create)))
        return ai_advisor.ask_claude({}, client=client, budget=_budget(), spend=self.led, **kw)

    def test_an_answer_is_priced_by_the_model_that_gave_it(self):
        resp = NS(stop_reason="end_turn", content=[NS(type="text", text="ok")],
                  model="claude-sonnet-5-5", usage=_usage(1000, 500))
        self._ask(mock.Mock(return_value=resp))
        self.assertAlmostEqual(self.led.month_usd(), (1000 * 2 + 500 * 10) / 1e6)

    def test_at_the_cap_nothing_is_sent(self):
        self.led.settle(self.led.reserve(4.95), 4.95)
        create = mock.Mock()
        with self.assertRaises(ai_advisor.AdvisorError) as ctx:
            self._ask(create)
        create.assert_not_called()
        self.assertIn("לתקרה החודשית", str(ctx.exception))

    def test_the_daily_cap_gives_the_dollars_back(self):
        budget = _budget()
        with mock.patch.dict(os.environ, {"PULSEDECK_CLAUDE_DAILY_REQUESTS": "0"}):
            with self.assertRaises(ai_advisor.AdvisorError):
                ai_advisor.ask_claude({}, client=NS(), budget=budget, spend=self.led)
        self.assertEqual(self.led.month_usd(), 0.0)

    def test_an_error_answer_costs_nothing_a_timeout_keeps_the_worst_case(self):
        import anthropic
        http = _http()
        req = http.Request("POST", "https://api.anthropic.com/v1/messages")
        rejected = anthropic.AuthenticationError("no", response=http.Response(401, request=req), body=None)
        with self.assertRaises(ai_advisor.AdvisorError) as ctx:
            self._ask(mock.Mock(side_effect=rejected))
        self.assertIn("חיבור Claude וטלגרם", str(ctx.exception))   # where to fix it
        self.assertEqual(self.led.month_usd(), 0.0)
        with self.assertRaises(ai_advisor.AdvisorError):
            self._ask(mock.Mock(side_effect=anthropic.APITimeoutError(request=req)))
        self.assertGreater(self.led.month_usd(), 0.05)

    def test_a_console_spend_limit_is_explained(self):
        import anthropic
        http = _http()
        req = http.Request("POST", "https://api.anthropic.com/v1/messages")
        limited = anthropic.RateLimitError("limit", response=http.Response(429, request=req), body=None)
        with self.assertRaises(ai_advisor.AdvisorError) as ctx:
            self._ask(mock.Mock(side_effect=limited))
        self.assertIn("Console", str(ctx.exception))


class KeyLookupTest(unittest.TestCase):
    def test_the_stored_key_wins_and_reaches_the_client(self):
        secrets_store.save(secrets_store.ANTHROPIC_KEY, "sk-ant-stored-0000000000")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-from-env"}):
            self.assertEqual(ai_advisor.api_key(), "sk-ant-stored-0000000000")
            self.assertEqual(ai_advisor.key_source(), "store")
            import anthropic
            resp = NS(stop_reason="end_turn", content=[NS(type="text", text="ok")], usage=None)
            fake = NS(beta=NS(messages=NS(create=mock.Mock(return_value=resp))))
            with mock.patch.object(anthropic, "Anthropic", return_value=fake) as make:
                ai_advisor.ask_claude({}, budget=_budget(), spend=_ledger())
        self.assertEqual(make.call_args.kwargs["api_key"], "sk-ant-stored-0000000000")
        self.assertEqual(make.call_args.kwargs["max_retries"], 0)

    def test_without_a_stored_key_the_environment_still_works(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-from-env"}):
            self.assertEqual(ai_advisor.api_key(), "sk-from-env")
            self.assertEqual(ai_advisor.key_source(), "env")

    def test_check_key_costs_nothing_and_reports_a_rejection(self):
        import anthropic
        http = _http()
        client = NS(models=NS(list=mock.Mock()))
        self.assertEqual(ai_advisor.check_key("k", client=client), (True, ""))
        client.models.list.assert_called_once_with(limit=1)
        req = http.Request("GET", "https://api.anthropic.com/v1/models")
        client.models.list.side_effect = anthropic.AuthenticationError(
            "no", response=http.Response(401, request=req), body=None)
        ok, why = ai_advisor.check_key("k", client=client)
        self.assertIs(ok, False)
        self.assertIn("Console", why)
        client.models.list.side_effect = anthropic.APIConnectionError(request=req)
        self.assertIsNone(ai_advisor.check_key("k", client=client)[0])


if __name__ == "__main__":
    unittest.main()
