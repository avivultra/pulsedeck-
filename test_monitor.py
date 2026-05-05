"""Unit tests for pure helpers in monitor.py (stdlib unittest, no extra deps)."""

import unittest

import monitor


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


if __name__ == "__main__":
    unittest.main()
