"""Tests for easing the load (load_actions) and the two ways Claude reaches it.

Every process here is fake: psutil.Process, the process table, the window
list, WM_CLOSE and TerminateProcess are all replaced. Nothing real is
lowered or closed."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import psutil

import load_actions as la

NORMAL = getattr(psutil, "NORMAL_PRIORITY_CLASS", 0)
GB = 1024 ** 3
SERVER = os.path.normcase(r"E:\tools\my-server")
WINDOWS_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "weirdsvc.exe")


class FakeProc:
    def __init__(self, pid, name, ct=1000.0, exe=None, cmd="", cwd=r"C:\Users\u", nice=NORMAL, deny=False):
        self.pid, self.name, self.ct = pid, name, ct
        self._exe = exe or rf"C:\Program Files\{name}\{name}.exe"
        self.cmd, self._cwd, self._nice, self.deny = cmd, cwd, nice, deny

    def create_time(self):
        return self.ct

    def nice(self, value=None):
        if value is None:
            return self._nice
        if self.deny:
            raise psutil.AccessDenied(self.pid)
        self._nice = value

    def exe(self):
        return self._exe

    def cmdline(self):
        return self.cmd.split(" ") if self.cmd else []

    def cwd(self):
        return self._cwd


def _machine():
    pulsedeck = os.path.join(str(Path(la.__file__).resolve().parent), "monitor.py")
    procs = [
        FakeProc(101, "chrome"), FakeProc(102, "chrome"),
        FakeProc(201, "MsMpEng"),
        FakeProc(301, "Code"),
        FakeProc(401, "pythonw", cmd=f"pythonw {pulsedeck} --dock"),
        FakeProc(402, "python", cmd="python worker.py"),
        FakeProc(403, "python", cmd="python -m pytest"),
        FakeProc(501, "node", cwd=SERVER),
        FakeProc(601, "weirdsvc", exe=WINDOWS_EXE),
        FakeProc(701, "claude"),
        FakeProc(801, "slack"),
        FakeProc(901, "notepad"),
    ]
    return {p.pid: p for p in procs}


class _Fixture(unittest.TestCase):
    """A fake machine: the table, the windows, the guard's inputs, the state file."""

    def setUp(self):
        self.procs = _machine()
        self.windows = {101: [0x1001], 801: [0x8001]}
        self.posted: list[list[int]] = []
        self.alive = set(self.procs)
        tmp = Path(tempfile.mkdtemp())
        rows = [(p.pid, p.name + ".exe", p.ct, 1.0, 100 * 1024 ** 2) for p in self.procs.values()]

        def process(pid):
            if pid not in self.procs or pid not in self.alive:
                raise psutil.NoSuchProcess(pid)
            return self.procs[pid]

        def post(hwnds):
            self.posted.append(list(hwnds))

        patches = [
            mock.patch.object(la, "STATE_PATH", tmp / "load_actions.json"),
            mock.patch.object(la, "_table", side_effect=lambda: [r for r in rows if r[0] in self.alive]),
            mock.patch.object(la, "_top_windows", side_effect=lambda: {k: v for k, v in self.windows.items()
                                                                       if k in self.alive}),
            mock.patch.object(la, "_post_close", side_effect=post),
            mock.patch.object(la, "_alive", side_effect=lambda pid, ct: pid in self.alive),
            mock.patch("psutil.Process", side_effect=process),
            mock.patch("pulsedeck_mcp._protected_pids", return_value={403}),   # "this session"
            mock.patch("pulsedeck_mcp._server_dirs", return_value=(SERVER.lower(),)),
            mock.patch("time.sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def history(self, names):
        items = [{"name": n, "cpu": c, "rss": r, "count": k} for n, c, r, k in names]
        return NS(who_was_busy=lambda t0, t1, limit: {"cpu": items, "memory": items})


class ClassifyTest(_Fixture):
    def test_each_program_gets_what_may_be_done_and_why_not(self):
        progs, source = la.heavy_programs(history=self.history([
            ("chrome", 30.0, 2 * GB, 2), ("MsMpEng", 9.0, GB, 1), ("Code", 5.0, GB, 1),
            ("python", 8.0, GB, 2), ("pythonw", 2.0, GB, 1), ("node", 4.0, GB, 1),
            ("weirdsvc", 3.0, GB, 1), ("claude", 7.0, GB, 1), ("slack", 1.0, GB, 1)]))
        self.assertEqual(source, "history")
        by = {p.name: p for p in progs}
        self.assertEqual(by["chrome"].can(), ["lower", "close"])
        self.assertTrue(by["chrome"].has_window)
        self.assertEqual(by["MsMpEng"].never, "antivirus")
        self.assertEqual(by["Code"].never, "editor")
        self.assertEqual(by["claude"].never, "claude")
        self.assertEqual(by["pythonw"].never, "pulsedeck")       # PulseDeck by its command line
        self.assertEqual(by["node"].never, "server")             # runs from a folder in server_cwds
        self.assertEqual(by["weirdsvc"].never, "system")         # runs from the Windows folder
        # Many programs are "python": lowering is allowed, closing by name is not,
        # and this session's own process is left out.
        self.assertEqual(by["python"].can(), ["lower"])
        self.assertEqual([t[0] for t in by["python"].targets], [402])
        self.assertEqual(by["python"].skipped, {"self": 1})

    def test_without_history_it_measures_for_a_second(self):
        with mock.patch.object(la, "_measure", return_value={"cpu": [{"name": "slack", "cpu": 3.0, "rss": GB,
                                                                       "count": 1}], "memory": []}) as m:
            progs, source = la.heavy_programs(history=self.history([]))
        m.assert_called_once()
        self.assertEqual((source, [p.name for p in progs]), ("measured", ["slack"]))

    def test_claude_gets_names_and_numbers_only(self):
        progs, _ = la.heavy_programs(history=self.history([("chrome", 30.0, 2 * GB, 2), ("node", 4.0, GB, 1)]))
        sent = la.for_claude(progs)
        self.assertEqual(sent[0], {"name": "chrome", "cpu_percent_of_machine": 30.0, "memory_mb": 2048,
                                   "count": 2, "has_window": True, "can": ["lower", "close"]})
        self.assertEqual(sent[1]["never_touched"], "server")
        text = json.dumps(sent)
        for private in ("Program Files", "my-server", "worker.py", "Users"):
            self.assertNotIn(private, text)


class LowerTest(_Fixture):
    def test_lower_then_restore(self):
        r = la.lower("chrome")
        self.assertEqual((r.outcome, r.done), ("lowered", 2))
        self.assertTrue(all(self.procs[p].nice() == la._BELOW for p in (101, 102)))
        self.assertEqual(la.lower("chrome").outcome, "already")
        progs, _ = la.heavy_programs(history=self.history([("chrome", 30.0, GB, 2)]))
        self.assertEqual(progs[0].can(), ["restore", "close"])
        r = la.restore("chrome")
        self.assertEqual((r.outcome, r.done), ("restored", 2))
        self.assertTrue(all(self.procs[p].nice() == NORMAL for p in (101, 102)))
        self.assertEqual(la.restore("chrome").outcome, "nothing")

    def test_protected_programs_are_refused_and_untouched(self):
        for name, reason in (("MsMpEng", "antivirus"), ("pythonw", "pulsedeck"), ("node", "server"),
                             ("Code", "editor"), ("claude", "claude"), ("weirdsvc", "system")):
            r = la.lower(name)
            self.assertEqual((r.outcome, r.reason), ("refused", reason), name)
        self.assertTrue(all(p.nice() == NORMAL for p in self.procs.values()))

    def test_python_lowers_only_the_unprotected_one(self):
        la.lower("python")
        self.assertEqual(self.procs[402].nice(), la._BELOW)
        self.assertEqual(self.procs[403].nice(), NORMAL)       # this session
        self.assertEqual(self.procs[401].nice(), NORMAL)       # PulseDeck

    def test_a_reused_pid_is_left_alone(self):
        self.procs[101].ct = 5000.0                            # pid 101 now belongs to someone else
        with mock.patch.object(la, "_table", return_value=[(101, "chrome.exe", 1000.0, 1.0, 1)]):
            self.assertNotEqual(la.lower("chrome").outcome, "lowered")
        self.assertEqual(self.procs[101].nice(), NORMAL)

    def test_access_denied_is_reported(self):
        self.procs[801].deny = True
        self.assertEqual(la.lower("slack").outcome, "denied")


class CloseTest(_Fixture):
    def test_close_asks_the_windows_like_x_and_waits(self):
        self.alive -= set()                                   # still alive when asked

        def gone(hwnds):
            self.posted.append(list(hwnds))
            self.alive -= {101, 102}
        with mock.patch.object(la, "_post_close", side_effect=gone):
            r = la.close("chrome")
        self.assertEqual(self.posted, [[0x1001]])             # only chrome's window
        self.assertEqual(r.outcome, "closed")
        self.assertEqual(r.freed, 200 * 1024 ** 2)
        self.assertFalse(r.can_force)

    def test_still_open_offers_force_only_as_a_next_step(self):
        r = la.close("slack", wait=0.01)
        self.assertEqual(r.outcome, "still_open")
        self.assertTrue(r.can_force)
        self.assertEqual(self.posted, [[0x8001]])

    def test_no_window_means_only_force_could_end_it(self):
        r = la.close("notepad")
        self.assertEqual(r.outcome, "no_window")
        self.assertEqual(self.posted, [])

    def test_hosts_and_protected_are_never_closed(self):
        self.assertEqual((la.close("python").outcome, la.close("python").reason), ("refused", "host"))
        self.assertEqual(la.close("MsMpEng").reason, "antivirus")
        self.assertEqual(la.close("pythonw").reason, "pulsedeck")
        self.assertEqual(self.posted, [])

    def test_force_needs_a_normal_close_first(self):
        with mock.patch("alerts.terminate_process", return_value="closed") as kill:
            self.assertEqual(la.force_close("slack").outcome, "not_tried")
            kill.assert_not_called()
            la.close("slack", wait=0.01)
            r = la.force_close("slack")
        self.assertEqual(r.outcome, "closed")
        kill.assert_called_once_with(801, "slack.exe", 1000.0)
        # Used up: a second force needs a new normal close.
        with mock.patch("alerts.terminate_process", return_value="closed") as kill:
            self.assertEqual(la.force_close("slack").outcome, "not_tried")
            kill.assert_not_called()

    def test_force_never_reaches_protected_processes(self):
        with mock.patch("alerts.terminate_process", return_value="closed") as kill:
            for name in ("python", "pythonw", "MsMpEng", "node", "claude"):
                la.close(name, wait=0.01)
                self.assertEqual(la.force_close(name).outcome, "refused", name)
        kill.assert_not_called()

    def test_messages_in_both_languages(self):
        import i18n
        r = la.Result("close", "closed", "chrome", freed=GB)
        i18n.set_language("he")
        self.assertIn("נסגר", r.text())
        self.assertEqual(r.text(english=True), "chrome closed, freeing about 1.0 GB.")


class LeftoversTest(unittest.TestCase):
    """Ask Claude's "clean": what "Clean all" would offer, by program, and
    closed only after the never-touched rules and a fresh idle check.
    Nothing real is closed: the search, the check and the close are mocked."""

    def _t(self, pid, name="node.exe", kind="ghost", mb=200):
        from idle_cleanup import CleanupTarget
        return CleanupTarget(pid=pid, name=name, create_time=1.0, rss_bytes=mb * 1024 ** 2,
                             kind=kind, detail="", member_pids=(pid,))

    def test_grouped_by_program_heaviest_first(self):
        out = la.leftovers_for_claude([self._t(1), self._t(2, kind="idle"), self._t(3, "Slack.exe", "idle", 900)])
        self.assertEqual(out, [
            {"name": "Slack", "count": 1, "memory_mb": 900, "kind": "idle", "can": ["clean"]},
            {"name": "node", "count": 2, "memory_mb": 400, "kind": "mixed", "can": ["clean"]}])

    def test_never_touched_programs_are_not_leftovers(self):
        found = ([self._t(1), self._t(2, "conhost.exe"), self._t(3, "Code.exe")], [self._t(4, "pwsh.exe", "idle")],
                 "", set())
        with mock.patch("idle_cleanup.gather", return_value=found):
            self.assertEqual([t.pid for t in la.leftovers()], [1])

    def _clean(self, name, found, *, why=None, woke=(), closed="closed"):
        guard = mock.Mock()
        guard.why.side_effect = lambda pid, _key: (why or {}).get(pid)

        def verify(targets):
            return [t for t in targets if t.pid not in woke], [(t, "woke") for t in targets if t.pid in woke]
        with mock.patch.object(la, "leftovers", return_value=found), \
             mock.patch.object(la, "_Guard", return_value=guard), \
             mock.patch("idle_cleanup.verify_idle", side_effect=verify) as check, \
             mock.patch("idle_cleanup.close_targets",
                        side_effect=lambda ts: [(t, closed) for t in ts]) as close:
            return la.clean(name), check, close

    def test_closes_only_that_programs_leftovers(self):
        r, check, close = self._clean("node", [self._t(1), self._t(2), self._t(3, "Slack.exe", "idle")])
        self.assertEqual((r.outcome, r.done, r.total), ("cleaned", 2, 2))
        self.assertEqual(r.freed, 400 * 1024 ** 2)
        self.assertEqual([t.pid for t in close.call_args.args[0]], [1, 2])
        self.assertTrue(r.ok)

    def test_a_leftover_that_woke_up_stays(self):
        r, _check, close = self._clean("node", [self._t(1), self._t(2)], woke={2})
        self.assertEqual((r.outcome, r.done, r.total), ("partly", 1, 2))
        self.assertEqual([t.pid for t in close.call_args.args[0]], [1])
        r, _check, close = self._clean("node", [self._t(1)], woke={1})
        self.assertEqual(r.outcome, "woke")
        close.assert_not_called()

    def test_never_reaches_protected_processes(self):
        r, check, close = self._clean("claude", [self._t(1, "claude.exe")])
        self.assertEqual((r.outcome, r.reason), ("refused", "claude"))
        r, check, close = self._clean("pythonw", [self._t(5, "pythonw.exe")], why={5: "pulsedeck"})
        self.assertEqual(r.outcome, "no_leftovers")
        check.assert_not_called()
        close.assert_not_called()

    def test_messages_in_both_languages(self):
        import i18n
        i18n.set_language("he")
        r = la.Result("clean", "cleaned", "node", done=3, total=3, freed=GB)
        self.assertEqual(r.text(), "נוקו 3 שאריות של node ושוחררו בערך 1.0 GB.")
        self.assertEqual(r.text(english=True), "Cleaned up 3 node leftover(s), freeing about 1.0 GB.")
        self.assertIn("שאריות", la.Result("clean", "partly", "node", done=1, total=2).text())


class ClaudeUsageTest(unittest.TestCase):
    """Claude's own share, for Ask Claude: counts and megabytes only."""

    NOW = 1_000_000.0

    def _rows(self):
        mb = 1024 ** 2
        return [{"kind": "app", "pid": 1, "memory": 500 * mb, "created": 0},
                {"kind": "app", "pid": 2, "memory": 300 * mb, "created": 0},
                {"kind": "session", "pid": 10, "memory": 470 * mb, "created": 0},
                {"kind": "session", "pid": 11, "memory": 480 * mb, "created": 0},
                {"kind": "session", "pid": 12, "memory": 450 * mb, "created": 0},
                {"kind": "shell", "pid": 20, "memory": 30 * mb, "created": self.NOW - 7 * 3600},
                {"kind": "shell", "pid": 21, "memory": 30 * mb, "created": self.NOW - 600}]

    def test_quiet_sessions_and_old_shells_are_counted(self):
        out = la.claude_usage(self._rows(), {10: 2 * 3600, 11: 3601, 12: 60}, now=self.NOW)
        self.assertEqual(out, {
            "app": {"processes": 2, "memory_mb": 800},
            "code_sessions": {"count": 3, "memory_mb": 1400, "idle_over_1h": 2, "idle_over_1h_memory_mb": 950},
            "shells_opened_by_claude": {"count": 2, "memory_mb": 60, "older_than_6h": 1},
            "never_touched": "claude"})

    def test_no_sampler_proves_no_session_quiet(self):
        out = la.claude_usage(self._rows(), {}, now=self.NOW)
        self.assertEqual(out["code_sessions"]["idle_over_1h"], 0)

    def test_without_claude_nothing_is_added(self):
        self.assertEqual(la.claude_usage([], {}), {})

    def test_context_keeps_the_parts_that_worked(self):
        with mock.patch.object(la, "heavy_programs", side_effect=RuntimeError("boom")), \
             mock.patch.object(la, "_claude_rows", return_value=self._rows()), \
             mock.patch.object(la, "_idle_seconds_by_pid", return_value={}):
            out = la.context_for_claude()
        self.assertNotIn("load_last_minute", out)
        self.assertEqual(out["claude_app"]["code_sessions"]["count"], 3)


class SuggestionsTest(unittest.TestCase):
    """The API channel: Claude's suggestions survive only where the snapshot allows them."""

    SNAP = {"load_last_minute": [
        {"name": "chrome", "can": ["lower", "close"]}, {"name": "python", "can": ["lower"]},
        {"name": "MsMpEng", "never_touched": "antivirus"}],
        "leftovers": [{"name": "node", "count": 3, "memory_mb": 600, "kind": "abandoned", "can": ["clean"]},
                      {"name": "python", "count": 1, "memory_mb": 150, "kind": "idle", "can": ["clean"]}]}

    def _response(self, actions, text="המחשב עמוס בגלל chrome."):
        return NS(stop_reason="tool_use", usage=None, content=[
            NS(type="text", text=text),
            NS(type="tool_use", name="suggest_actions", input={"actions": actions})])

    def _ask(self, response, actions_out):
        import ai_advisor
        create = mock.Mock(return_value=response)
        client = NS(beta=NS(messages=NS(create=create)))
        budget = ai_advisor.Budget(Path(tempfile.mkdtemp()) / "u.json")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            text = ai_advisor.ask_claude(self.SNAP, language="he", client=client, budget=budget,
                                         actions_out=actions_out)
        return text, create.call_args.kwargs

    def test_only_allowed_programs_and_actions_survive(self):
        out: list = []
        text, kw = self._ask(self._response([
            {"program": "Chrome", "action": "close", "why": "תופס 2 GB"},
            {"program": "python", "action": "close", "why": "x"},        # not in its "can"
            {"program": "MsMpEng", "action": "lower", "why": "x"},       # never touched
            {"program": "explorer", "action": "close", "why": "x"},      # not in the snapshot
            {"program": "chrome", "action": "lower", "why": "x"},        # a second one for chrome
        ]), out)
        self.assertEqual(out, [{"program": "chrome", "action": "close", "why": "תופס 2 GB"}])
        self.assertEqual(text, "המחשב עמוס בגלל chrome.")
        self.assertEqual(kw["tools"][0]["name"], "suggest_actions")

    def test_clean_only_for_leftovers(self):
        out: list = []
        self._ask(self._response([
            {"program": "chrome", "action": "clean", "why": "x"},          # chrome has no leftovers
            {"program": "node", "action": "clean", "why": "3 נטושים"},
            {"program": "python", "action": "clean", "why": "לא פעיל"},    # lower from the minute, clean from leftovers
        ]), out)
        self.assertEqual([(a["program"], a["action"]) for a in out], [("node", "clean"), ("python", "clean")])

    def test_no_tool_without_actions_out(self):
        import ai_advisor
        create = mock.Mock(return_value=NS(stop_reason="end_turn", usage=None,
                                           content=[NS(type="text", text="ok")]))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            ai_advisor.ask_claude(self.SNAP, client=NS(beta=NS(messages=NS(create=create))),
                                  budget=ai_advisor.Budget(Path(tempfile.mkdtemp()) / "u.json"))
        self.assertNotIn("tools", create.call_args.kwargs)


class ConfirmTextTest(unittest.TestCase):
    def test_every_hebrew_dialog_line_starts_with_a_hebrew_word(self):
        # Windows lays a line out in the direction of its first letter.
        import i18n
        from claude_panel import _confirm_text
        from dark_menu import _reads_rtl
        i18n.set_language("he")
        try:
            for action in ("lower", "restore", "close", "force", "clean"):
                title, text, ok, _danger = _confirm_text(action, "chrome")
                for line in [title, ok] + text.split("\n"):
                    self.assertTrue(_reads_rtl(line), (action, line))
        finally:
            i18n.set_language("he")


class PanelTest(unittest.TestCase):
    """The Ask Claude window: a suggestion is a button; nothing runs before
    the user confirms it, and a normal close that fails offers force as a
    second, separate confirmation. Claude and every action are mocked."""

    def setUp(self):
        import tkinter as tk
        import i18n
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"no display: {e}")
        self.root.withdraw()
        i18n.set_language("he")

    def tearDown(self):
        self.root.destroy()

    def _wait_for(self, cond, timeout=5.0):
        import time
        end = time.monotonic() + timeout
        while not cond():
            self.assertLess(time.monotonic(), end, "timed out")
            self.root.update()
            time.sleep(0.02)

    def _labels(self, w):
        import tkinter as tk
        for c in w.winfo_children():
            if isinstance(c, tk.Label):
                yield c
            yield from self._labels(c)

    def test_suggestion_button_confirm_then_run(self):
        import ai_advisor
        import claude_panel
        import dark_menu

        def fake_ask(snapshot, question, language, actions_out):
            actions_out.append({"program": "slack", "action": "close", "why": "תופס הרבה זיכרון"})
            return "המחשב עמוס."

        not_closed = la.Result("close", "still_open", "slack")
        dialogs = []
        real_confirm = dark_menu.confirm
        with mock.patch.object(ai_advisor, "is_configured", return_value=True), \
             mock.patch.object(ai_advisor, "ask_claude", side_effect=fake_ask), \
             mock.patch.object(la, "heavy_programs", return_value=([], "history")), \
             mock.patch.object(la, "leftovers", return_value=[]), \
             mock.patch.object(la, "_claude_rows", return_value=[]), \
             mock.patch.object(la, "close", return_value=not_closed) as close, \
             mock.patch.object(la, "force_close", return_value=la.Result("force", "closed", "slack")) as force, \
             mock.patch.object(dark_menu, "confirm", side_effect=lambda *a, **k: dialogs.append((a, k))
                               or real_confirm(*a, **k)):
            win = claude_panel.open_claude_panel(self.root, lambda: {})
            self._wait_for(lambda: any("slack" in w.cget("text") for w in self._labels(win)))
            button = next(w for w in self._labels(win) if "סגור את slack" in w.cget("text"))
            button.event_generate("<Button-1>")
            self.root.update()
            self.assertEqual(len(dialogs), 1)
            close.assert_not_called()                          # asked, not run
            title, _text, ok, answer = dialogs[0][0][1], dialogs[0][0][2], dialogs[0][0][3], dialogs[0][0][4]
            self.assertIn("slack", title)
            answer(False)                                      # the user said no
            self.root.update()
            close.assert_not_called()
            answer(True)                                       # now yes
            self._wait_for(lambda: close.called and "בכוח" in button.cget("text"))
            close.assert_called_once_with("slack")
            force.assert_not_called()                          # force is its own step
            button.event_generate("<Button-1>")
            self.root.update()
            self.assertEqual(len(dialogs), 2)
            self.assertTrue(dialogs[1][1].get("danger"))
            dialogs[1][0][4](True)
            self._wait_for(lambda: force.called)
            force.assert_called_once_with("slack")
            win.destroy()

    def test_clean_suggestion_asks_as_dangerous_then_runs(self):
        import ai_advisor
        import claude_panel
        import dark_menu

        def fake_ask(snapshot, question, language, actions_out):
            actions_out.append({"program": "node", "action": "clean", "why": "3 תהליכים נטושים"})
            return "יש שאריות."

        dialogs = []
        cleaned = la.Result("clean", "cleaned", "node", done=3, total=3, freed=GB)
        with mock.patch.object(ai_advisor, "is_configured", return_value=True), \
             mock.patch.object(ai_advisor, "ask_claude", side_effect=fake_ask), \
             mock.patch.object(la, "context_for_claude", return_value={}), \
             mock.patch.object(la, "clean", return_value=cleaned) as clean, \
             mock.patch.object(dark_menu, "confirm", side_effect=lambda *a, **k: dialogs.append((a, k))):
            win = claude_panel.open_claude_panel(self.root, lambda: {})
            self._wait_for(lambda: any("node" in w.cget("text") for w in self._labels(win)))
            button = next(w for w in self._labels(win) if "נקה את השאריות של node" in w.cget("text"))
            button.event_generate("<Button-1>")
            self.root.update()
            clean.assert_not_called()                          # asked, not run
            self.assertTrue(dialogs[0][1].get("danger"))
            dialogs[0][0][4](True)
            self._wait_for(lambda: clean.called and "✓" in button.cget("text"))
            clean.assert_called_once_with("node")
            win.destroy()


class CommandLineTest(unittest.TestCase):
    def test_actions_need_a_name_and_refusals_are_errors(self):
        import pulsedeck_mcp
        self.assertEqual(pulsedeck_mcp.cli(["lower"]), 1)
        refused = la.Result("lower", "refused", "MsMpEng", reason="antivirus")
        with mock.patch.object(la, "lower", return_value=refused):
            with self.assertRaises(ValueError) as e:
                pulsedeck_mcp.TOOLS["pulsedeck_lower"][0]({"name": "MsMpEng"})
        self.assertIn("never touched", str(e.exception))

    def test_load_report(self):
        import pulsedeck_mcp
        progs = [la.Program("chrome", 30.0, 2 * GB, 12, targets=[(1, 1.0, "chrome.exe", 1)], has_window=True),
                 la.Program("MsMpEng", 9.0, GB, 1, never="antivirus"),
                 la.Program("pythonw", 2.0, GB, 3, never="server", skipped={"server": 2, "pulsedeck": 1})]
        with mock.patch.object(la, "heavy_programs", return_value=(progs, "history")):
            text = pulsedeck_mcp.tool_load({})
        self.assertIn("- chrome ×12: CPU 30.0%, memory 2.0 GB, has a window. Can: lower, close", text)
        self.assertIn("- MsMpEng: CPU 9.0%, memory 1.0 GB. Never touched: the antivirus", text)
        # Mixed reasons are all named, so nobody has to guess which process is which.
        self.assertIn("Never touched: 2 a server you keep running, 1 PulseDeck itself", text)
        self.assertIn("explicit yes", text)


if __name__ == "__main__":
    unittest.main()
