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


if __name__ == "__main__":
    unittest.main()
