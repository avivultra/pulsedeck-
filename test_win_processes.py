"""Tests for the one-call process table (win_processes) and the sampler's use
of it. Read-only: nothing is started except a busy loop in this process."""
from __future__ import annotations

import os
import struct
import time
import unittest
from unittest import mock

import psutil

import win_processes as wp
from process_monitor import ProcessSampler

BASE = 0x10000          # fake address of the buffer, for the name pointers


def _entry(pid, name, create_unix, cpu_s, rss, next_off):
    e = bytearray(wp._ENTRY_MIN)
    create = int(create_unix * 1e7) + wp._EPOCH_DIFF_100NS if create_unix else 0
    struct.pack_into("<I", e, 0, next_off)
    struct.pack_into("<qqq", e, wp._OFF_CREATE_USER_KERNEL, create, int(cpu_s * 1e7 * 0.75), int(cpu_s * 1e7 * 0.25))
    struct.pack_into("<H", e, wp._OFF_NAME_LENGTH, len(name) * 2)
    struct.pack_into("<Q", e, wp._OFF_PID, pid)
    struct.pack_into("<Q", e, wp._OFF_WORKING_SET, rss)
    return e


def _table(rows):
    """A SYSTEM_PROCESS_INFORMATION chain with each name right after its entry."""
    blobs, offset = [], 0
    for i, (pid, name, ct, cpu, rss) in enumerate(rows):
        size = wp._ENTRY_MIN + len(name) * 2
        e = _entry(pid, name, ct, cpu, rss, size if i < len(rows) - 1 else 0)
        if name:
            struct.pack_into("<Q", e, wp._OFF_NAME_BUFFER, BASE + offset + wp._ENTRY_MIN)
        blobs.append(bytes(e) + name.encode("utf-16-le"))
        offset += size
    return b"".join(blobs)


class ParseTest(unittest.TestCase):
    def test_reads_every_field_of_every_entry(self):
        buf = _table([(0, "", 0, 9000.0, 8192), (4, "", 0, 50.0, 1 << 20),
                      (1234, "chrome.exe", 1_700_000_000.5, 12.5, 300 << 20)])

        def read_name(addr, chars):
            start = addr - BASE
            return buf[start:start + chars * 2].decode("utf-16-le")

        rows = wp.parse(buf, len(buf), read_name, boot_time=1_699_000_000.0)
        self.assertEqual([r[:2] for r in rows], [(0, "System Idle Process"), (4, "System"), (1234, "chrome.exe")])
        pid, name, created, cpu, rss = rows[2]
        self.assertAlmostEqual(created, 1_700_000_000.5, places=3)
        self.assertAlmostEqual(cpu, 12.5, places=3)
        self.assertEqual(rss, 300 << 20)
        self.assertEqual(rows[1][2], 1_699_000_000.0)      # no start time: boot time

    def test_stops_at_the_reported_size(self):
        buf = _table([(8, "a.exe", 1.0e9, 1.0, 1), (9, "b.exe", 1.0e9, 1.0, 1)])
        first = wp._ENTRY_MIN + 10
        self.assertEqual(len(wp.parse(buf, first, lambda a, n: "x")), 1)


@unittest.skipUnless(wp.AVAILABLE, "Windows, 64-bit Python")
class AgreesWithPsutilTest(unittest.TestCase):
    def test_this_process_reads_the_same_as_psutil(self):
        rows = {r[0]: r for r in wp.Snapshotter().read()}
        me = psutil.Process()
        ct = me.cpu_times()
        pid, name, created, cpu_s, rss = rows[os.getpid()]
        self.assertEqual(name, me.name())
        self.assertAlmostEqual(created, me.create_time(), delta=1.0)
        self.assertAlmostEqual(cpu_s, ct.user + ct.system, delta=0.5)
        self.assertGreater(rss, 0)

    def test_sees_the_same_processes(self):
        native = {r[0] for r in wp.Snapshotter().read()}
        listed = set(psutil.pids())
        self.assertGreater(len(native & listed) / max(1, len(listed)), 0.95)


class SamplerTest(unittest.TestCase):
    def test_measures_cpu_between_ticks(self):
        s = ProcessSampler()
        s._tick()
        s._tick()                         # every process now primed
        t0 = time.process_time()
        while time.process_time() - t0 < 0.4:
            pass
        s._tick()
        me = next(p for p in s.snapshot() if p.pid == os.getpid())
        self.assertGreater(me.cpu_percent_raw, 20)

    def test_a_reused_pid_starts_over(self):
        s = ProcessSampler()
        tables = [[(10, "a.exe", 100.0, 1.0, 5 << 20)],
                  [(10, "a.exe", 100.0, 1.5, 5 << 20)],
                  [(10, "b.exe", 200.0, 0.1, 5 << 20)]]
        s._native = mock.Mock()
        s._native.read.side_effect = tables
        s._tick()
        self.assertEqual(s.snapshot(), [])                    # first sighting
        s._tick()
        self.assertEqual([p.name for p in s.snapshot()], ["a.exe"])
        s._tick()
        self.assertEqual(s.snapshot(), [])                    # a new process: first sighting again

    def test_falls_back_to_psutil_if_the_native_read_fails(self):
        s = ProcessSampler()
        s._native = mock.Mock()
        s._native.read.side_effect = OSError("boom")
        s._tick()
        s._tick()
        self.assertIsNone(s._native)
        self.assertTrue(s.snapshot())


if __name__ == "__main__":
    unittest.main()
