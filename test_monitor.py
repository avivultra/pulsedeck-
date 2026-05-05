"""Unit tests for pure helpers in monitor.py (stdlib unittest, no extra deps)."""

import tempfile
import unittest
from pathlib import Path

import monitor
from metric_history import append_metrics_row, render_history_chart


class TestFormatUptime(unittest.TestCase):
    def test_seconds_only(self) -> None:
        self.assertEqual(monitor.format_uptime(0), "0s")
        self.assertEqual(monitor.format_uptime(45), "45s")

    def test_minutes_and_seconds(self) -> None:
        self.assertEqual(monitor.format_uptime(65), "1m 5s")

    def test_hour_without_redundant_zeros(self) -> None:
        self.assertEqual(monitor.format_uptime(3600), "1h")

    def test_negative(self) -> None:
        self.assertEqual(monitor.format_uptime(-1), "?")


class TestAsciiBar(unittest.TestCase):
    def test_clamped(self) -> None:
        self.assertEqual(len(monitor.ascii_bar(50)), len(monitor.ascii_bar(0)))
        self.assertTrue(monitor.ascii_bar(100).count("#") >= monitor.ascii_bar(0).count("#"))


class TestGibFormat(unittest.TestCase):
    def test_pair(self) -> None:
        one_gib = 1024**3
        s = monitor.format_gib_usage(one_gib // 2, one_gib)
        self.assertIn("0.5", s)
        self.assertIn("1.0", s)


class TestMetricHistory(unittest.TestCase):
    def test_append_writes_header(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.csv"
            append_metrics_row(
                p,
                unix_time=1_700_000_000.0,
                cpu_percent=12.3,
                ram_percent=45.6,
                disk_percent=None,
                swap_percent=None,
                temp_celsius=55.5,
            )
            text = p.read_text(encoding="utf-8")
            self.assertIn("timestamp_iso", text)
            self.assertIn("55.5", text)

    def test_chart_requires_two_rows(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            csv_p = Path(d) / "a.csv"
            png_p = Path(d) / "out.png"
            append_metrics_row(
                csv_p,
                unix_time=1.0,
                cpu_percent=1.0,
                ram_percent=2.0,
                disk_percent=None,
                swap_percent=None,
                temp_celsius=None,
            )
            self.assertFalse(render_history_chart(csv_p, png_p))

    def test_chart_writes_with_two_rows(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            csv_p = Path(d) / "b.csv"
            png_p = Path(d) / "c.png"
            for t in (1.0, 2.0):
                append_metrics_row(
                    csv_p,
                    unix_time=1_700_000_000.0 + t,
                    cpu_percent=10.0 + t,
                    ram_percent=20.0,
                    disk_percent=30.0,
                    swap_percent=None,
                    temp_celsius=40.0,
                )
            self.assertTrue(render_history_chart(csv_p, png_p))
            self.assertGreater(png_p.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()
