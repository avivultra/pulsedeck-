"""Command lines can carry secrets. Two invariants:

* no MCP tool's output contains a process's command line at all;
* any text built from a command line passes redaction whatever the
  `redact_secrets` screen toggle says.

The MCP test starts one harmless sleeper of its own (with a marker in its
argv, listening on a free local port) and only reads; nothing is closed.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ghost_sweeper  # noqa: E402
import pulsedeck_mcp  # noqa: E402
import server_scanner  # noqa: E402

MARKER = "pdLeakMarker" + "7Q2x"
SLEEPER = ("import socket, sys, time; s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(); "
           "print(s.getsockname()[1], flush=True); time.sleep(60)")


class McpOutputHasNoCommandLines(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp()
        cls.child = subprocess.Popen([sys.executable, "-c", SLEEPER, "--token", MARKER, MARKER],
                                     cwd=cls.folder, stdout=subprocess.PIPE, text=True)
        cls.port = int(cls.child.stdout.readline())

    @classmethod
    def tearDownClass(cls):
        if cls.child.poll() is None:
            cls.child.kill()
        cls.child.wait(timeout=10)
        cls.child.stdout.close()

    def test_folder_and_port_tools(self):
        listing = pulsedeck_mcp.tool_leftovers({"folder": self.folder})
        self.assertIn(f"pid {self.child.pid}", listing)       # it is there, so this is not vacuous
        self.assertNotIn(MARKER, listing)
        port = pulsedeck_mcp.tool_port({"port": self.port})
        self.assertIn(f"pid {self.child.pid}", port)
        self.assertNotIn(MARKER, port)

    def test_a_refused_close_does_not_echo_it_either(self):
        with mock.patch.object(pulsedeck_mcp, "_server_dirs", return_value=(self.folder.lower(),)), \
             mock.patch("alerts.terminate_process") as terminate:
            with self.assertRaises(ValueError) as caught:
                pulsedeck_mcp.tool_close({"pid": self.child.pid, "folder": self.folder})
        terminate.assert_not_called()
        self.assertNotIn(MARKER, str(caught.exception))


class TextFromCommandLinesIsAlwaysRedacted(unittest.TestCase):
    SECRET = "s3cretV" + "alue42"

    def _proc(self, argv):
        p = mock.Mock()
        p.cmdline.return_value = argv
        return p

    def test_server_hint_masks_a_flag_value_it_keeps(self):
        # The hint drops flags, which used to leave the value after --token bare.
        for argv in (["node", "srv.js", "--token", self.SECRET],
                     ["node", "srv.js", "--api-key=" + self.SECRET],
                     ["python", "bot.py", "sk-" + "ant-" + "x1Y2" * 8]):
            with self.subTest(argv[-1][:6]):
                for shown in (True, False):            # the screen toggle must not matter
                    with mock.patch.object(ghost_sweeper, "secrets_are_redacted", return_value=shown):
                        hint = server_scanner._cmdline_hint(self._proc(argv))
                    self.assertNotIn(self.SECRET, hint)
                    self.assertNotIn("x1Y2x1Y2", hint)
                    self.assertTrue(hint.startswith(("srv.js", "bot.py")))

    def test_redact_args(self):
        self.assertEqual(ghost_sweeper.redact_args(["app", "--password", "pw", "--port", "80"]),
                         ["app", "--password", ghost_sweeper.MASK, "--port", "80"])
        # A flag right after a secret flag is not taken as its value.
        self.assertEqual(ghost_sweeper.redact_args(["app", "--token", "--verbose"]),
                         ["app", "--token", "--verbose"])


class ScreenToggleIsScreenOnly(unittest.TestCase):
    """sweeper.redact_secrets=false unmasks the ghost panel on screen, and
    nothing else: the sweep log, Ask Claude and Telegram stay clean."""

    SECRET = "hunter" + "2Zq9"
    BARE = "gh" + "p_" + "Q1w2E3r4" * 5
    TITLE = "כותרת" + "Marker8"

    def setUp(self):
        import logging

        class Capture(logging.Handler):
            def __init__(self):
                super().__init__()
                self.lines = []

            def emit(self, record):
                self.lines.append(record.getMessage())

        self.capture = Capture()
        isolated = logging.getLogger("ghost_sweeper.audit.leaks-test")
        isolated.handlers, isolated.propagate = [self.capture], False
        isolated.setLevel(logging.INFO)
        original = ghost_sweeper._sweep_logger
        ghost_sweeper._sweep_logger = isolated       # never the real history/sweeper.log
        self.addCleanup(setattr, ghost_sweeper, "_sweep_logger", original)
        toggle = mock.patch.object(ghost_sweeper, "secrets_are_redacted", return_value=False)
        toggle.start()
        self.addCleanup(toggle.stop)

    def _assert_clean(self, text):
        for leak in (self.SECRET, self.BARE, self.TITLE):
            self.assertNotIn(leak, text)

    def test_sweep_log(self):
        from test_ghost_sweeper import _fake_ghost
        g = _fake_ghost(cmdline=f"node bot.js --password {self.SECRET} {self.BARE}",
                        session_title=self.TITLE)
        ghost_sweeper.GhostSweeper()._record_sweep([g])
        ghost_sweeper.GhostSweeper().log_user_action(g, "close")
        text = "\n".join(self.capture.lines)
        self.assertIn("NEW node.exe", text)
        self._assert_clean(text)

    def test_closing_a_quiet_chat_does_not_log_its_title(self):
        import idle_cleanup
        import quiet_sessions
        for kind in ("claude", "terminal"):
            t = idle_cleanup.CleanupTarget(
                pid=7, name="claude.exe", create_time=1.0, rss_bytes=1, kind=kind,
                detail=quiet_sessions._session_detail(self.TITLE, 4000), cmdline="")
            idle_cleanup._log_close(t, "closed")
        text = "\n".join(self.capture.lines)
        self.assertIn("kind=claude", text)
        self._assert_clean(text)

    def test_ask_claude_context(self):
        """What Ask Claude sends is built from names and numbers only. A sleeper
        whose folder name and argv carry markers runs meanwhile; neither may
        show up. (Reads the real machine for a few seconds; closes nothing.)"""
        import json
        import load_actions
        folder = tempfile.mkdtemp(prefix=self.TITLE)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                  "--token", self.SECRET, self.BARE], cwd=folder)
        try:
            context = json.dumps(load_actions.context_for_claude(), ensure_ascii=False)
        finally:
            child.kill()
            child.wait(timeout=10)
        self.assertTrue(context.strip("{}"))
        self._assert_clean(context)
        self.assertNotIn(folder, context)

    def test_telegram_only_ever_sends_spend_reports(self):
        """Telegram gets money totals, never process data: the only modules
        that send are the spend reports and the setup's test message, and
        neither reads processes."""
        import re
        senders = sorted(p.name for p in HERE.glob("*.py")
                         if not p.name.startswith("test_")
                         and "telegram_notify.send(" in p.read_text(encoding="utf-8"))
        self.assertEqual(senders, ["claude_setup.py", "spend_reports.py"])
        process_modules = re.compile(r"^\s*(?:import|from)\s+(psutil|ghost_sweeper|quiet_sessions|"
                                     r"load_actions|idle_cleanup|pulsedeck_mcp)\b", re.M)
        for name in senders:
            with self.subTest(name):
                self.assertIsNone(process_modules.search((HERE / name).read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
