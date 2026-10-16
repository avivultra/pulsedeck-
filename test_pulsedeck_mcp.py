"""Tests for the MCP server: protocol framing and the leftovers/close tools."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pulsedeck_mcp

HERE = Path(__file__).resolve().parent


class ProtocolTest(unittest.TestCase):
    def test_initialize_list_and_call_over_stdio(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "pulsedeck_port", "arguments": {"port": 1}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}},
        ]
        out = subprocess.run([sys.executable, str(HERE / "pulsedeck_mcp.py")],
                             input="\n".join(json.dumps(m) for m in msgs) + "\n",
                             capture_output=True, text=True, timeout=60, cwd=HERE)
        replies = [json.loads(line) for line in out.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4])        # no reply to the notification
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertEqual(names, {"pulsedeck_status", "pulsedeck_leftovers", "pulsedeck_port",
                                 "pulsedeck_slow_at", "pulsedeck_close"})
        self.assertIn("Nothing is listening on port 1", replies[2]["result"]["content"][0]["text"])
        self.assertIn("error", replies[3])

    def test_non_ascii_paths_survive_the_pipe_whatever_the_code_page(self):
        # Windows pipes default to the ANSI code page (cp1255 on a Hebrew
        # system); an MCP client sends and expects UTF-8.
        import os
        folder = tempfile.mkdtemp(prefix="גרף ביצועים ")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "pulsedeck_leftovers", "arguments": {"folder": folder}}}
        out = subprocess.run([sys.executable, str(HERE / "pulsedeck_mcp.py")],
                             input=(json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"),
                             capture_output=True, timeout=60, cwd=HERE, env=env)
        reply = json.loads(out.stdout.decode("utf-8"))
        text = reply["result"]["content"][0]["text"]
        self.assertFalse(reply["result"]["isError"], text)
        self.assertIn(folder, text)

    def test_tool_errors_come_back_as_text(self):
        r = pulsedeck_mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                  "params": {"name": "pulsedeck_leftovers",
                                             "arguments": {"folder": "/definitely/not/here"}}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("Not a folder", r["result"]["content"][0]["text"])


class LeftoversTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=self.folder)
        time.sleep(0.5)

    def tearDown(self):
        if self.child.poll() is None:
            self.child.kill()

    def test_finds_a_process_running_from_the_folder_and_closes_it(self):
        found = [d for d in pulsedeck_mcp.leftovers(self.folder) if d["pid"] == self.child.pid]
        self.assertEqual(len(found), 1)
        text = pulsedeck_mcp.tool_leftovers({"folder": self.folder})
        self.assertIn(f"pid {self.child.pid}", text)
        msg = pulsedeck_mcp.tool_close({"pid": self.child.pid, "create_time": found[0]["create_time"],
                                        "folder": self.folder})
        self.assertIn("Closed", msg)
        self.child.wait(timeout=10)

    def test_refuses_processes_outside_the_folder(self):
        other = tempfile.mkdtemp()
        with self.assertRaises(ValueError):
            pulsedeck_mcp.tool_close({"pid": self.child.pid, "folder": other})
        self.assertIsNone(self.child.poll())

    def test_never_lists_itself_or_its_parents(self):
        pids = {d["pid"] for d in pulsedeck_mcp.leftovers(str(HERE))}
        self.assertNotIn(pulsedeck_mcp.os.getpid(), pids)


class CommandLineTest(unittest.TestCase):
    """`python pulsedeck_mcp.py <command>` — the same tools without MCP. Nothing
    is closed: close is only exercised on a pid that is not in the folder."""

    def _run(self, *args):
        import os
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        out = subprocess.run([sys.executable, str(HERE / "pulsedeck_mcp.py"), *args],
                             capture_output=True, timeout=60, cwd=HERE, env=env)
        return out.returncode, out.stdout.decode("utf-8"), out.stderr.decode("utf-8")

    def test_each_command_reaches_its_tool_with_its_arguments(self):
        from unittest import mock
        seen = {}

        def fake(name):
            def tool(args):
                seen[name] = args
                return f"{name} ok"
            return tool

        fakes = {cmd: (fake(cmd), names, req) for cmd, (_f, names, req) in pulsedeck_mcp.COMMANDS.items()}
        with mock.patch.dict(pulsedeck_mcp.COMMANDS, fakes), mock.patch("builtins.print"):
            self.assertEqual(pulsedeck_mcp.cli(["status"]), 0)
            self.assertEqual(pulsedeck_mcp.cli(["leftovers"]), 0)
            self.assertEqual(pulsedeck_mcp.cli(["leftovers", "E:\\proj"]), 0)
            self.assertEqual(pulsedeck_mcp.cli(["port", "8080"]), 0)
            self.assertEqual(pulsedeck_mcp.cli(["slow-at", "14:30"]), 0)
            self.assertEqual(pulsedeck_mcp.cli(["close", "12", "1000.5", "E:\\proj"]), 0)
        self.assertEqual(seen["status"], {})
        self.assertEqual(seen["leftovers"], {"folder": "E:\\proj"})
        self.assertEqual(seen["port"], {"port": "8080"})
        self.assertEqual(seen["slow-at"], {"time": "14:30"})
        self.assertEqual(seen["close"], {"pid": "12", "create_time": "1000.5", "folder": "E:\\proj"})

    def test_wrong_usage_is_an_error(self):
        from unittest import mock
        with mock.patch("builtins.print"):
            self.assertEqual(pulsedeck_mcp.cli(["nope"]), 1)
            self.assertEqual(pulsedeck_mcp.cli(["port"]), 1)
            self.assertEqual(pulsedeck_mcp.cli(["port", "1", "2"]), 1)
            # close needs pid, create_time AND folder — no guessing the folder.
            self.assertEqual(pulsedeck_mcp.cli(["close", "12"]), 1)
            self.assertEqual(pulsedeck_mcp.cli(["--help"]), 0)

    def test_leftovers_and_port_over_a_real_pipe(self):
        folder = tempfile.mkdtemp(prefix="גרף ביצועים ")
        code, out, _err = self._run("leftovers", folder)
        self.assertEqual(code, 0)
        self.assertIn(f"Nothing is running from {folder}", out)
        code, out, _err = self._run("port", "1")
        self.assertEqual((code, out.strip()), (0, "Nothing is listening on port 1."))

    def test_tool_errors_exit_1_with_the_message_on_stderr(self):
        code, out, err = self._run("leftovers", "/definitely/not/here")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Not a folder", err)
        code, _out, err = self._run("close", "999999", "1.0", tempfile.mkdtemp())
        self.assertEqual(code, 1)
        self.assertIn("refusing to close", err)
        code, _out, err = self._run("port", "eighty")
        self.assertEqual(code, 1)
        self.assertIn("Error", err)


class PulseDeckIsNeverClosedTest(unittest.TestCase):
    """PulseDeck runs from its own folder, so it is a 'leftover' of that folder
    unless recognised. Fake processes only — nothing real is listed or closed."""

    HOME = str(pulsedeck_mcp.PULSEDECK_DIR)

    def _cmd(self, script):
        return f'"C:\\Py\\pythonw.exe" "{self.HOME}\\{script}" --dock --history --tray'

    def test_recognises_its_own_entry_points(self):
        self.assertTrue(pulsedeck_mcp.is_pulsedeck(self._cmd("monitor.py")))
        self.assertTrue(pulsedeck_mcp.is_pulsedeck(self._cmd("tray_runner.py").upper()))
        self.assertTrue(pulsedeck_mcp.is_pulsedeck(self._cmd("monitor.py").replace("\\", "/")))
        self.assertFalse(pulsedeck_mcp.is_pulsedeck(self._cmd("vite_server.py")))
        self.assertFalse(pulsedeck_mcp.is_pulsedeck('python "C:\\other\\monitor.py"'))
        self.assertFalse(pulsedeck_mcp.is_pulsedeck(""))

    def _fake(self, pid, cmd):
        from types import SimpleNamespace
        from unittest import mock
        p = mock.Mock()
        p.info = {"pid": pid, "name": "pythonw.exe", "create_time": 1000.0,
                  "memory_info": SimpleNamespace(rss=80 * 1024 ** 2),
                  "cpu_times": SimpleNamespace(user=1.0, system=0.5)}
        p.cwd.return_value = self.HOME
        p.cmdline.return_value = cmd.split(" ")
        p.parent.return_value = None
        return p

    def test_not_listed_as_a_leftover_and_refused_by_close(self):
        from unittest import mock
        pulsedeck = self._fake(4242, self._cmd("monitor.py"))
        dev = self._fake(4343, f'python "{self.HOME}\\dev_server.py"')
        with mock.patch.object(pulsedeck_mcp.psutil, "process_iter", return_value=[pulsedeck, dev]), \
             mock.patch.object(pulsedeck_mcp, "_protected_pids", return_value=set()), \
             mock.patch("ghost_sweeper._connection_map", return_value=({}, {})), \
             mock.patch.object(pulsedeck_mcp.psutil, "Process", return_value=pulsedeck), \
             mock.patch("alerts.terminate_process") as terminate:
            self.assertEqual([d["pid"] for d in pulsedeck_mcp.leftovers(self.HOME)], [4343])
            with self.assertRaisesRegex(ValueError, "PulseDeck"):
                pulsedeck_mcp.tool_close({"pid": 4242, "folder": self.HOME})
        terminate.assert_not_called()


class ServerFoldersAreNeverClosedTest(unittest.TestCase):
    """sweeper.server_cwds protects a folder from Clean all; the MCP server must
    honour it too. Fake processes only."""

    def test_listed_with_a_label_and_refused_by_close(self):
        from types import SimpleNamespace
        from unittest import mock
        from ghost_sweeper import normalize_dirs
        root = tempfile.mkdtemp()
        server_dir = str(Path(root) / "muse-bridge")
        p = mock.Mock()
        p.info = {"pid": 5151, "name": "python.exe", "create_time": 1000.0,
                  "memory_info": SimpleNamespace(rss=40 * 1024 ** 2),
                  "cpu_times": SimpleNamespace(user=1.0, system=0.0)}
        p.cwd.return_value = server_dir
        p.cmdline.return_value = ["python.exe", "bridge.py"]
        p.parent.return_value = None
        with mock.patch.object(pulsedeck_mcp.psutil, "process_iter", return_value=[p]), \
             mock.patch.object(pulsedeck_mcp, "_protected_pids", return_value=set()), \
             mock.patch.object(pulsedeck_mcp, "_server_dirs", return_value=normalize_dirs([server_dir])), \
             mock.patch("ghost_sweeper._connection_map", return_value=({}, {})), \
             mock.patch.object(pulsedeck_mcp.psutil, "Process", return_value=p), \
             mock.patch("alerts.terminate_process") as terminate:
            self.assertIn("never closed", pulsedeck_mcp.tool_leftovers({"folder": root}))
            with self.assertRaisesRegex(ValueError, "server_cwds"):
                pulsedeck_mcp.tool_close({"pid": 5151, "folder": root})
        terminate.assert_not_called()

    def test_a_sibling_folder_sharing_a_prefix_is_not_a_leftover(self):
        from types import SimpleNamespace
        from unittest import mock
        root = Path(tempfile.mkdtemp())

        def fake(pid, script):
            p = mock.Mock()
            p.info = {"pid": pid, "name": "node.exe", "create_time": 1000.0,
                      "memory_info": SimpleNamespace(rss=1), "cpu_times": None}
            p.cwd.return_value = ""
            p.cmdline.return_value = ["node.exe", str(script)]
            p.parent.return_value = None
            return p

        mine = fake(1, root / "proj" / "server.js")
        sibling = fake(2, root / "proj2" / "server.js")
        with mock.patch.object(pulsedeck_mcp.psutil, "process_iter", return_value=[mine, sibling]), \
             mock.patch.object(pulsedeck_mcp, "_protected_pids", return_value=set()), \
             mock.patch.object(pulsedeck_mcp, "_server_dirs", return_value=()), \
             mock.patch("ghost_sweeper._connection_map", return_value=({}, {})):
            self.assertEqual([d["pid"] for d in pulsedeck_mcp.leftovers(str(root / "proj"))], [1])

    def test_server_dirs_are_read_without_creating_config(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(pulsedeck_mcp, "PULSEDECK_DIR", Path(tmp)):
                self.assertEqual(pulsedeck_mcp._server_dirs(), ())
                self.assertFalse((Path(tmp) / "config.json").exists())
                (Path(tmp) / "config.json").write_text(
                    json.dumps({"sweeper": {"server_cwds": ["E:/Flow-Hub/SecurityGuard/"]}}),
                    encoding="utf-8")
                self.assertEqual(pulsedeck_mcp._server_dirs(), ("e:\\flow-hub\\securityguard",))


if __name__ == "__main__":
    unittest.main()
