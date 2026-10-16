"""Tests for finding slow moments."""
from __future__ import annotations

import unittest

import numpy as np

import i18n
from slowdown import describe, find_episodes

DAY = 86400.0


def series(seconds, busy_ranges, value=90.0, base=20.0):
    t = np.arange(seconds, dtype=float) / DAY + 20000.0
    cpu = np.full(seconds, base)
    for a, b in busy_ranges:
        cpu[a:b] = value
    return t, cpu, np.full(seconds, 50.0)


class FindEpisodesTest(unittest.TestCase):
    def test_short_bursts_are_ignored_and_long_ones_found(self):
        t, cpu, ram = series(1000, [(100, 110), (300, 400)])
        eps = find_episodes(t, cpu, ram)
        self.assertEqual(len(eps), 1)
        self.assertAlmostEqual((eps[0]["start"] - t[0]) * DAY, 300, delta=1)
        self.assertEqual(eps[0]["kind"], "cpu")
        self.assertEqual(eps[0]["peak_cpu"], 90.0)

    def test_close_bursts_merge_into_one_episode(self):
        t, cpu, ram = series(1000, [(100, 120), (150, 170)])      # 30 s gap < 90 s
        eps = find_episodes(t, cpu, ram)
        self.assertEqual(len(eps), 1)
        self.assertAlmostEqual((eps[0]["end"] - eps[0]["start"]) * DAY, 69, delta=1)

    def test_memory_pressure_and_nan(self):
        t, cpu, ram = series(500, [])
        ram[200:300] = 92.0
        cpu[10] = np.nan
        eps = find_episodes(t, cpu, ram)
        self.assertEqual([e["kind"] for e in eps], ["ram"])

    def test_empty(self):
        self.assertEqual(find_episodes([], [], []), [])


class DescribeTest(unittest.TestCase):
    def test_names_the_busiest(self):
        i18n.set_language("en")
        try:
            t, cpu, ram = series(500, [(100, 200)])
            ep = find_episodes(t, cpu, ram)[0]
            text = describe(ep, {"cpu": [{"name": "chrome", "cpu": 35.0}, {"name": "MsMpEng", "cpu": 12.0}],
                                 "memory": []})
            self.assertIn("CPU reached 90%", text)
            self.assertIn("mostly chrome (35%), MsMpEng (12%)", text)
        finally:
            i18n.set_language(None)


if __name__ == "__main__":
    unittest.main()
