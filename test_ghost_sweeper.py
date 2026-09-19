"""Tests for the Ghost Sweeper and for the non-blocking sensor/CSV paths.

Kept in its own module rather than appended to test_monitor.py because these
cover a different concern: not "is this value formatted right" but "does the
monitor stay responsive, and are the durations it reports honest".
"""

import logging
import tempfile
import time
import unittest
from pathlib import Path

from metric_history import CSV_FIELDNAMES, append_metrics_row


class TestGhostSweeperHelpers(unittest.TestCase):
    """Pure helpers — no processes, no threads."""

    def test_humanize_duration_scales(self) -> None:
        import ghost_sweeper as gs

        self.assertEqual(gs.humanize_duration(None), "—")
        self.assertIn("שניות", gs.humanize_duration(30))
        self.assertIn("דקות", gs.humanize_duration(600))
        self.assertIn("שעות", gs.humanize_duration(7200))
        self.assertIn("ימים", gs.humanize_duration(200000))

    def test_humanize_bytes(self) -> None:
        import ghost_sweeper as gs

        self.assertEqual(gs.humanize_bytes(None), "—")
        self.assertEqual(gs.humanize_bytes(10 * 1024 * 1024), "10 MiB")
        self.assertEqual(gs.humanize_bytes(2 * 1024 ** 3), "2.00 GiB")

    def test_is_current_user_strips_domain(self) -> None:
        import ghost_sweeper as gs

        user = gs._CURRENT_USER
        self.assertTrue(gs._is_current_user(user))
        self.assertTrue(gs._is_current_user("LAPTOP-X" + chr(92) + user))
        self.assertTrue(gs._is_current_user(user.upper()))
        # An unreadable owner means SYSTEM / a service account — never ours.
        self.assertFalse(gs._is_current_user(""))
        self.assertFalse(gs._is_current_user("NT AUTHORITY" + chr(92) + "SYSTEM"))

    def test_candidate_filter(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        # conhost belongs to the Janitor, never to the sweeper
        self.assertFalse(sw._is_candidate("conhost.exe", has_ports=True))
        self.assertFalse(sw._is_candidate("svchost.exe", has_ports=True))
        # known runtime, no ports needed
        self.assertTrue(sw._is_candidate("node.exe", has_ports=False))
        # unknown name, but it is listening — worth a look
        self.assertTrue(sw._is_candidate("mystery.exe", has_ports=True))
        self.assertFalse(sw._is_candidate("mystery.exe", has_ports=False))

    def test_trim_lineage_keeps_live_and_newest_dead(self) -> None:
        import ghost_sweeper as gs

        live = {1: ("a.exe", 100.0), 2: ("b.exe", 200.0)}
        dead = {1000 + i: (f"d{i}.exe", float(i)) for i in range(50)}
        trimmed = gs._trim_lineage({**live, **dead}, live_pids={1, 2},
                                   keep_dead=10)

        self.assertIn(1, trimmed)
        self.assertIn(2, trimmed)
        self.assertEqual(len(trimmed), 12)          # 2 live + 10 dead
        # The kept dead entries are the most recently started ones.
        self.assertIn(1049, trimmed)
        self.assertNotIn(1000, trimmed)

    def test_trim_lineage_noop_when_under_budget(self) -> None:
        import ghost_sweeper as gs

        lineage = {1: ("a.exe", 1.0), 99: ("dead.exe", 2.0)}
        self.assertIs(gs._trim_lineage(lineage, {1}, keep_dead=10), lineage)


class TestCommandLineRedaction(unittest.TestCase):
    """Secrets must be masked; ordinary arguments must survive untouched."""

    def _r(self, text: str) -> str:
        from ghost_sweeper import redact_cmdline

        return redact_cmdline(text)

    def _assert_masked(self, cmdline: str, secret: str) -> None:
        from ghost_sweeper import MASK

        out = self._r(cmdline)
        self.assertNotIn(secret, out, f"secret survived redaction in: {out}")
        self.assertIn(MASK, out)

    def test_equals_form(self) -> None:
        self._assert_masked("mysql -u root --password=hunter2 db", "hunter2")

    def test_colon_form(self) -> None:
        self._assert_masked("app.exe --client-secret:xyz789 --verbose", "xyz789")

    def test_space_separated_form(self) -> None:
        self._assert_masked("node s.js --api-key abc123XYZ --port 3000",
                            "abc123XYZ")

    def test_basic_auth_pair(self) -> None:
        self._assert_masked("ngrok http 8473 --basic-auth=aviv:Sup3rS3cret",
                            "Sup3rS3cret")

    def test_bare_env_style_assignment(self) -> None:
        self._assert_masked("set PASSWORD=letmein && run.bat", "letmein")

    def test_url_password_is_masked_but_host_survives(self) -> None:
        out = self._r("psql postgres://aviv:pw123@localhost:5432/app")
        self.assertNotIn("pw123", out)
        self.assertIn("localhost:5432/app", out)
        self.assertIn("aviv", out)          # username is not a secret

    def test_token_variants(self) -> None:
        for flag in ("--token", "--access-token", "--refresh_token",
                     "--apikey", "--auth-token"):
            self._assert_masked(f"python app.py {flag}=ghp_secretvalue",
                                "ghp_secretvalue")

    def test_case_insensitive(self) -> None:
        self._assert_masked("app --PASSWORD=Hunter2", "Hunter2")

    def test_key_pointing_at_a_file_is_not_a_secret(self) -> None:
        # `--key` on its own is nearly always a path to a .pem, not a password.
        cmd = "python train.py --key models/private.pem --epochs 10"
        self.assertEqual(self._r(cmd), cmd)

    def test_ordinary_command_line_is_untouched(self) -> None:
        cmd = ("E:" + chr(92) + "Watchdog" + chr(92) + "python.exe "
               "-m http.server 8473 --bind 127.0.0.1")
        self.assertEqual(self._r(cmd), cmd)

    def test_path_containing_the_word_password_is_untouched(self) -> None:
        cmd = ("ssh -i C:" + chr(92) + "Users" + chr(92) + "aviv" + chr(92)
               + "my-password-notes" + chr(92) + "id_rsa host")
        self.assertEqual(self._r(cmd), cmd)

    def test_empty_input(self) -> None:
        self.assertEqual(self._r(""), "")

    def test_redaction_is_idempotent(self) -> None:
        once = self._r("app --password=hunter2")
        self.assertEqual(self._r(once), once)

    def test_multiple_secrets_in_one_line(self) -> None:
        out = self._r("app --password=aaa --token=bbb --api-key=ccc")
        for secret in ("aaa", "bbb", "ccc"):
            self.assertNotIn(secret, out)

    def test_preference_round_trips_through_config(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import config as app_config
        import ghost_sweeper as gs

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.object(app_config, "PROJECT_DIR", base):
                self.assertTrue(gs.secrets_are_redacted())   # default on
                gs.set_secrets_redacted(False)
                self.assertFalse(gs.secrets_are_redacted())
                gs.set_secrets_redacted(True)
                self.assertTrue(gs.secrets_are_redacted())


class TestGhostSweeperParentResolution(unittest.TestCase):
    """The PID-reuse trap is the whole reason _resolve_parent exists."""

    def test_live_parent_older_than_child_is_the_real_parent(self) -> None:
        import ghost_sweeper as gs

        raw = {10: {"name": "claude.exe", "create_time": 100.0}}
        name, alive = gs._resolve_parent(10, 200.0, raw=raw, lineage={})
        self.assertEqual(name, "claude.exe")
        self.assertTrue(alive)

    def test_live_pid_younger_than_child_is_a_recycled_impostor(self) -> None:
        import ghost_sweeper as gs

        # PID 10 exists, but it started AFTER the child — so it cannot be the
        # child's parent. The real parent is dead and we must say so.
        raw = {10: {"name": "notepad.exe", "create_time": 500.0}}
        name, alive = gs._resolve_parent(10, 200.0, raw=raw,
                                         lineage={10: ("claude.exe", 100.0)})
        self.assertFalse(alive)
        self.assertEqual(name, "claude.exe")     # remembered, not the impostor

    def test_dead_parent_named_from_lineage_cache(self) -> None:
        import ghost_sweeper as gs

        name, alive = gs._resolve_parent(77, 200.0, raw={},
                                         lineage={77: ("node.exe", 50.0)})
        self.assertFalse(alive)
        self.assertEqual(name, "node.exe")

    def test_unknown_dead_parent_is_reported_as_unknown(self) -> None:
        import ghost_sweeper as gs

        self.assertEqual(gs._resolve_parent(77, 200.0, {}, {}), ("?", False))

    def test_ppid_zero_is_never_alive(self) -> None:
        import ghost_sweeper as gs

        self.assertEqual(gs._resolve_parent(0, 1.0, {}, {}), ("?", False))


class TestGhostSweeperClassification(unittest.TestCase):
    """_classify turns raw numbers into the sentence the user reads."""

    def _classify(self, **kw):
        import ghost_sweeper as gs

        base = dict(window_pct=0.0, lifetime_pct=0.0, idle_for=3600.0,
                    rss_delta=0, observed=600.0, established=0,
                    reasons=["ORPHAN"])
        base.update(kw)
        return gs._classify(**base)

    def test_high_cpu_reads_as_stuck(self) -> None:
        verdict, detail = self._classify(window_pct=80.0)
        self.assertEqual(verdict, "STUCK")
        self.assertIn("לולאה", detail)

    def test_growing_memory_without_cpu_reads_as_leak(self) -> None:
        verdict, detail = self._classify(rss_delta=200 * 1024 * 1024)
        self.assertEqual(verdict, "LEAKING")
        self.assertIn("זיכרון", detail)

    def test_active_connection_outranks_orphanhood(self) -> None:
        verdict, detail = self._classify(established=3)
        self.assertEqual(verdict, "WORKING")
        self.assertIn("3", detail)

    def test_single_connection_uses_singular_wording(self) -> None:
        _, detail = self._classify(established=1)
        self.assertIn("חיבור רשת פעיל אחד", detail)

    def test_spinning_beats_connections(self) -> None:
        # A process burning a core is stuck even if someone is connected.
        verdict, _ = self._classify(window_pct=90.0, established=2)
        self.assertEqual(verdict, "STUCK")

    def test_idle_and_quiet_reads_as_finished(self) -> None:
        verdict, detail = self._classify()
        self.assertEqual(verdict, "FINISHED")
        self.assertIn("לא ביצע שום עבודה", detail)

    def test_idle_server_gets_its_own_explanation(self) -> None:
        verdict, detail = self._classify(reasons=["IDLE_SERVER"])
        self.assertEqual(verdict, "FINISHED")
        self.assertIn("פורט", detail)

    def test_light_work_is_flagged_as_still_working(self) -> None:
        verdict, detail = self._classify(window_pct=8.0)
        self.assertEqual(verdict, "WORKING")
        self.assertIn("כדאי לבדוק", detail)

    def test_short_observation_falls_back_to_lifetime_average(self) -> None:
        # Watched for only 10 s: the window number is meaningless, so the
        # lifetime average must drive the verdict instead.
        verdict, _ = self._classify(observed=10.0, window_pct=0.0,
                                    lifetime_pct=90.0)
        self.assertEqual(verdict, "STUCK")


class TestGhostSweeperTracking(unittest.TestCase):
    """First-sighting estimates and cross-scan stability."""

    def test_first_sighting_backdates_idleness_from_lifetime(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        now = 10_000.0
        created = now - 7200.0          # two hours old
        track = sw._update_track(5, "python.exe", created,
                                 cpu_total=1.0,      # ~0% of two hours
                                 rss=1000, ppid=9, parent_alive=True,
                                 lineage={}, now=now)
        self.assertTrue(track.idle_since_is_estimate)
        self.assertEqual(track.last_cpu_activity_wall, created)

    def test_first_sighting_of_busy_process_is_not_backdated(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        now = 10_000.0
        created = now - 100.0
        track = sw._update_track(5, "python.exe", created,
                                 cpu_total=90.0,     # 90% of its life
                                 rss=1000, ppid=9, parent_alive=True,
                                 lineage={}, now=now)
        self.assertFalse(track.idle_since_is_estimate)
        self.assertEqual(track.last_cpu_activity_wall, now)

    def test_orphan_timestamp_is_backdated_then_frozen(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        now = 10_000.0
        created = now - 3600.0
        t1 = sw._update_track(5, "node.exe", created, 0.5, 1000, 9,
                              parent_alive=False, lineage={}, now=now)
        self.assertTrue(t1.orphan_since_is_estimate)
        self.assertEqual(t1.parent_missing_since_wall, created)

        # A later scan must not reset the clock — the reported orphan age has
        # to keep growing, not restart near zero.
        t2 = sw._update_track(5, "node.exe", created, 0.5, 1000, 9,
                              parent_alive=False, lineage={}, now=now + 1200)
        self.assertIs(t1, t2)
        self.assertEqual(t2.parent_missing_since_wall, created)

    def test_observed_cpu_activity_clears_the_estimate_flag(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        now = 10_000.0
        created = now - 7200.0
        sw._update_track(5, "node.exe", created, 1.0, 1000, 9, True, {}, now)
        track = sw._update_track(5, "node.exe", created, 31.0, 1000, 9,
                                 True, {}, now + 600)
        self.assertFalse(track.idle_since_is_estimate)
        self.assertEqual(track.last_cpu_activity_wall, now + 600)

    def test_cpu_jitter_below_epsilon_does_not_count_as_activity(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        now = 10_000.0
        created = now - 7200.0
        sw._update_track(5, "node.exe", created, 1.0, 1000, 9, True, {}, now)
        track = sw._update_track(5, "node.exe", created, 1.2, 1000, 9,
                                 True, {}, now + 600)
        self.assertEqual(track.last_cpu_activity_wall, created)
        self.assertTrue(track.idle_since_is_estimate)

    def test_pid_reuse_starts_a_fresh_track(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        first = sw._update_track(5, "node.exe", 100.0, 1.0, 1000, 9,
                                 True, {}, 200.0)
        second = sw._update_track(5, "python.exe", 9999.0, 0.0, 50, 9,
                                  True, {}, 10_000.0)
        self.assertIsNot(first, second)
        self.assertEqual(second.name, "python.exe")
        self.assertEqual(second.create_time, 9999.0)

    def test_ignore_list_matches_name_and_instance(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        self.assertFalse(sw._is_ignored("node.exe", "1:2"))
        sw.ignore_name("Node.EXE")
        self.assertTrue(sw._is_ignored("node.exe", "1:2"))
        sw.ignore_instance("7:123")
        self.assertTrue(sw._is_ignored("other.exe", "7:123"))
        self.assertFalse(sw._is_ignored("other.exe", "8:123"))


def _fake_ghost(**overrides):
    """A GhostProcess with sane defaults, for log/formatting tests."""
    import ghost_sweeper as gs

    fields = dict(
        pid=1234, name="node.exe", exe="C:/node.exe",
        cmdline="node server.js", cwd="E:/proj", username="aviv",
        create_time=1000.0, age_seconds=7200.0,
        parent_pid=99, parent_name="claude.exe", parent_alive=False,
        orphaned_for_seconds=3600.0, orphan_time_is_lower_bound=False,
        cpu_seconds_total=2.0, cpu_percent_window=0.1,
        cpu_percent_lifetime=0.1, idle_for_seconds=3600.0,
        idle_time_is_lower_bound=False, rss_bytes=10 * 1024 * 1024,
        rss_delta_bytes=0, num_threads=4, listening_ports=(),
        established_connections=0, reasons=("ORPHAN",), verdict="FINISHED",
        verdict_detail="…", observed_seconds=1200.0, first_scan=False,
    )
    fields.update(overrides)
    return gs.GhostProcess(**fields)


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class TestSweepLog(unittest.TestCase):
    """history/sweeper.log is the only thing that survives to be read later."""

    def setUp(self) -> None:
        import ghost_sweeper as gs

        # Swap the module's audit logger for an isolated one that writes
        # nowhere. Without this, running the suite in the project directory
        # would append fake "node.exe pid=1234" rows to the user's real
        # history/sweeper.log — the very file this feature exists to keep
        # trustworthy weeks later.
        self.handler = _CapturingHandler()
        isolated = logging.getLogger("ghost_sweeper.audit.test")
        isolated.handlers = [self.handler]
        isolated.setLevel(logging.INFO)
        isolated.propagate = False

        original = gs._sweep_logger
        gs._sweep_logger = isolated

        def _restore() -> None:
            gs._sweep_logger = original
            isolated.handlers = []

        self.addCleanup(_restore)

    def _lines(self, prefix: str) -> list[str]:
        return [ln for ln in self.handler.lines if ln.startswith(prefix)]

    def test_every_sweep_writes_one_summary_with_a_breakdown(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        sw._record_sweep([_fake_ghost(verdict="FINISHED"),
                          _fake_ghost(pid=2, verdict="STUCK")])
        summaries = self._lines("sweep:")
        self.assertEqual(len(summaries), 1)
        self.assertIn("2 ghosts", summaries[0])
        self.assertIn("finished=1", summaries[0])
        self.assertIn("stuck=1", summaries[0])
        self.assertIn("leaking=0", summaries[0])

    def test_empty_sweep_still_records_the_zero(self) -> None:
        import ghost_sweeper as gs

        gs.GhostSweeper()._record_sweep([])
        self.assertIn("0 ghosts", self._lines("sweep:")[0])

    def test_new_ghost_is_logged_once_not_every_sweep(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        g = _fake_ghost()
        sw._record_sweep([g])
        sw._record_sweep([g])
        sw._record_sweep([g])
        self.assertEqual(len(self._lines("NEW ")), 1)
        self.assertEqual(len(self._lines("sweep:")), 3)

    def test_new_line_carries_the_decision_making_fields(self) -> None:
        import ghost_sweeper as gs

        gs.GhostSweeper()._record_sweep([_fake_ghost()])
        line = self._lines("NEW ")[0]
        for expected in ("node.exe", "pid=1234", "verdict=FINISHED",
                         "reasons=ORPHAN", "claude.exe", "dead",
                         "cwd=E:/proj"):
            self.assertIn(expected, line)

    def test_gone_is_logged_when_a_ghost_disappears(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        sw._record_sweep([_fake_ghost()])
        sw._record_sweep([])
        gone = self._lines("GONE ")
        self.assertEqual(len(gone), 1)
        self.assertIn("node.exe", gone[0])
        self.assertIn("FINISHED", gone[0])

    def test_gone_is_not_repeated_on_later_sweeps(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        sw._record_sweep([_fake_ghost()])
        sw._record_sweep([])
        sw._record_sweep([])
        self.assertEqual(len(self._lines("GONE ")), 1)

    def test_command_lines_are_always_masked_on_disk(self) -> None:
        """The panel checkbox governs the screen. A log file outlives the
        session, so it never gets the unmasked version."""
        from unittest.mock import patch

        import ghost_sweeper as gs

        secret_cmd = "ngrok http 8473 --basic-auth=aviv:Sup3rS3cret"
        # Even with display masking explicitly turned OFF:
        with patch.object(gs, "secrets_are_redacted", return_value=False):
            gs.GhostSweeper()._record_sweep([_fake_ghost(cmdline=secret_cmd)])
        line = self._lines("NEW ")[0]
        self.assertNotIn("Sup3rS3cret", line)
        self.assertIn(gs.MASK, line)

    def test_user_action_is_recorded(self) -> None:
        import ghost_sweeper as gs

        gs.GhostSweeper().log_user_action(_fake_ghost(), "closed")
        line = self._lines("USER ")[0]
        self.assertIn("closed", line)
        self.assertIn("node.exe", line)
        self.assertIn("pid=1234", line)

    def test_a_pid_reused_by_a_new_process_counts_as_a_new_ghost(self) -> None:
        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        sw._record_sweep([_fake_ghost(pid=7, create_time=1000.0)])
        sw._record_sweep([_fake_ghost(pid=7, create_time=9999.0)])
        self.assertEqual(len(self._lines("NEW ")), 2)
        self.assertEqual(len(self._lines("GONE ")), 1)

    def test_logging_failure_never_breaks_a_scan(self) -> None:
        from unittest.mock import patch

        import ghost_sweeper as gs

        sw = gs.GhostSweeper()
        with patch.object(gs, "_get_sweep_logger",
                          side_effect=OSError("disk full")):
            sw._do_scan()               # must not raise
        self.assertIsInstance(sw.get_ghosts(), list)


class TestLogDurationFormat(unittest.TestCase):
    def test_compact_ascii_durations(self) -> None:
        import ghost_sweeper as gs

        self.assertEqual(gs._log_duration(None), "?")
        self.assertEqual(gs._log_duration(45), "45s")
        self.assertEqual(gs._log_duration(90), "1m")
        self.assertEqual(gs._log_duration(3600), "1h")
        self.assertEqual(gs._log_duration(3600 + 20 * 60), "1h20m")
        self.assertEqual(gs._log_duration(86400), "1d")
        self.assertEqual(gs._log_duration(86400 + 4 * 3600), "1d4h")

    def test_durations_are_pure_ascii_for_grepping(self) -> None:
        import ghost_sweeper as gs

        for seconds in (0, 45, 90, 3600, 100000, 1_000_000):
            gs._log_duration(seconds).encode("ascii")   # raises if not


class TestGhostSweeperIntegration(unittest.TestCase):
    """A real scan of this machine — no assumptions about what it finds."""

    def test_scan_returns_wellformed_ghosts_and_never_includes_self(self) -> None:
        import os

        import ghost_sweeper as gs

        sw = gs.GhostSweeper(idle_seconds=1800)
        ghosts = sw.scan()
        self.assertIsInstance(ghosts, list)
        for g in ghosts:
            self.assertNotEqual(g.pid, os.getpid())
            self.assertTrue(g.reasons, "a ghost must record why it was flagged")
            self.assertIn(g.verdict, gs.VERDICT_LABELS)
            self.assertNotIn(g.name.lower(), gs.NEVER_REPORT)
            self.assertTrue(gs._is_current_user(g.username))
            self.assertGreaterEqual(g.age_seconds, 1800)
            self.assertEqual(g.instance_key, f"{g.pid}:{g.create_time:.0f}")

    def test_sorted_worst_first(self) -> None:
        import ghost_sweeper as gs

        order = {"STUCK": 0, "LEAKING": 1, "FINISHED": 2, "WORKING": 3}
        sw = gs.GhostSweeper(idle_seconds=1800)
        ranks = [order[g.verdict] for g in sw.scan()]
        self.assertEqual(ranks, sorted(ranks))

    def test_second_scan_does_not_shrink_reported_ages(self) -> None:
        """Guards the bug where a ghost reported '35 minutes idle' on one scan
        reported '20 seconds idle' on the next."""
        import ghost_sweeper as gs

        sw = gs.GhostSweeper(idle_seconds=1800)
        first = {g.instance_key: g for g in sw.scan()}
        second = {g.instance_key: g for g in sw.scan()}
        for key, later in second.items():
            earlier = first.get(key)
            if earlier is None:
                continue
            self.assertGreaterEqual(
                later.idle_for_seconds, earlier.idle_for_seconds - 1.0,
                f"{later.name} idle age went backwards")
            if (earlier.orphaned_for_seconds is not None
                    and later.orphaned_for_seconds is not None):
                self.assertGreaterEqual(
                    later.orphaned_for_seconds,
                    earlier.orphaned_for_seconds - 1.0,
                    f"{later.name} orphan age went backwards")


class TestSensorRefresher(unittest.TestCase):
    """temperature_readings must never block its caller."""

    def test_public_readers_do_not_call_the_probes_inline(self) -> None:
        from unittest.mock import patch

        import temperature_readings as tr

        def explode():
            raise AssertionError("probe chain called on the caller's thread")

        with patch.object(tr._cpu_slot, "_reader", explode), \
             patch.object(tr._gpu_slot, "_reader", explode):
            tr.read_primary_temp_celsius()
            tr.read_gpu_temp_celsius()
            tr.read_gpu_memory_mib()

    def test_failing_sensor_backs_off_but_stays_bounded(self) -> None:
        import temperature_readings as tr

        slot = tr._SensorSlot("test", ttl=8.0, reader=lambda: None)
        self.assertEqual(slot.effective_ttl(), 8.0)
        for _ in range(tr._FAILURES_BEFORE_BACKOFF):
            slot.refresh()
        self.assertGreater(slot.effective_ttl(), 8.0)
        for _ in range(50):
            slot.refresh()
        self.assertLessEqual(slot.effective_ttl(), tr._MAX_BACKOFF_SEC)

    def test_success_resets_backoff_and_publishes_value(self) -> None:
        import temperature_readings as tr

        results = [None, None, None, None, 42.0]
        slot = tr._SensorSlot("test", ttl=8.0, reader=lambda: results.pop(0))
        for _ in range(4):
            slot.refresh()
        self.assertGreater(slot.effective_ttl(), 8.0)
        slot.refresh()
        self.assertEqual(slot.value, 42.0)
        self.assertEqual(slot.effective_ttl(), 8.0)

    def test_raising_reader_is_a_failure_not_a_crash(self) -> None:
        import temperature_readings as tr

        def boom():
            raise RuntimeError("nvidia-smi exploded")

        slot = tr._SensorSlot("test", ttl=8.0, reader=boom)
        slot.refresh()                      # must not propagate
        self.assertIsNone(slot.value)
        self.assertEqual(slot._failures, 1)

    def test_stale_only_after_ttl_elapses(self) -> None:
        import temperature_readings as tr

        slot = tr._SensorSlot("test", ttl=8.0, reader=lambda: 1.0)
        self.assertTrue(slot.is_stale(time.monotonic()))
        slot.refresh()
        self.assertFalse(slot.is_stale(time.monotonic()))
        self.assertTrue(slot.is_stale(time.monotonic() + 9.0))


class TestCsvAppendFastPath(unittest.TestCase):
    """The memoised header check must stay correct, not just fast."""

    def _append(self, path: Path, cpu: float = 1.0) -> None:
        append_metrics_row(
            path, unix_time=time.time(), cpu_percent=cpu, ram_percent=2.0,
            disk_percent=None, swap_percent=None, temp_celsius=None,
        )

    def test_header_written_once_and_rows_accumulate(self) -> None:
        import metric_history

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.csv"
            metric_history.invalidate_csv_caches()
            for i in range(5):
                self._append(path, cpu=float(i))
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 6)          # 1 header + 5 rows
            self.assertEqual(lines[0], ",".join(CSV_FIELDNAMES))

    def test_external_truncation_is_detected_and_header_rewritten(self) -> None:
        import metric_history

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.csv"
            metric_history.invalidate_csv_caches()
            self._append(path)
            self._append(path)
            # Something outside this process replaced the file with a smaller
            # one — the memoised "header is fine" verdict must not survive.
            path.write_text("", encoding="utf-8")
            self._append(path)
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(lines[0], ",".join(CSV_FIELDNAMES))
            self.assertEqual(len(lines), 2)

    def test_legacy_schema_is_still_archived(self) -> None:
        import metric_history

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.csv"
            metric_history.invalidate_csv_caches()
            path.write_text("old,header\n1,2\n", encoding="utf-8")
            self._append(path)
            archived = list(Path(tmp).glob("metrics-schema-legacy-*.csv"))
            self.assertEqual(len(archived), 1)
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(lines[0], ",".join(CSV_FIELDNAMES))


if __name__ == "__main__":
    unittest.main()
