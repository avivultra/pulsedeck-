"""Tests for net_readings: the network rates the dock shows, read off the Tk thread.
No real network counters are read: every reader gets fake ones."""
from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace as NS

import net_readings
from net_readings import NetReader


class _Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def _io(sent, recv):
    return NS(bytes_sent=sent, bytes_recv=recv)


class RatesTest(unittest.TestCase):
    """The reader driven by hand: no thread."""

    def _reader(self, samples):
        clock = _Clock()
        it = iter(samples)
        return NetReader(counters=lambda: next(it), clock=clock, background=False), clock

    def test_rates_come_from_two_reads(self):
        r, clock = self._reader([_io(1_000, 50_000), _io(3_000, 150_000)])
        self.assertIsNone(r.rates())
        r.read_once()
        self.assertIsNone(r.rates())                    # one read is not a rate yet
        clock.t += 2.0
        r.read_once()
        self.assertEqual(r.rates(), (1_000.0, 50_000.0))

    def test_an_old_answer_is_not_shown(self):
        r, clock = self._reader([_io(0, 0), _io(10, 10)])
        r.read_once()
        clock.t += 1.0
        r.read_once()
        self.assertIsNotNone(r.rates())
        clock.t += net_readings.STALE_SEC + 0.1          # the next read hung
        self.assertIsNone(r.rates())

    def test_a_reset_counter_reads_as_zero(self):
        r, clock = self._reader([_io(9_000, 9_000), _io(1_000, 12_000)])
        r.read_once()
        clock.t += 1.0
        r.read_once()
        self.assertEqual(r.rates(), (0.0, 3_000.0))

    def test_a_long_gap_is_not_averaged_into_a_rate(self):
        r, clock = self._reader([_io(0, 0), _io(10**9, 10**9), _io(10**9 + 500, 10**9 + 500)])
        r.read_once()
        clock.t += net_readings.MAX_GAP_SEC + 50         # a sleeping machine
        r.read_once()
        self.assertIsNone(r.rates())
        clock.t += 1.0
        r.read_once()                                    # the next pair counts again
        self.assertEqual(r.rates(), (500.0, 500.0))

    def test_a_failed_read_keeps_the_last_answer(self):
        samples = [_io(0, 0), _io(100, 100)]
        clock = _Clock()

        def counters():
            if not samples:
                raise OSError("adapter went away")
            return samples.pop(0)

        r = NetReader(counters=counters, clock=clock, background=False)
        r.read_once()
        clock.t += 1.0
        r.read_once()
        r.read_once()                                    # raises inside, never outside
        self.assertEqual(r.rates(), (100.0, 100.0))

    def test_no_interfaces_means_no_rates(self):
        r = NetReader(counters=lambda: None, clock=_Clock(), background=False)
        r.read_once()
        r.read_once()
        self.assertIsNone(r.rates())


class BackgroundTest(unittest.TestCase):
    """The real thread, with fake counters."""

    def _wait_for(self, cond, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def test_a_hung_read_never_blocks_the_caller(self):
        release = threading.Event()
        entered = threading.Event()

        def stuck():
            entered.set()
            release.wait(10)
            return _io(0, 0)

        r = NetReader(counters=stuck)
        self.addCleanup(r.stop)
        self.addCleanup(release.set)
        started = time.perf_counter()
        for _ in range(5):                              # five ticks of the dock
            self.assertIsNone(r.rates())
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertTrue(entered.wait(3), "the reader thread never read")

    def test_several_windows_share_one_read(self):
        calls = []

        def counting():
            calls.append(time.monotonic())
            return _io(len(calls) * 1000, 0)

        r = NetReader(counters=counting)
        self.addCleanup(r.stop)
        for _ in range(10):                             # the dock and the vault, same moment
            r.rates()
        self.assertTrue(self._wait_for(lambda: calls), "the reader thread never read")
        self.assertEqual(len(calls), 1)
        r.rates()                                       # asked again: read again
        self.assertTrue(self._wait_for(lambda: len(calls) >= 2))
        self.assertGreaterEqual(calls[1] - calls[0], net_readings.MIN_INTERVAL_SEC - 0.05)

    def test_nothing_is_read_while_nobody_asks(self):
        calls = []
        r = NetReader(counters=lambda: calls.append(1) or _io(0, 0))
        self.addCleanup(r.stop)
        r.rates()
        self.assertTrue(self._wait_for(lambda: calls))
        time.sleep(net_readings.MIN_INTERVAL_SEC * 3)
        self.assertEqual(len(calls), 1)


class TkThreadTest(unittest.TestCase):
    def test_the_windows_never_read_the_counters_themselves(self):
        from pathlib import Path
        here = Path(__file__).parent
        for name in ("dock_strip.py", "vault_window.py"):
            source = (here / name).read_text(encoding="utf-8")
            self.assertNotIn("net_io_counters", source, f"{name} reads on the Tk thread again")
            self.assertIn("read_net_rates", source)


if __name__ == "__main__":
    unittest.main()
