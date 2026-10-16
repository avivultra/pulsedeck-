"""Tests for the Ask-Claude connection (no network: the client is faked)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import ai_advisor


def _snap(**kw):
    base = dict(cpu_percent=91.0, cpu_logical=8, ram_percent=60.0, ram_used=6 * 1024 ** 3,
                ram_total=16 * 1024 ** 3, disk_percent=70.0, swap_percent=None,
                temp_celsius=None, battery_plugged=None)
    base.update(kw)
    return NS(**base)


class LoadEnvTest(unittest.TestCase):
    def test_reads_values_strips_quotes_and_keeps_existing_vars(self):
        p = Path(tempfile.mkdtemp()) / ".env"
        p.write_text('# comment\nexport PD_TEST_A="one"\nPD_TEST_B=two\n\nPD_TEST_C=fromfile\n',
                     encoding="utf-8")
        with mock.patch.dict(os.environ, {"PD_TEST_C": "already"}, clear=False):
            ai_advisor.load_env(p)
            self.assertEqual(os.environ["PD_TEST_A"], "one")
            self.assertEqual(os.environ["PD_TEST_B"], "two")
            self.assertEqual(os.environ["PD_TEST_C"], "already")
        for k in ("PD_TEST_A", "PD_TEST_B"):
            os.environ.pop(k, None)

    def test_missing_file_is_fine(self):
        ai_advisor.load_env(Path(tempfile.mkdtemp()) / "nope.env")


class SnapshotTest(unittest.TestCase):
    def test_names_and_numbers_only(self):
        groups = [("chrome.exe", 40.0, 2 * 1024 ** 3, 12), ("idle.exe", 0.0, 10, 1)]
        events = [{"time": "10:00:00", "reason": "CPU jumped",
                   "suspects": [("chrome.exe", "1900 MiB")]}]
        s = ai_advisor.build_snapshot(_snap(), groups, events)
        self.assertEqual(s["now"]["cpu_percent"], 91.0)
        self.assertEqual([g["name"] for g in s["top_cpu"]], ["chrome.exe"])   # 0 % dropped
        self.assertEqual(s["top_memory"][0]["memory_mb"], 2048)
        self.assertEqual(s["load_events_today"][0]["suspects"], ["chrome.exe"])


class AskClaudeTest(unittest.TestCase):
    def _client(self, response):
        create = mock.Mock(return_value=response)
        return NS(beta=NS(messages=NS(create=create))), create

    def test_sends_snapshot_with_fallback_and_returns_text(self):
        resp = NS(stop_reason="end_turn", content=[NS(type="thinking"), NS(type="text", text="All good.")])
        client, create = self._client(resp)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            out = ai_advisor.ask_claude({"now": {}}, "why slow?", language="he", client=client)
        self.assertEqual(out, "All good.")
        kw = create.call_args.kwargs
        self.assertEqual(kw["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", kw["betas"])
        self.assertIn("Hebrew", kw["system"])
        self.assertIn("why slow?", kw["messages"][0]["content"])

    def test_refusal_becomes_a_user_message(self):
        client, _ = self._client(NS(stop_reason="refusal", content=[]))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with self.assertRaises(ai_advisor.AdvisorError):
                ai_advisor.ask_claude({}, client=client)

    def test_no_key_explains_the_env_file(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(ai_advisor, "ENV_PATH", Path(tempfile.mkdtemp()) / ".env"):
            with self.assertRaises(ai_advisor.AdvisorError) as ctx:
                ai_advisor.ask_claude({})
        self.assertIn(".env", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
