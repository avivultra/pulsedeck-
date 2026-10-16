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


if __name__ == "__main__":
    unittest.main()
