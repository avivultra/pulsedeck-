"""Tests for quiet_sessions: which Claude conversations and terminals the
"Close quiet Claude chats" list offers. Every process is a fake; nothing real
is read or closed."""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

import pytest

import ai_advisor
import quiet_sessions as qs
from idle_cleanup import CleanupTarget

NOW = 2_000_000_000.0
HOUR = 3600.0
APP = 100                     # the Claude desktop app's process


class FakeProc:
    def __init__(self, pid, name, *, ppid=APP, cmd=(), cwd="", rss=400 * 2**20,
                 created=NOW - 20 * HOUR, children=()):
        self.pid, self.info = pid, {"pid": pid, "name": name}
        self._ppid, self._cmd, self._cwd = ppid, list(cmd), cwd
        self._rss, self._created, self._children = rss, created, list(children)

    def cmdline(self): return self._cmd
    def exe(self): return r"C:\Program Files\Claude\claude.exe"
    def cwd(self): return self._cwd
    def ppid(self): return self._ppid
    def children(self, recursive=False): return self._children
    def name(self): return self.info["name"]
    def create_time(self): return self._created
    def memory_info(self): return mock.Mock(rss=self._rss)


def _session(pid, sid, cwd, **kw):
    return FakeProc(pid, "claude.exe", cmd=["claude.exe", "claude-code", "--resume", sid], cwd=cwd, **kw)


def _sid(n):
    return f"{n:08d}-0000-0000-0000-000000000000"


class _World:
    """Fake processes, transcripts (sid -> age in seconds) and titles."""

    def __init__(self, procs, ages, titles=None, established=(), me=()):
        self.procs, self.ages, self.titles = procs, ages, titles or {}
        self.established, self.me = set(established), set(me)

    def __enter__(self):
        def find_transcript(sid):
            return f"T:{sid}" if sid in self.ages else None

        def mtime(path):
            sid = path.split(":", 1)[1]
            return NOW - self.ages[sid] if sid in self.ages else None

        def title(cmd):
            return self.titles.get(qs.claude_sessions.session_id(cmd))

        self._patches = [
            mock.patch.object(qs.psutil, "process_iter",
                              lambda _attrs=None: [FakeProc(APP, "claude.exe", ppid=1)] + self.procs),
            mock.patch.object(qs, "_established_pids", lambda: self.established),
            mock.patch.object(qs, "_self_and_ancestors", lambda: self.me),
            mock.patch.object(qs.claude_sessions, "_find_transcript", find_transcript),
            mock.patch.object(qs.claude_sessions, "session_title", title),
            mock.patch.object(qs, "_transcript_mtime", mtime),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()

    def find(self, protected=((), ())):
        return qs.find(now=NOW, protected=protected)


class FindSessionsTest(unittest.TestCase):
    def test_only_conversations_quiet_for_an_hour(self):
        procs = [_session(1, _sid(1), r"E:\old", rss=500 * 2**20),
                 _session(2, _sid(2), r"E:\fresh")]
        with _World(procs, {_sid(1): 3 * HOUR, _sid(2): 5 * 60}, {_sid(1): "משחק"}) as w:
            sessions, _ = w.find()
        self.assertEqual([t.pid for t in sessions], [1])
        self.assertIn("משחק", sessions[0].detail)
        self.assertEqual(sessions[0].kind, "claude")
        self.assertEqual(sessions[0].cmdline, "")           # the long command line is not kept

    def test_never_offers_one_that_talks_works_or_is_pulsedecks_parent(self):
        procs = [_session(1, _sid(1), r"E:\a"),
                 _session(2, _sid(2), r"E:\b", children=[FakeProc(9, "bash.exe")]),
                 _session(3, _sid(3), r"E:\c"),
                 _session(4, _sid(4), r"E:\d", children=[FakeProc(8, "conhost.exe")])]
        ages = {_sid(i): 5 * HOUR for i in range(1, 5)}
        with _World(procs, ages, established={1}, me={3}) as w:
            sessions, _ = w.find()
        self.assertEqual([t.pid for t in sessions], [4])     # its console host is not work

    def test_no_transcript_cannot_be_proven_quiet(self):
        procs = [FakeProc(1, "claude.exe", cmd=["claude.exe", "claude-code"], cwd=r"E:\x")]
        with _World(procs, {}) as w:
            self.assertEqual(w.find(), ([], []))

    def test_protected_by_folder_or_title_word(self):
        procs = [_session(1, _sid(1), r"C:\devProject\FlowStream-V2\sub"),
                 _session(2, _sid(2), r"E:\guides"),
                 _session(3, _sid(3), r"E:\game")]
        ages = {_sid(i): 9 * HOUR for i in range(1, 4)}
        titles = {_sid(2): "יועץ FlowStreamV2", _sid(3): "משחק"}
        with _World(procs, ages, titles) as w:
            sessions, _ = w.find(protected=((r"c:\devproject\flowstream-v2",), ("flowstream",)))
        self.assertEqual([t.pid for t in sessions], [3])

    def test_heaviest_first(self):
        procs = [_session(1, _sid(1), r"E:\a", rss=100), _session(2, _sid(2), r"E:\b", rss=900)]
        with _World(procs, {_sid(1): 2 * HOUR, _sid(2): 2 * HOUR}) as w:
            sessions, _ = w.find()
        self.assertEqual([t.pid for t in sessions], [2, 1])


class FindTerminalsTest(unittest.TestCase):
    def _shell(self, pid, cwd, **kw):
        return FakeProc(pid, "pwsh.exe", cwd=cwd, rss=90 * 2**20, **kw)

    def test_terminal_goes_with_its_conversation(self):
        procs = [_session(1, _sid(1), r"E:\game"), self._shell(11, r"E:\game")]
        with _World(procs, {_sid(1): 2 * HOUR}, {_sid(1): "משחק"}) as w:
            _, terminals = w.find()
        self.assertEqual([t.pid for t in terminals], [11])
        self.assertIn("משחק", terminals[0].detail)
        self.assertNotIn("💬", terminals[0].detail)

    def test_terminal_left_by_a_closed_conversation(self):
        procs = [self._shell(11, r"E:\gone")]
        with _World(procs, {}) as w:
            _, terminals = w.find()
        self.assertEqual([t.pid for t in terminals], [11])

    def test_terminal_stays_when_an_open_conversation_shares_its_folder(self):
        # Two conversations in one folder, one quiet and one active: the
        # terminal may be the active one's.
        procs = [_session(1, _sid(1), r"E:\flow"), _session(2, _sid(2), r"E:\flow"),
                 self._shell(11, r"E:\flow")]
        with _World(procs, {_sid(1): 9 * HOUR, _sid(2): 60}) as w:
            sessions, terminals = w.find()
        self.assertEqual([t.pid for t in sessions], [1])
        self.assertEqual(terminals, [])

    def test_busy_young_or_foreign_terminals_stay(self):
        procs = [self._shell(11, r"E:\gone", children=[FakeProc(12, "python.exe")]),
                 self._shell(13, r"E:\gone", created=NOW - 60),
                 self._shell(14, r"E:\gone", ppid=555)]          # not the Claude app's
        with _World(procs, {}) as w:
            self.assertEqual(w.find()[1], [])

    def test_a_protected_conversations_terminal_stays(self):
        procs = [_session(1, _sid(1), r"E:\flow"), self._shell(11, r"E:\flow")]
        with _World(procs, {_sid(1): 9 * HOUR}, {_sid(1): "FlowStream"}) as w:
            self.assertEqual(w.find(protected=((), ("flowstream",))), ([], []))


class VerifyTest(unittest.TestCase):
    def _target(self, path, mtime):
        return CleanupTarget(pid=1, name="claude.exe", create_time=0, rss_bytes=1, kind="claude",
                             detail="", member_pids=(1,), stamp_path=path, stamp_mtime=mtime)

    def test_a_conversation_written_in_meanwhile_stays(self, ):
        changed, same = self._target("a", 1.0), self._target("b", 2.0)
        with mock.patch.object(qs, "verify_idle", lambda ts, **kw: (list(ts), [])), \
                mock.patch.object(qs, "_transcript_mtime", {"a": 5.0, "b": 2.0}.get):
            ok, skipped = qs.verify([changed, same])
        self.assertEqual(ok, [same])
        self.assertEqual([t for t, _why in skipped], [changed])


class SummaryTest(unittest.TestCase):
    def test_numbers_only(self):
        t = CleanupTarget(pid=1, name="claude.exe", create_time=0, rss_bytes=512 * 2**20, kind="claude",
                          detail="💬 סוד", cwd=r"E:\secret")
        out = qs.summary(([t], []))
        self.assertEqual(out["quiet_sessions"], 1)
        self.assertEqual(out["quiet_sessions_memory_mb"], 512)
        self.assertNotIn("סוד", str(out))
        self.assertNotIn("secret", str(out))
        self.assertEqual(qs.summary(([], [])), {})


class ProtectedSettingsTest(unittest.TestCase):
    def test_read_from_config(self):
        cfg = {"claude": {"protected_chat_folders": [r"C:\devProject\FlowStream-V2\\"],
                          "protected_chat_words": [" FlowStream ", ""]}}
        with mock.patch("config.load_config", return_value=cfg):
            self.assertEqual(qs.protected_settings(), ((r"c:\devproject\flowstream-v2",), ("flowstream",)))


class AskClaudeTest(unittest.TestCase):
    def test_close_quiet_chats_is_allowed_only_when_offered(self):
        snap = {"claude_app": {"closable_quiet_chats": {"quiet_sessions": 2, "can": ["close_quiet_chats"]}}}
        self.assertEqual(ai_advisor._allowed_actions(snap)["claude"], ("Claude", ["close_quiet_chats"]))
        self.assertNotIn("claude", ai_advisor._allowed_actions({"claude_app": {}}))


if __name__ == "__main__":
    unittest.main()
