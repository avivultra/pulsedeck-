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
import dependencies  # noqa: E402
import monitor  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
