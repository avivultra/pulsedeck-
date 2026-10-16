"""Tests for per-program history (SQLite in a temp dir)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as P

from process_history import ProcessHistory

MB = 1024 ** 2


def procs(chrome_cpu, code_cpu=1.0):
    return [P(name="chrome.exe", cpu_percent=chrome_cpu / 2, rss_bytes=800 * MB),
            P(name="chrome.exe", cpu_percent=chrome_cpu / 2, rss_bytes=700 * MB),
            P(name="Code.exe", cpu_percent=code_cpu, rss_bytes=400 * MB),
            P(name="System Idle Process", cpu_percent=90, rss_bytes=0)]


class ProcessHistoryTest(unittest.TestCase):
    def setUp(self):
        self.h = ProcessHistory(Path(tempfile.mkdtemp()) / "p.sqlite", rollup_sec=15)

    def tearDown(self):
        self.h.close()

    def test_rollup_folds_names_averages_cpu_and_answers_around(self):
        t0 = 1_000_000.0
        for i, cpu in enumerate([10, 30, 20]):          # three ticks, then the window closes
            self.h.offer(procs(cpu), t0 + i * 5)
        self.h.offer(procs(0), t0 + 16)                  # flush at >= 15 s
        busy = self.h.around(t0 + 16, window_sec=30)
        top = busy["cpu"][0]
        self.assertEqual(top["name"], "chrome")
        self.assertAlmostEqual(top["cpu"], 15.0, places=1)   # (10+30+20+0)/4
        self.assertEqual(top["rss"], 1500 * MB)
        self.assertEqual(top["count"], 2)
        self.assertNotIn("System Idle Process", [i["name"] for i in busy["memory"]])

    def test_nothing_written_before_the_window_closes_and_series(self):
        t0 = 2_000_000.0
        self.h.offer(procs(10), t0)
        self.assertEqual(self.h.who_was_busy(t0 - 100, t0 + 100)["memory"], [])
        self.h.offer(procs(10), t0 + 20)
        self.h.offer(procs(50), t0 + 40)
        s = self.h.series("chrome.exe", t0, t0 + 100)
        self.assertEqual(len(s), 2)
        self.assertGreater(s[1][1], s[0][1])

    def test_old_rows_are_pruned(self):
        old = 3_000_000.0
        self.h.offer(procs(10), old)
        self.h.offer(procs(10), old + 20)
        new = old + 20 * 86400
        self.h.offer(procs(10), new)
        self.h.offer(procs(10), new + 20)                # first flush after a day prunes
        self.assertEqual(self.h.who_was_busy(old - 10, old + 100)["memory"], [])
        self.assertTrue(self.h.who_was_busy(new, new + 100)["memory"])


if __name__ == "__main__":
    unittest.main()
