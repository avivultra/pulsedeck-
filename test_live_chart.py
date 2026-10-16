"""Tests for the live chart's data layer (no window is opened)."""
from __future__ import annotations

import math
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import live_chart
from live_chart import (
    MAX_PLOT_POINTS,
    _IncrementalSeriesReader,
    _wall_days,
    _window,
)

HEADER = ("timestamp_iso,unix_time,cpu_percent,ram_percent,disk_percent,"
          "swap_percent,temp_celsius,vram_percent\n")
T0 = datetime(2026, 10, 1, 12, 0, 0)


def _row(i: int, cpu: float = 10.0, ram: float = 50.0, disk: str = "70.00") -> str:
    ts = T0 + timedelta(seconds=i)
    return f"{ts.isoformat(timespec='seconds')},{ts.timestamp():.3f},{cpu:.2f},{ram:.2f},{disk},,,\n"


class IncrementalReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.csv = self.dir / "metrics.csv"

    def test_reads_only_appended_rows(self):
        self.csv.write_text(HEADER + _row(0) + _row(1), encoding="utf-8")
        r = _IncrementalSeriesReader(self.csv)
        self.assertEqual(len(r.read()), 2)
        with self.csv.open("a", encoding="utf-8") as f:
            f.write(_row(2, cpu=99))
        s = r.read()
        self.assertEqual(len(s), 3)
        self.assertEqual(s.cols["cpu"][-1], 99.0)
        self.assertAlmostEqual(s.t[-1], _wall_days(T0 + timedelta(seconds=2)))

    def test_half_written_row_waits_for_its_newline(self):
        full = _row(1, cpu=42)
        self.csv.write_text(HEADER + _row(0) + full[:15], encoding="utf-8")
        r = _IncrementalSeriesReader(self.csv)
        self.assertEqual(len(r.read()), 1)
        with self.csv.open("a", encoding="utf-8") as f:
            f.write(full[15:])
        s = r.read()
        self.assertEqual(len(s), 2)
        self.assertEqual(s.cols["cpu"][-1], 42.0)

    def test_shrunk_file_is_reread(self):
        self.csv.write_text(HEADER + "".join(_row(i) for i in range(10)), encoding="utf-8")
        r = _IncrementalSeriesReader(self.csv)
        self.assertEqual(len(r.read()), 10)
        self.csv.write_text(HEADER + _row(20), encoding="utf-8")   # rotation
        self.assertEqual(len(r.read()), 1)

    def test_missing_optional_values_are_nan_and_bad_rows_skipped(self):
        self.csv.write_text(HEADER + _row(0, disk="") + "garbage,,x,y,,,,\n" + _row(1),
                            encoding="utf-8")
        s = _IncrementalSeriesReader(self.csv).read()
        self.assertEqual(len(s), 2)
        self.assertTrue(math.isnan(s.cols["disk"][0]))
        self.assertEqual(s.cols["disk"][1], 70.0)

    def test_big_file_skips_to_tail_and_ages_out_old_rows(self):
        keep_days = 100 / 86400            # keep ~100 seconds
        self.csv.write_text(HEADER + "".join(_row(i) for i in range(5000)), encoding="utf-8")
        r = _IncrementalSeriesReader(self.csv, keep_days=keep_days)
        dropped = []
        r.on_drop = dropped.append
        s = r.read()
        self.assertLess(len(s), 5000)       # did not parse the whole file
        self.assertAlmostEqual(s.t[-1], _wall_days(T0 + timedelta(seconds=4999)))
        with self.csv.open("a", encoding="utf-8") as f:
            f.write("".join(_row(i) for i in range(5000, 5300)))
        s = r.read()
        span_s = (s.t[-1] - s.t[0]) * 86400
        self.assertLessEqual(span_s, 100 * 1.1 + 1)
        self.assertTrue(dropped)


class WindowTest(unittest.TestCase):
    def _series(self, n: int):
        import numpy as np
        t = np.arange(n, dtype=float) / 86400
        cpu = np.full(n, 5.0)
        cpu[n // 3] = 97.0                  # a one-second spike
        return t, {"cpu": cpu, "ram": np.full(n, 50.0)}

    def test_long_window_is_capped_but_keeps_the_spike(self):
        t, cols = self._series(86400)
        wt, wcols, raw_n = _window(t, cols, None)
        self.assertEqual(raw_n, 86400)
        self.assertLessEqual(len(wt), MAX_PLOT_POINTS)
        self.assertEqual(wcols["cpu"].max(), 97.0)

    def test_window_start_is_respected(self):
        t, cols = self._series(1000)
        wt, _, raw_n = _window(t, cols, t[900])
        self.assertEqual(raw_n, 100)
        self.assertEqual(wt[0], t[900])

    def test_result_never_aliases_the_input(self):
        t, cols = self._series(10)
        wt, wcols, _ = _window(t, cols, None)
        wt[0] = -1
        wcols["cpu"][0] = -1
        self.assertNotEqual(t[0], -1)
        self.assertNotEqual(cols["cpu"][0], -1)


class SpikeSuspectsTest(unittest.TestCase):
    def test_suspects_are_parsed_per_event(self):
        d = Path(tempfile.mkdtemp())
        (d / f"spikes-{datetime.now():%Y-%m-%d}.md").write_text(
            "# Spike report\n\n## ⚠ 10:00:01 — CPU jumped\n\n"
            "- **CPU:** 10.0% → 90.0%\n- **RAM:** 50.0% → 51.0%\n\n"
            "1. `chrome.exe` — 1900 MiB\n2. `Code.exe` — 1100 MiB\n\n---\n\n"
            "## ⚠ 11:00:00 — RAM 91%\n\n- **RAM:** 80.0% → 91.0%\n",
            encoding="utf-8")
        events = live_chart._read_today_spikes(d)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["suspects"],
                         [("chrome.exe", "1900 MiB"), ("Code.exe", "1100 MiB")])
        self.assertEqual(events[0]["cpu_after"], 90.0)
        self.assertEqual(events[1]["suspects"], [])


class VerdictTest(unittest.TestCase):
    def test_calm(self):
        color, text, issues = live_chart._verdict(10, 40, 50)
        self.assertEqual(color, live_chart.GREEN)
        self.assertEqual(issues, [])

    def test_busy_cpu_is_amber_and_names_the_resource(self):
        color, _text, issues = live_chart._verdict(70, 40, None)
        self.assertEqual(color, live_chart.AMBER)
        self.assertEqual(issues, ["cpu"])

    def test_full_memory_is_red(self):
        color, _text, issues = live_chart._verdict(10, 95, None)
        self.assertEqual(color, live_chart.RED)
        self.assertEqual(issues, ["ram"])

    def test_heat_alone_is_red(self):
        color, _text, _issues = live_chart._verdict(10, 40, 90)
        self.assertEqual(color, live_chart.RED)


class GroupByNameTest(unittest.TestCase):
    def test_same_name_processes_fold_and_idle_is_ignored(self):
        from types import SimpleNamespace as P
        procs = [P(name="chrome.exe", cpu_percent=2.0, rss_bytes=100),
                 P(name="Chrome.exe", cpu_percent=3.0, rss_bytes=200),
                 P(name="System Idle Process", cpu_percent=90.0, rss_bytes=0),
                 P(name="code.exe", cpu_percent=1.0, rss_bytes=50)]
        groups = {g[0].lower(): g for g in live_chart._group_by_name(procs)}
        self.assertEqual(set(groups), {"chrome.exe", "code.exe"})
        _name, cpu, rss, count = groups["chrome.exe"]
        self.assertEqual((cpu, rss, count), (5.0, 300, 2))


class SelfUsageTest(unittest.TestCase):
    def test_reports_plausible_numbers(self):
        from self_usage import format_self_usage, self_usage
        pct, rss = self_usage()
        sum(i * i for i in range(200_000))      # burn a little CPU
        pct, rss = self_usage()
        self.assertGreaterEqual(pct, 0.0)
        self.assertLessEqual(pct, 100.0)
        self.assertGreater(rss, 1024 * 1024)
        self.assertIn("MB", format_self_usage(pct, rss))


if __name__ == "__main__":
    unittest.main()
