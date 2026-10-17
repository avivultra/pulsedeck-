"""Tests for what PulseDeck.exe needs: where its files live, how it starts,
one copy at a time, and the Start-with-Windows switch.

Nothing here touches the real registry, creates a real mutex or starts
PowerShell: winreg, kernel32 and subprocess are fakes.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import i18n

i18n.set_language("he")

import app_paths  # noqa: E402
import autostart  # noqa: E402
import dependencies  # noqa: E402
import monitor  # noqa: E402
import single_instance  # noqa: E402


class DataDirTest(unittest.TestCase):
    def _data_dir(self, *, frozen: bool, env: dict[str, str]) -> Path:
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(sys, "frozen", frozen, create=True):
            if "PULSEDECK_DATA_DIR" not in env:
                os.environ.pop("PULSEDECK_DATA_DIR", None)
            return app_paths._data_dir()

    def test_from_source_files_stay_next_to_the_code(self):
        self.assertEqual(self._data_dir(frozen=False, env={}), app_paths.CODE_DIR)

    def test_the_exe_keeps_its_files_in_local_app_data(self):
        got = self._data_dir(frozen=True, env={"LOCALAPPDATA": "C:\\Users\\x\\AppData\\Local"})
        self.assertEqual(got, Path("C:\\Users\\x\\AppData\\Local") / "PulseDeck")

    def test_an_override_wins_both_ways(self):
        for frozen in (False, True):
            self.assertEqual(self._data_dir(frozen=frozen, env={"PULSEDECK_DATA_DIR": "D:\\try"}),
                             Path("D:\\try"))

    def test_every_writer_uses_the_data_folder(self):
        import ai_advisor
        import config
        import load_actions
        import metric_history
        import pulsedeck_mcp
        self.assertEqual(config.config_path(), app_paths.DATA_DIR / "config.json")
        self.assertEqual(ai_advisor.ENV_PATH, app_paths.DATA_DIR / ".env")
        self.assertEqual(ai_advisor.USAGE_PATH.parent, app_paths.HISTORY_DIR)
        self.assertEqual(load_actions.STATE_PATH.parent, app_paths.HISTORY_DIR)
        self.assertEqual(metric_history.DEFAULT_HISTORY_DIR, app_paths.HISTORY_DIR)
        self.assertEqual(pulsedeck_mcp.CONFIG_DIR, app_paths.DATA_DIR)


class StartModeTest(unittest.TestCase):
    def test_a_double_clicked_exe_opens_the_strip(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(monitor.default_argv([]), ["--dock", "--history", "--tray"])
            self.assertEqual(monitor.default_argv(["--once"]), ["--once"])

    def test_from_source_an_empty_command_line_is_still_the_console(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(monitor.default_argv([]), [])

    def test_history_mode_needs_no_matplotlib(self):
        with mock.patch.object(dependencies, "_try_import", side_effect=lambda m: m != "matplotlib"):
            self.assertEqual(dependencies.check_features({"history"}), [])

    def test_the_exe_names_the_folder_for_the_key(self):
        import ai_advisor
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(ai_advisor, "ENV_PATH", Path("C:\\Users\\x\\AppData\\Local\\PulseDeck\\.env")):
            text = ai_advisor.missing_key_text()
        self.assertIn("C:\\Users\\x\\AppData\\Local\\PulseDeck", text)
        self.assertNotIn("monitor.py", text)


class _FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.store: dict[tuple[str, str], object] = {}

    class _Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def OpenKey(self, _root, path, _reserved=0, _access=0):
        return self._Key(path)

    def CreateKeyEx(self, _root, path, _reserved=0, _access=0):
        return self._Key(path)

    def QueryValueEx(self, key, name):
        try:
            return self.store[(key.path, name)], self.REG_SZ
        except KeyError:
            raise FileNotFoundError(name) from None

    def SetValueEx(self, key, name, _reserved, _type, value):
        self.store[(key.path, name)] = value

    def DeleteValue(self, key, name):
        if self.store.pop((key.path, name), None) is None:
            raise FileNotFoundError(name)


class AutostartTest(unittest.TestCase):
    def setUp(self):
        self.reg = _FakeWinreg()
        patches = [mock.patch.dict(sys.modules, {"winreg": self.reg}),
                   mock.patch.object(sys, "platform", "win32")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_switching_on_and_off(self):
        self.assertFalse(autostart.is_enabled())
        autostart.set_enabled(True)
        self.assertTrue(autostart.is_enabled())
        self.assertEqual(self.reg.store[(autostart.RUN_KEY, "PulseDeck")], autostart.command())
        autostart.set_enabled(False)
        self.assertFalse(autostart.is_enabled())
        self.assertEqual(self.reg.store, {})

    def test_off_in_task_manager_reads_as_off_and_on_clears_it(self):
        autostart.set_enabled(True)
        self.reg.store[(autostart.APPROVED_KEY, "PulseDeck")] = b"\x03" + bytes(11)
        self.assertFalse(autostart.is_enabled())
        self.reg.store[(autostart.APPROVED_KEY, "PulseDeck")] = b"\x02" + bytes(11)
        self.assertTrue(autostart.is_enabled())
        self.reg.store[(autostart.APPROVED_KEY, "PulseDeck")] = b"\x03" + bytes(11)
        autostart.set_enabled(True)
        self.assertTrue(autostart.is_enabled())

    def test_the_command_is_this_same_pulsedeck(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", "C:\\Apps\\PulseDeck\\PulseDeck.exe"):
            self.assertEqual(autostart.command(), '"C:\\Apps\\PulseDeck\\PulseDeck.exe"')
        with mock.patch.object(sys, "frozen", False, create=True):
            cmd = autostart.command()
        self.assertIn(str(app_paths.CODE_DIR / "monitor.py"), cmd)
        self.assertTrue(cmd.endswith("--dock --history --tray"))


@unittest.skipUnless(os.name == "nt", "the named mutex is Windows-only")
class SingleInstanceTest(unittest.TestCase):
    def _acquire(self, last_error: int, handle: int = 42) -> tuple[bool, mock.Mock]:
        kernel32 = mock.Mock()
        kernel32.CreateMutexW.return_value = handle
        with mock.patch("ctypes.WinDLL", return_value=kernel32), \
             mock.patch("ctypes.get_last_error", return_value=last_error), \
             mock.patch.object(single_instance, "_handle", None):
            return single_instance.acquire(), kernel32

    def test_the_first_copy_runs(self):
        ok, kernel32 = self._acquire(0)
        self.assertTrue(ok)
        kernel32.CloseHandle.assert_not_called()

    def test_a_second_copy_steps_aside(self):
        ok, kernel32 = self._acquire(single_instance.ERROR_ALREADY_EXISTS)
        self.assertFalse(ok)
        kernel32.CloseHandle.assert_called_once_with(42)

    def test_a_failed_check_never_blocks_the_start(self):
        ok, _ = self._acquire(5, handle=0)
        self.assertTrue(ok)


class AlreadyRunningTextTest(unittest.TestCase):
    def test_the_hebrew_notice_opens_with_a_hebrew_letter(self):
        # Its first letter sets the line's direction on Windows.
        text = single_instance.already_running_text()
        self.assertTrue("֐" <= text[0] <= "׿", text)
        self.assertIn("PulseDeck", text)


@unittest.skipUnless(os.name == "nt", "a .lnk is a Windows shortcut")
class ShortcutTest(unittest.TestCase):
    def test_the_exe_shortcut_points_at_the_exe(self):
        import install_shortcut
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", "C:\\Apps\\PulseDeck\\PulseDeck.exe"), \
             mock.patch.object(install_shortcut, "_desktop_dir", return_value=Path(os.devnull).parent), \
             mock.patch("subprocess.run", return_value=done) as run:
            install_shortcut._windows_install()
        script = run.call_args[0][0][-1]
        self.assertIn("$lnk.TargetPath = 'C:\\Apps\\PulseDeck\\PulseDeck.exe'", script)
        self.assertIn("$lnk.Arguments = ''", script)
        self.assertIn("$lnk.WorkingDirectory = 'C:\\Apps\\PulseDeck'", script)


class SourceTest(unittest.TestCase):
    def test_every_file_compiles_on_this_python(self):
        # CI runs 3.11-3.13. A module no test imports used to slip through:
        # install_shortcut.py had 3.12-only f-string syntax from May to October.
        for path in sorted(app_paths.CODE_DIR.glob("*.py")):
            with self.subTest(path.name):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")


if __name__ == "__main__":
    unittest.main()
