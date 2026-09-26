"""Tests for 'clean all', the freeze black box, and the lighter probes.

The rule under test everywhere: nothing active is ever offered or closed.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import psutil

import alerts
import freeze_watch
import idle_cleanup
from idle_cleanup import MIB, CleanupTarget
from process_monitor import ProcessInfo

WIN = "c:\\windows\\"


def _info(pid, name, rss_mb, idle_sec, observed=3600.0):
    return ProcessInfo(pid=pid, name=name, cpu_percent=0, cpu_percent_raw=0,
                       rss_bytes=rss_mb * MIB, process_uptime_seconds=observed,
                       last_active_seconds_ago=idle_sec, observed_seconds=observed)


class _FakeProc:
    def __init__(self, table, pid):
        if pid not in table:
            raise psutil.NoSuchProcess(pid)
        self._t = table
        self.pid = pid
        self._d = table[pid]

    def name(self):
        return self._d["name"]

    def parent(self):
        ppid = self._d.get("ppid")
        return _FakeProc(self._t, ppid) if ppid in self._t else None

    def children(self, recursive=False):
        return [_FakeProc(self._t, p) for p, d in self._t.items()
                if d.get("ppid") == self.pid]

    def memory_info(self):
        return SimpleNamespace(rss=self._d.get("rss", 0))

    def username(self):
        return self._d.get("user", "PC\\aviv")

    def exe(self):
        return self._d.get("exe", "C:\\Program Files\\App\\app.exe")

    def create_time(self):
        return 1000.0


class IdleHogTargetsTest(unittest.TestCase):
    def _run(self, snap, table, *, windowed=(), networked=(), ancestors=None):
        sampler = mock.Mock()
        sampler.snapshot.return_value = snap
        with mock.patch.object(idle_cleanup.psutil, "Process",
                               side_effect=lambda pid: _FakeProc(table, pid)), \
             mock.patch.object(idle_cleanup, "visible_window_pids",
                               return_value=set(windowed)), \
             mock.patch.object(idle_cleanup, "_network_pids",
                               return_value=set(networked)), \
             mock.patch.object(idle_cleanup, "_self_and_ancestors",
                               return_value={99999}), \
             mock.patch.object(idle_cleanup, "_ancestor_pids",
                               side_effect=lambda pid: (ancestors or {}).get(pid, [])), \
             mock.patch.object(idle_cleanup, "_windows_dir", return_value=WIN), \
             mock.patch("ghost_sweeper._is_current_user",
                        side_effect=lambda u: u.endswith("aviv")):
            return idle_cleanup.idle_hog_targets(sampler)

    def test_idle_tray_app_is_offered(self):
        targets, _ = self._run([_info(10, "Malwarebytes.exe", 266, None)],
                               {10: {"name": "Malwarebytes.exe"}})
        self.assertEqual([t.pid for t in targets], [10])
        self.assertEqual(targets[0].kind, "idle")

    def test_recently_active_is_never_offered(self):
        targets, _ = self._run([_info(10, "app.exe", 500, 60.0)],
                               {10: {"name": "app.exe"}})
        self.assertEqual(targets, [])

    def test_open_window_protects_process_and_its_children(self):
        table = {10: {"name": "app.exe"}, 11: {"name": "helper.exe", "ppid": 10}}
        snap = [_info(10, "app.exe", 300, None), _info(11, "helper.exe", 300, None)]
        targets, _ = self._run(snap, table, windowed={10}, ancestors={11: [10]})
        self.assertEqual(targets, [])

    def test_network_activity_protects(self):
        targets, _ = self._run([_info(10, "app.exe", 300, None)],
                               {10: {"name": "app.exe"}}, networked={10})
        self.assertEqual(targets, [])

    def test_windows_system_other_users_small_and_known_are_skipped(self):
        table = {
            1: {"name": "SearchApp.exe", "exe": "C:\\Windows\\SystemApps\\s.exe"},
            2: {"name": "svc.exe", "user": "NT AUTHORITY\\SYSTEM"},
            3: {"name": "tiny.exe"},
            4: {"name": "MsMpEng.exe"},
            5: {"name": "claude.exe"},           # sweeper's domain, never a hog
        }
        snap = [_info(1, "SearchApp.exe", 300, None), _info(2, "svc.exe", 300, None),
                _info(3, "tiny.exe", 40, None), _info(4, "MsMpEng.exe", 500, None),
                _info(5, "claude.exe", 600, None)]
        targets, _ = self._run(snap, table)
        self.assertEqual(targets, [])

    def test_same_name_children_fold_into_one_family(self):
        table = {10: {"name": "msedge.exe"},
                 11: {"name": "msedge.exe", "ppid": 10},
                 12: {"name": "msedge.exe", "ppid": 10}}
        snap = [_info(10, "msedge.exe", 60, None), _info(11, "msedge.exe", 50, None),
                _info(12, "msedge.exe", 40, None)]
        targets, _ = self._run(snap, table)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].rss_bytes, 150 * MIB)
        self.assertEqual(set(targets[0].member_pids), {10, 11, 12})

    def test_one_busy_family_member_makes_the_family_active(self):
        table = {10: {"name": "msedge.exe"}, 11: {"name": "msedge.exe", "ppid": 10}}
        snap = [_info(10, "msedge.exe", 150, None), _info(11, "msedge.exe", 50, 10.0)]
        targets, _ = self._run(snap, table)
        self.assertEqual(targets, [])

    def test_cannot_prove_idleness_right_after_start(self):
        targets, note = self._run([_info(10, "app.exe", 300, None, observed=90.0)],
                                  {10: {"name": "app.exe"}})
        self.assertEqual(targets, [])
        self.assertIn("דקות", note)

    def test_no_sampler(self):
        targets, note = idle_cleanup.idle_hog_targets(None)
        self.assertEqual(targets, [])
        self.assertTrue(note)


class GhostTargetsTest(unittest.TestCase):
    def _ghost(self, pid, verdict):
        return SimpleNamespace(pid=pid, name="node.exe", create_time=1.0,
                               rss_bytes=200 * MIB, verdict=verdict,
                               idle_for_seconds=3600.0)

    def test_only_finished_ghosts_are_offered(self):
        ghosts = [self._ghost(1, "FINISHED"), self._ghost(2, "STUCK"),
                  self._ghost(3, "LEAKING"), self._ghost(4, "WORKING")]
        with mock.patch.object(idle_cleanup, "visible_window_pids", return_value=set()):
            targets = idle_cleanup.ghost_targets(ghosts)
        self.assertEqual([t.pid for t in targets], [1])

    def test_finished_ghost_with_open_window_is_left_alone(self):
        with mock.patch.object(idle_cleanup, "visible_window_pids", return_value={1}):
            self.assertEqual(idle_cleanup.ghost_targets([self._ghost(1, "FINISHED")]), [])


class VerifyIdleTest(unittest.TestCase):
    def _t(self, pid):
        return CleanupTarget(pid=pid, name=f"p{pid}.exe", create_time=1.0,
                             rss_bytes=200 * MIB, kind="idle", detail="",
                             member_pids=(pid,))

    def _verify(self, before, after, *, windowed=(), established=()):
        calls = {"n": 0}
        n_targets = len(before)

        def fake(pid, _ct):
            calls["n"] += 1
            # First pass reads `before`; everything after reads `after`.
            src = before if calls["n"] <= n_targets else after
            return src.get(pid)

        with mock.patch.object(idle_cleanup, "_cpu_and_rss", side_effect=fake), \
             mock.patch.object(idle_cleanup, "visible_window_pids",
                               return_value=set(windowed)), \
             mock.patch.object(idle_cleanup, "_established_pids",
                               return_value=set(established)):
            return idle_cleanup.verify_idle([self._t(p) for p in before], sample_seconds=0)

    def test_still_idle_passes(self):
        ok, skipped = self._verify({1: (5.0, 100 * MIB)}, {1: (5.01, 100 * MIB)})
        self.assertEqual([t.pid for t in ok], [1])
        self.assertEqual(skipped, [])

    def test_woke_up_is_skipped(self):
        ok, skipped = self._verify({1: (5.0, 100 * MIB)}, {1: (5.5, 100 * MIB)})
        self.assertEqual(ok, [])
        self.assertIn("מעבד", skipped[0][1])

    def test_growing_memory_is_skipped(self):
        ok, skipped = self._verify({1: (5.0, 100 * MIB)}, {1: (5.0, 150 * MIB)})
        self.assertEqual(ok, [])

    def test_new_window_or_connection_is_skipped(self):
        ok, _ = self._verify({1: (5.0, 1)}, {1: (5.0, 1)}, windowed={1})
        self.assertEqual(ok, [])
        ok, _ = self._verify({1: (5.0, 1)}, {1: (5.0, 1)}, established={1})
        self.assertEqual(ok, [])

    def test_gone_or_reused_pid_is_skipped(self):
        ok, skipped = self._verify({1: (5.0, 1)}, {1: None})
        self.assertEqual(ok, [])
        self.assertEqual(skipped[0][1], "כבר לא רץ")


class TerminateProcessTest(unittest.TestCase):
    def test_protected_is_refused(self):
        self.assertEqual(alerts.terminate_process(4, "svchost.exe"), "protected")

    def test_wait_denied_but_process_gone_counts_as_closed(self):
        proc = mock.Mock()
        proc.create_time.return_value = 1.0
        proc.wait.side_effect = psutil.AccessDenied(123)
        with mock.patch.object(alerts.psutil, "Process", return_value=proc), \
             mock.patch.object(alerts, "_still_running", return_value=False):
            self.assertEqual(alerts.terminate_process(123, "app.exe", 1.0), "closed")

    def test_wait_denied_and_still_alive_is_denied(self):
        proc = mock.Mock()
        proc.create_time.return_value = 1.0
        proc.wait.side_effect = psutil.AccessDenied(123)
        with mock.patch.object(alerts.psutil, "Process", return_value=proc), \
             mock.patch.object(alerts, "_still_running", return_value=True):
            self.assertEqual(alerts.terminate_process(123, "app.exe", 1.0), "denied")

    def test_pid_reuse_is_left_alone(self):
        proc = mock.Mock()
        proc.create_time.return_value = 5000.0
        with mock.patch.object(alerts.psutil, "Process", return_value=proc):
            self.assertEqual(alerts.terminate_process(123, "app.exe", 1.0), "pid_reused")
        proc.terminate.assert_not_called()


class WmiProbeTest(unittest.TestCase):
    def test_gives_up_after_repeated_empty_answers(self):
        from cpu_probes import WindowsWmiProbe
        probe = WindowsWmiProbe()
        empty = SimpleNamespace(returncode=0, stdout="")
        with mock.patch("os.name", "nt"), \
             mock.patch("subprocess.run", return_value=empty) as run:
            for _ in range(10):
                if probe.is_available():
                    probe.read()
        self.assertEqual(run.call_count, WindowsWmiProbe.GIVE_UP_AFTER_EMPTY)

    def test_timeout_is_quiet_and_does_not_count(self):
        from cpu_probes import WindowsWmiProbe
        probe = WindowsWmiProbe()
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("powershell", 4)):
            self.assertIsNone(probe.read())
        self.assertEqual(probe._empty_answers, 0)


class FreezeWatchTest(unittest.TestCase):
    def tearDown(self):
        freeze_watch.stop()

    def test_header_and_recovery_stamp(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "freeze.log"
            freeze_watch.start(path, timeout=30)
            freeze_watch.beat()                              # normal beat
            freeze_watch._last_beat_mono = time.monotonic() - 45
            freeze_watch.beat()                              # after a stall
            freeze_watch.stop()
            text = path.read_text(encoding="utf-8")
        self.assertIn("PulseDeck started", text)
        self.assertEqual(text.count("UI recovered"), 1)


class SamplerObservedTest(unittest.TestCase):
    def test_observed_seconds_grows_from_first_sighting(self):
        from process_monitor import ProcessSampler
        s = ProcessSampler()
        s._tick()                                  # first sighting (primes)
        for pid in list(s._first_seen):
            s._first_seen[pid] -= 400              # pretend we saw it 400 s ago
        s._tick()
        observed = [p.observed_seconds for p in s.snapshot()]
        self.assertTrue(observed)
        self.assertGreaterEqual(max(observed), 399)


if __name__ == "__main__":
    unittest.main()
