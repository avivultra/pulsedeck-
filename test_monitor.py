"""Unit tests for pure helpers in monitor.py (stdlib unittest, no extra deps)."""

import csv
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config as app_config
import dependencies
import monitor
from metric_history import (
    ARCHIVE_PREFIX,
    CSV_FIELDNAMES,
    append_metrics_row,
    iter_history_rows,
    prune_old_archives,
    render_combined_chart,
    render_history_chart,
    rotate_history,
)


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


class TestConfig(unittest.TestCase):
    def test_creates_defaults_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = app_config.load_config(Path(d))
            self.assertEqual(cfg["rotation"]["weeks_to_keep"], 12)
            self.assertTrue((Path(d) / "config.json").exists())

    def test_partial_file_filled_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.json").write_text(
                json.dumps({"ui": {"dock": True}}), encoding="utf-8"
            )
            cfg = app_config.load_config(Path(d))
            self.assertTrue(cfg["ui"]["dock"])
            self.assertFalse(cfg["ui"]["tray"])  # default preserved
            self.assertEqual(cfg["rotation"]["weeks_to_keep"], 12)

    def test_save_then_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = app_config.load_config(Path(d))
            cfg["disk_path"] = "E:\\"
            app_config.save_config(cfg, Path(d))
            reloaded = app_config.load_config(Path(d))
            self.assertEqual(reloaded["disk_path"], "E:\\")


class TestRotation(unittest.TestCase):
    def _write_main(self, history_dir: Path, rows: list[dict]) -> Path:
        main = history_dir / "metrics.csv"
        with main.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            w.writeheader()
            w.writerows(rows)
        return main

    def test_rotates_old_rows_into_archive(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            hd = Path(d)
            now = time.time()
            rows = [
                {"timestamp_iso": "x", "unix_time": f"{now - 30*86400 + i:.3f}",
                 "cpu_percent": "1", "ram_percent": "2", "disk_percent": "",
                 "swap_percent": "", "temp_celsius": ""}
                for i in range(3)
            ] + [
                {"timestamp_iso": "x", "unix_time": f"{now - 60 + i:.3f}",
                 "cpu_percent": "3", "ram_percent": "4", "disk_percent": "",
                 "swap_percent": "", "temp_celsius": ""}
                for i in range(2)
            ]
            self._write_main(hd, rows)
            stats = rotate_history(hd, weeks_to_keep=12)
            self.assertEqual(stats["moved"], 3)
            self.assertEqual(stats["kept"], 2)
            self.assertEqual(len(list(hd.glob(f"{ARCHIVE_PREFIX}*.csv"))), 1)

    def test_no_rotation_when_all_rows_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            hd = Path(d)
            now = time.time()
            self._write_main(hd, [
                {"timestamp_iso": "x", "unix_time": f"{now:.3f}",
                 "cpu_percent": "1", "ram_percent": "2", "disk_percent": "",
                 "swap_percent": "", "temp_celsius": ""}
            ])
            stats = rotate_history(hd, weeks_to_keep=12)
            self.assertEqual(stats["moved"], 0)

    def test_prune_deletes_only_old_archives(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            hd = Path(d)
            now = datetime.now(timezone.utc)
            for w in range(15):
                date = now - timedelta(weeks=w)
                y, wk, _ = date.isocalendar()
                (hd / f"{ARCHIVE_PREFIX}{y:04d}-{wk:02d}.csv").write_text("h\n", encoding="utf-8")
            deleted = prune_old_archives(hd, weeks_to_keep=12)
            self.assertEqual(deleted, 3)
            self.assertEqual(len(list(hd.glob(f"{ARCHIVE_PREFIX}*.csv"))), 12)


class TestCombinedHistory(unittest.TestCase):
    def test_iter_merges_main_and_archive_in_time_order(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            hd = Path(d)
            # Old archive row
            (hd / f"{ARCHIVE_PREFIX}2024-01.csv").write_text(
                "timestamp_iso,unix_time,cpu_percent,ram_percent,disk_percent,swap_percent,temp_celsius\n"
                "old,100.0,1,2,,,\n",
                encoding="utf-8",
            )
            # Main CSV with newer rows
            main = hd / "metrics.csv"
            with main.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
                w.writeheader()
                w.writerow({"timestamp_iso": "new", "unix_time": "200.0",
                            "cpu_percent": "3", "ram_percent": "4",
                            "disk_percent": "", "swap_percent": "", "temp_celsius": ""})

            rows_main_only = iter_history_rows(hd, include_archive=False)
            rows_combined = iter_history_rows(hd, include_archive=True)
            self.assertEqual(len(rows_main_only), 1)
            self.assertEqual(len(rows_combined), 2)
            self.assertEqual(rows_combined[0]["unix_time"], "100.0")
            self.assertEqual(rows_combined[1]["unix_time"], "200.0")

    def test_combined_chart_renders_with_two_rows(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            hd = Path(d)
            now = time.time()
            for i, ts in enumerate((now - 86400, now)):
                append_metrics_row(
                    hd / "metrics.csv",
                    unix_time=ts, cpu_percent=10.0 + i, ram_percent=20.0,
                    disk_percent=None, swap_percent=None, temp_celsius=None,
                )
            png = hd / "out.png"
            self.assertTrue(render_combined_chart(hd, png, include_archive=True))
            self.assertGreater(png.stat().st_size, 100)


class TestDependencyCheck(unittest.TestCase):
    def test_no_features_returns_empty_when_psutil_present(self) -> None:
        # psutil is required by the project itself, so it must be importable here.
        missing = dependencies.check_features(set())
        self.assertEqual(missing, [])

    def test_missing_module_is_reported(self) -> None:
        original = dependencies.FEATURE_REQUIREMENTS
        try:
            dependencies.FEATURE_REQUIREMENTS = {
                "fake": (dependencies.Requirement("fake", "definitely_not_installed_xyz",
                                                  "definitely-not-installed-xyz"),),
            }
            missing = dependencies.check_features({"fake"})
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0].pip_name, "definitely-not-installed-xyz")
            msg = dependencies.format_missing(missing)
            self.assertIn("pip install", msg)
            self.assertIn("definitely-not-installed-xyz", msg)
        finally:
            dependencies.FEATURE_REQUIREMENTS = original

    def test_format_missing_empty_returns_empty_string(self) -> None:
        self.assertEqual(dependencies.format_missing([]), "")


class TestAlertGuards(unittest.TestCase):
    def test_protected_names_blocks_kill(self) -> None:
        import alerts
        self.assertTrue(alerts._is_protected(99999, "svchost.exe"))
        self.assertTrue(alerts._is_protected(99999, "SERVICES.exe"))  # case-insensitive
        self.assertTrue(alerts._is_protected(99999, "System"))
        self.assertFalse(alerts._is_protected(99999, "python.exe"))
        self.assertFalse(alerts._is_protected(99999, "notepad.exe"))

    def test_dispatcher_cooldown_gates_second_event(self) -> None:
        import alerts as alerts_mod
        d = alerts_mod.AlertDispatcher(cooldown_seconds=300)
        # We can't actually pop a Tk window in tests; just check the gate.
        self.assertTrue(d._gate())
        self.assertFalse(d._gate())

    def test_dispatcher_zero_cooldown_always_fires(self) -> None:
        import alerts as alerts_mod
        d = alerts_mod.AlertDispatcher(cooldown_seconds=0)
        self.assertTrue(d._gate())
        # Zero cooldown means subsequent gate calls also pass (well, last_fired==now → diff=0, not <0, so True)
        self.assertTrue(d._gate())


class TestProcessSampler(unittest.TestCase):
    def test_top_returns_processes(self) -> None:
        import process_monitor
        top_cpu, top_rss = process_monitor.sample_now(refresh_wait=0.5)
        self.assertGreater(len(top_rss), 0)
        # All entries should have positive RSS
        for p in top_rss:
            self.assertGreater(p.rss_bytes, 0)
            self.assertTrue(p.name)


class TestSpikeDetect(unittest.TestCase):
    def test_no_spike_on_steady(self) -> None:
        from spike_reporter import detect_spike
        from monitor import Snapshot

        def snap(cpu: float, ram: float) -> Snapshot:
            return Snapshot(cpu_percent=cpu, ram_percent=ram, ram_used=0, ram_total=1,
                            disk_path="C:\\", disk_percent=None, disk_used=None, disk_total=None,
                            swap_percent=None, uptime_sec=0,
                            battery_percent=None, battery_plugged=None,
                            cpu_logical=8, temp_celsius=None)
        is_spike, _, _ = detect_spike(snap(20, 50), snap(22, 51))
        self.assertFalse(is_spike)

    def test_spike_on_cpu_jump(self) -> None:
        from spike_reporter import detect_spike
        from monitor import Snapshot
        def snap(cpu: float, ram: float) -> Snapshot:
            return Snapshot(cpu_percent=cpu, ram_percent=ram, ram_used=0, ram_total=1,
                            disk_path="C:\\", disk_percent=None, disk_used=None, disk_total=None,
                            swap_percent=None, uptime_sec=0,
                            battery_percent=None, battery_plugged=None,
                            cpu_logical=8, temp_celsius=None)
        is_spike, reason, trigger = detect_spike(snap(20, 50), snap(60, 51))
        self.assertTrue(is_spike)
        self.assertEqual(trigger, "cpu")


class TestAlertFormatters(unittest.TestCase):
    def test_format_relative_he_active(self) -> None:
        from alerts import format_relative_he
        self.assertEqual(format_relative_he(0), "פעיל עכשיו")
        self.assertEqual(format_relative_he(3), "פעיל עכשיו")
        self.assertIn("שניות", format_relative_he(30))
        self.assertIn("דקות", format_relative_he(300))
        self.assertIn("שעות", format_relative_he(7200))
        self.assertIn("ימים", format_relative_he(200_000))
        self.assertEqual(format_relative_he(None), "ברקע / לא נצפתה פעילות")

    def test_format_uptime_he(self) -> None:
        from alerts import format_uptime_he
        self.assertEqual(format_uptime_he(45), "45s")
        self.assertEqual(format_uptime_he(120), "2m")
        self.assertEqual(format_uptime_he(3600), "1h")
        self.assertEqual(format_uptime_he(3700), "1h 1m")
        self.assertEqual(format_uptime_he(86400), "1d")

    def test_self_pid_is_protected(self) -> None:
        import os, alerts
        self.assertTrue(alerts._is_protected(os.getpid(), "anything.exe"))


class TestJanitor(unittest.TestCase):
    """Tests for the conhost-zombie scanner."""

    def _mock_proc(self, pid: int, name: str, ppid: int = 0, rss: int = 0):
        """Build a fake psutil.Process-like object for process_iter."""
        from unittest.mock import MagicMock
        p = MagicMock()
        p.info = {
            "pid": pid, "name": name, "ppid": ppid,
            "memory_info": MagicMock(rss=rss),
        }
        return p

    def _patched_iter(self, processes):
        """Return a context manager patching psutil.process_iter + Process()."""
        from unittest.mock import patch, MagicMock
        # Build pid -> name lookup for psutil.Process(ppid).name() resolution
        names = {p.info["pid"]: p.info["name"] for p in processes}

        def _fake_process(pid):
            if pid not in names:
                import psutil
                raise psutil.NoSuchProcess(pid)
            m = MagicMock()
            m.name.return_value = names[pid]
            return m

        return (patch("janitor.psutil.process_iter", return_value=processes),
                patch("janitor.psutil.Process", side_effect=_fake_process))

    def test_groups_25_conhost_under_claude(self) -> None:
        import janitor
        # parent claude.exe with PID 100, plus 25 conhost children
        procs = [self._mock_proc(100, "claude.exe", ppid=1)]
        for i in range(25):
            procs.append(self._mock_proc(200 + i, "conhost.exe",
                                         ppid=100, rss=5_000_000))
        scanner = janitor.JanitorScanner(conhost_threshold=20)
        p1, p2 = self._patched_iter(procs)
        with p1, p2:
            groups = scanner.scan()
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g.parent_name, "claude.exe")
        self.assertEqual(g.parent_pid, 100)
        self.assertEqual(g.count, 25)
        self.assertEqual(g.total_rss_bytes, 25 * 5_000_000)

    def test_below_threshold_returns_empty(self) -> None:
        import janitor
        procs = [self._mock_proc(100, "claude.exe", ppid=1)]
        for i in range(19):
            procs.append(self._mock_proc(200 + i, "conhost.exe", ppid=100))
        scanner = janitor.JanitorScanner(conhost_threshold=20)
        p1, p2 = self._patched_iter(procs)
        with p1, p2:
            groups = scanner.scan()
        self.assertEqual(groups, [])

    def test_self_pid_parent_is_excluded(self) -> None:
        import os, janitor
        my_pid = os.getpid()
        # 25 conhost children of THIS process — must be ignored
        procs = []
        for i in range(25):
            procs.append(self._mock_proc(900 + i, "conhost.exe", ppid=my_pid))
        scanner = janitor.JanitorScanner(conhost_threshold=20)
        p1, p2 = self._patched_iter(procs)
        with p1, p2:
            groups = scanner.scan()
        self.assertEqual(groups, [])

    def test_unsuspicious_parent_excluded(self) -> None:
        import janitor
        # 30 conhost children of an "innocent" parent (not in suspicious list)
        procs = [self._mock_proc(100, "explorer.exe", ppid=1)]
        for i in range(30):
            procs.append(self._mock_proc(200 + i, "conhost.exe", ppid=100))
        scanner = janitor.JanitorScanner(conhost_threshold=20)
        p1, p2 = self._patched_iter(procs)
        with p1, p2:
            groups = scanner.scan()
        self.assertEqual(groups, [])

    def test_count_total_zombies_returns_sum(self) -> None:
        import janitor
        # Two parents each with 22 zombies → total 44
        procs = [
            self._mock_proc(100, "claude.exe", ppid=1),
            self._mock_proc(101, "node.exe", ppid=1),
        ]
        for i in range(22):
            procs.append(self._mock_proc(200 + i, "conhost.exe", ppid=100, rss=1000))
        for i in range(22):
            procs.append(self._mock_proc(300 + i, "conhost.exe", ppid=101, rss=2000))
        scanner = janitor.JanitorScanner(conhost_threshold=20)
        p1, p2 = self._patched_iter(procs)
        with p1, p2:
            scanner._do_scan()
        self.assertEqual(scanner.count_total_zombies(), 44)
        self.assertEqual(len(scanner.get_groups()), 2)


if __name__ == "__main__":
    unittest.main()
