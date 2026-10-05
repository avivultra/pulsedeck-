"""Tests for the never-clean-all rules: protected server folders, parts of an
open app, Claude Code conversations — and for reading a conversation title
without reading (or leaking) anything else from its transcript.

Every process here is fake. Nothing is scanned, sampled or closed for real.
"""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import i18n

i18n.set_language("he")

import claude_sessions as cs
import ghost_sweeper as gs
import idle_cleanup
from idle_cleanup import MIB
from process_monitor import ProcessInfo

DIRS = gs.normalize_dirs([r"E:\tools\muse-bridge", "E:/flow-hub/SecurityGuard/"])
SID = "a3797e73-0523-4774-befd-e8e4085847b9"
CODE_CMD = (r"C:\Users\x\AppData\Roaming\Claude\claude-code\2.1\claude.exe "
            f"--output-format stream-json --resume {SID}")
APP_CMD = (r"C:\Program Files\WindowsApps\Claude_2\app\Claude.exe --type=utility "
           r"--utility-sub-type=node.mojom.NodeService --lang=he")


def _ghost(**overrides):
    fields = dict(
        pid=1234, name="python.exe", exe="C:/py/python.exe",
        cmdline="python bridge.py", cwd=r"E:\tools\muse-bridge", username="aviv",
        create_time=1000.0, age_seconds=7200.0,
        parent_pid=99, parent_name="cmd.exe", parent_alive=False,
        orphaned_for_seconds=3600.0, orphan_time_is_lower_bound=False,
        cpu_seconds_total=2.0, cpu_percent_window=0.0,
        cpu_percent_lifetime=0.0, idle_for_seconds=7200.0,
        idle_time_is_lower_bound=False, rss_bytes=30 * MIB,
        rss_delta_bytes=0, num_threads=4, listening_ports=(8772,),
        established_connections=0, reasons=("ORPHAN", "DORMANT"),
        verdict="FINISHED", verdict_detail="…", observed_seconds=1200.0,
        first_scan=False,
    )
    fields.update(overrides)
    return gs.GhostProcess(**fields)


class ProtectedFolderTest(unittest.TestCase):
    def test_working_folder_inside_a_protected_folder(self):
        self.assertEqual(gs.protected_dir_of(r"E:\tools\muse-bridge", "python bridge.py", DIRS),
                         r"e:\tools\muse-bridge")
        self.assertTrue(gs.protected_dir_of(r"e:/TOOLS/muse-bridge/sub", "", DIRS))

    def test_script_path_on_the_command_line_when_folder_is_unreadable(self):
        # SecurityGuard's watchdog runs elevated: its cwd reads as "".
        cmd = r"C:\py\pythonw.exe E:\flow-hub\SecurityGuard\watchdog_guard.pyw"
        self.assertEqual(gs.protected_dir_of("", cmd, DIRS), r"e:\flow-hub\securityguard")
        quoted = r'"C:\py\python.exe" "E:\flow-hub\SecurityGuard\main.py"'
        self.assertTrue(gs.protected_dir_of("", quoted, DIRS))

    def test_sibling_folder_sharing_a_prefix_is_not_protected(self):
        self.assertIsNone(gs.protected_dir_of(r"E:\tools\muse-bridge2", "python x.py", DIRS))
        self.assertIsNone(gs.protected_dir_of("", r"python E:\flow-hub\SecurityGuardX\a.py", DIRS))

    def test_no_protected_folders_configured(self):
        self.assertIsNone(gs.protected_dir_of(r"E:\tools\muse-bridge", "x", ()))
        self.assertEqual(gs.normalize_dirs(["", None, "  "]), ())


class RoleTest(unittest.TestCase):
    def test_server_wins_over_everything(self):
        self.assertEqual(gs.role_of("python.exe", "", "python bridge.py",
                                    r"E:\tools\muse-bridge", False, "?", DIRS),
                         gs.ROLE_SERVER)

    def test_helper_of_an_open_app(self):
        self.assertEqual(gs.role_of("claude.exe", "", APP_CMD, r"C:\WINDOWS\system32",
                                    True, "claude.exe", DIRS), gs.ROLE_APP_PART)

    def test_helper_whose_app_is_gone_is_an_ordinary_ghost(self):
        self.assertEqual(gs.role_of("claude.exe", "", APP_CMD, "", False,
                                    "claude.exe", DIRS), "")

    def test_claude_code_conversation(self):
        self.assertEqual(gs.role_of("claude.exe", "", CODE_CMD, r"E:\guides",
                                    True, "claude.exe", DIRS), gs.ROLE_CLAUDE)

    def test_plain_node_server_has_no_role(self):
        self.assertEqual(gs.role_of("node.exe", "", "node server.js", r"E:\proj",
                                    False, "?", DIRS), "")

    def test_only_role_less_finished_ghosts_are_clean_all_material(self):
        self.assertTrue(_ghost(role="").offer_in_clean_all)
        for role in (gs.ROLE_SERVER, gs.ROLE_APP_PART, gs.ROLE_CLAUDE):
            self.assertFalse(_ghost(role=role).offer_in_clean_all)
        self.assertFalse(_ghost(verdict="WORKING").offer_in_clean_all)


class CleanAllTargetsTest(unittest.TestCase):
    def test_ghosts_with_a_role_are_never_offered(self):
        ghosts = [_ghost(pid=1, role=gs.ROLE_SERVER),
                  _ghost(pid=2, role=gs.ROLE_APP_PART),
                  _ghost(pid=3, role=gs.ROLE_CLAUDE),
                  _ghost(pid=4, role="", cwd=r"E:\proj", cmdline="node old.js")]
        with mock.patch.object(idle_cleanup, "visible_window_pids", return_value=set()):
            targets = idle_cleanup.ghost_targets(ghosts)
        self.assertEqual([t.pid for t in targets], [4])
        self.assertEqual((targets[0].cwd, targets[0].cmdline), (r"E:\proj", "node old.js"))

    def test_idle_hog_from_a_protected_folder_is_not_offered(self):
        snap = [ProcessInfo(pid=10, name="bridge-host.exe", cpu_percent=0, cpu_percent_raw=0,
                            rss_bytes=300 * MIB, process_uptime_seconds=9000,
                            last_active_seconds_ago=9000, observed_seconds=9000),
                ProcessInfo(pid=11, name="tray.exe", cpu_percent=0, cpu_percent_raw=0,
                            rss_bytes=300 * MIB, process_uptime_seconds=9000,
                            last_active_seconds_ago=9000, observed_seconds=9000)]
        procs = {
            10: dict(name="bridge-host.exe", cwd=r"E:\tools\muse-bridge", cmd=["bridge-host.exe"]),
            11: dict(name="tray.exe", cwd=r"C:\Apps\Tray", cmd=["tray.exe", "--quiet"]),
        }

        class _P:
            def __init__(self, pid):
                self.pid, self._d = pid, procs[pid]

            def name(self): return self._d["name"]
            def parent(self): return None
            def children(self, recursive=False): return []
            def memory_info(self): return SimpleNamespace(rss=300 * MIB)
            def username(self): return "PC\\aviv"
            def exe(self): return r"C:\Apps\x.exe"
            def create_time(self): return 1000.0
            def cwd(self): return self._d["cwd"]
            def cmdline(self): return self._d["cmd"]

        sampler = mock.Mock()
        sampler.snapshot.return_value = snap
        with mock.patch.object(idle_cleanup.psutil, "Process", side_effect=_P), \
             mock.patch.object(idle_cleanup, "visible_window_pids", return_value=set()), \
             mock.patch.object(idle_cleanup, "_network_pids", return_value=set()), \
             mock.patch.object(idle_cleanup, "_self_and_ancestors", return_value={99999}), \
             mock.patch.object(idle_cleanup, "_ancestor_pids", return_value=[]), \
             mock.patch.object(idle_cleanup, "_windows_dir", return_value="c:\\windows\\"), \
             mock.patch("ghost_sweeper._is_current_user", return_value=True):
            targets, _note = idle_cleanup.idle_hog_targets(
                sampler, min_idle=3600, protected_dirs=DIRS)
        self.assertEqual([t.pid for t in targets], [11])
        self.assertEqual(targets[0].cwd, r"C:\Apps\Tray")
        self.assertEqual(targets[0].cmdline, "tray.exe --quiet")

    def test_idle_threshold_is_respected(self):
        snap = [ProcessInfo(pid=11, name="tray.exe", cpu_percent=0, cpu_percent_raw=0,
                            rss_bytes=300 * MIB, process_uptime_seconds=9000,
                            last_active_seconds_ago=1800, observed_seconds=9000)]
        sampler = mock.Mock()
        sampler.snapshot.return_value = snap
        with mock.patch.object(idle_cleanup, "visible_window_pids", return_value=set()), \
             mock.patch.object(idle_cleanup, "_network_pids", return_value=set()), \
             mock.patch.object(idle_cleanup, "_self_and_ancestors", return_value={99999}):
            targets, _ = idle_cleanup.idle_hog_targets(sampler, min_idle=3600)
        self.assertEqual(targets, [])     # 30 min quiet < 60 min threshold


class SessionTitleTest(unittest.TestCase):
    SECRET = "THE CONVERSATION BODY MUST NEVER LEAVE THE FILE"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cs._cache.clear()
        self.patch = mock.patch.object(cs, "projects_dir", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        cs._cache.clear()
        self.tmp.cleanup()

    def _write(self, records, *, sid=SID, project="E--guides"):
        folder = self.root / project
        folder.mkdir(exist_ok=True)
        with (folder / f"{sid}.jsonl").open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_newest_custom_title_wins(self):
        self._write([{"type": "custom-title", "customTitle": "ישן"},
                     {"type": "user", "message": self.SECRET},
                     {"type": "ai-title", "aiTitle": "Generated"},
                     {"type": "custom-title", "customTitle": "יועץ FLOWSTREAM"},
                     {"type": "assistant", "message": self.SECRET}])
        self.assertEqual(cs.session_title(CODE_CMD), "יועץ FLOWSTREAM")

    def test_generated_title_when_there_is_no_custom_one(self):
        self._write([{"type": "user", "message": self.SECRET},
                     {"type": "ai-title", "aiTitle": "Review the dock"}])
        self.assertEqual(cs.session_title(CODE_CMD), "Review the dock")

    def test_no_title_record_means_none(self):
        self._write([{"type": "user", "message": self.SECRET}])
        self.assertIsNone(cs.session_title(CODE_CMD))

    def test_missing_transcript_or_session_flag_means_none(self):
        self.assertIsNone(cs.session_title(CODE_CMD))
        self.assertIsNone(cs.session_title("claude.exe --output-format stream-json"))

    def test_a_message_that_mentions_the_title_key_is_not_a_title(self):
        # A user message quoting '"custom-title"' must not be taken as one.
        self._write([{"type": "user", "message": 'see "custom-title" ' + self.SECRET}])
        self.assertIsNone(cs.session_title(CODE_CMD))

    def test_title_spanning_block_boundaries_is_found(self):
        filler = [{"type": "user", "message": "x" * 5000} for _ in range(40)]
        self._write([{"type": "custom-title", "customTitle": "רחוק"}] + filler)
        self.assertEqual(cs.session_title(CODE_CMD), "רחוק")

    def test_title_is_cleaned_and_capped(self):
        self._write([{"type": "custom-title", "customTitle": "a\nb\t" + "z" * 200}])
        title = cs.session_title(CODE_CMD)
        self.assertNotIn("\n", title)
        self.assertLessEqual(len(title), cs.MAX_TITLE_CHARS)

    def test_only_the_title_string_comes_back(self):
        self._write([{"type": "custom-title", "customTitle": "שם", "sessionId": SID},
                     {"type": "user", "message": self.SECRET}])
        title = cs.session_title(CODE_CMD)
        self.assertIsInstance(title, str)
        self.assertNotIn(self.SECRET, title)
        for _ts, cached in cs._cache.values():
            self.assertTrue(cached is None or self.SECRET not in cached)

    def test_is_claude_code_vs_desktop_app(self):
        self.assertTrue(cs.is_claude_code("claude.exe", "", CODE_CMD))
        self.assertFalse(cs.is_claude_code("claude.exe", "", APP_CMD))
        self.assertFalse(cs.is_claude_code("node.exe", "", CODE_CMD))


class TitleNeverLoggedTest(unittest.TestCase):
    def test_sweep_log_never_contains_the_title(self):
        lines: list[str] = []

        class _H(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        audit = logging.getLogger("test.server_guard.audit")
        audit.handlers = [_H()]
        audit.propagate = False
        audit.setLevel(logging.INFO)
        ghost = _ghost(name="claude.exe", cmdline=CODE_CMD, cwd=r"E:\guides",
                       role=gs.ROLE_CLAUDE, session_title="שם שיחה סודי")
        sw = gs.GhostSweeper()
        with mock.patch.object(gs, "_get_sweep_logger", return_value=audit):
            sw._record_sweep([ghost])
            sw.log_user_action(ghost, "closed")
            sw._record_sweep([])
        self.assertTrue(lines)
        self.assertFalse(any("שם שיחה סודי" in line for line in lines))
        self.assertTrue(any("role=CLAUDE_SESSION" in line for line in lines))


class PanelTextTest(unittest.TestCase):
    def test_claude_card_shows_title_and_folder_or_folder_alone(self):
        import ghost_panel as gp
        with_title = _ghost(role=gs.ROLE_CLAUDE, cwd=r"E:\guides", session_title="יועץ")
        self.assertEqual(gp.claude_context(with_title), "💬 יועץ   ·   📁 E:\\guides")
        no_title = _ghost(role=gs.ROLE_CLAUDE, cwd=r"E:\guides", session_title="")
        self.assertEqual(gp.claude_context(no_title), "📁 E:\\guides")

    def test_role_chips(self):
        import ghost_panel as gp
        self.assertEqual(gp.describe_role(_ghost(role=gs.ROLE_SERVER))[0], "שרת")
        self.assertEqual(gp.describe_role(_ghost(role=gs.ROLE_SERVER,
                                                 listening_ports=()))[0], "מוגן")
        app = gp.describe_role(_ghost(role=gs.ROLE_APP_PART, name="claude.exe",
                                      exe=r"C:\Program Files\WindowsApps\Claude_2\app\Claude.exe"))
        self.assertIn("Claude", app[1])
        self.assertIsNone(gp.describe_role(_ghost(role="")))


if __name__ == "__main__":
    unittest.main()
