"""The periodic cleanup offer only asks. Nothing real is searched or closed:
the searches, the verify step and the closer are all fakes."""
from __future__ import annotations

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cleanup_offer  # noqa: E402
from idle_cleanup import MIB, CleanupTarget  # noqa: E402


def T(pid, kind="idle", mb=100, name="node.exe", detail="", members=()):
    return CleanupTarget(pid=pid, name=name, create_time=1.0, rss_bytes=mb * MIB, kind=kind,
                         detail=detail, member_pids=tuple(members) or (pid,))


class FindOffer(unittest.TestCase):
    def test_combines_both_searches_each_process_once(self):
        chat = T(1, "claude", 400, "claude.exe", "צ'אט דמו  ·  שקט 70 דק'")
        term = T(2, "terminal", 50, "pwsh.exe", "צ'אט דמו")
        ghost = T(3, "ghost", 120)
        dup = T(1, "idle", 400, "claude.exe")                 # the same chat seen as a hog
        family = T(9, "idle", 80, members=(9, 3))              # holds pid 3 as a member
        got = cleanup_offer.find_offer(lambda: ([chat], [term]), lambda: [ghost, dup, family])
        self.assertEqual([t.pid for t in got], [1, 2, 3])

    def test_one_search_failing_keeps_the_other(self):
        def boom():
            raise RuntimeError("no access")
        got = cleanup_offer.find_offer(boom, lambda: [T(5)])
        self.assertEqual([t.pid for t in got], [5])

    def test_texts(self):
        ts = [T(1, "claude", 1000, "claude.exe", "תיקון באג  ·  שקט 2 ש'"), T(2, mb=420)]
        self.assertIn("2", cleanup_offer.headline(ts))
        self.assertIn("1,420 MB", cleanup_offer.headline(ts))
        self.assertTrue(cleanup_offer.label(ts[0]).startswith("תיקון באג"))
        self.assertTrue(cleanup_offer.label(ts[1]).startswith("node.exe"))
        text = cleanup_offer.outcome_text(([(ts[0], "closed"), (ts[1], "denied")], [(T(3), "woke")]))
        self.assertEqual(len(text.splitlines()), 3)

    def test_sections_keep_every_target(self):
        ts = [T(1, "claude"), T(2, "terminal"), T(3, "ghost"), T(4, "idle"), T(5, "odd")]
        got = [t.pid for _title, items in cleanup_offer.sections(ts) for t in items]
        self.assertEqual(sorted(got), [1, 2, 3, 4, 5])


class Settings(unittest.TestCase):
    def test_the_switch_persists_and_defaults_on(self):
        import config
        tmp = Path(tempfile.mkdtemp())
        real_load, real_save = config.load_config, config.save_config
        with mock.patch.object(config, "load_config", lambda: real_load(tmp)), \
             mock.patch.object(config, "save_config", lambda cfg, _base=None: real_save(cfg, tmp)):
            self.assertEqual(cleanup_offer.settings(), (True, 3.0))
            cleanup_offer.set_enabled(False)
            self.assertFalse(cleanup_offer.is_enabled())
            cleanup_offer.set_enabled(True)
            self.assertTrue(cleanup_offer.is_enabled())


class _TkTest(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:                   # no display (CI without Xvfb)
            self.skipTest(f"no display: {e}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def settle(self, seconds=0.0):
        import time
        end = time.time() + seconds
        while True:
            self.root.update()
            if time.time() >= end:
                break
            time.sleep(0.02)

    def wait_for(self, cond, timeout=5.0):
        import time
        end = time.time() + timeout
        while not cond() and time.time() < end:
            self.root.update()
            time.sleep(0.02)
        self.assertTrue(cond())


AREA = (0, 0, 1920, 1040)


class Card(_TkTest):
    def make(self, targets, **kw):
        self.verify = mock.Mock(side_effect=lambda ts: (ts[1:], [(ts[0], "woke up")]))
        self.closer = mock.Mock(side_effect=lambda ts: [(t, "closed") for t in ts])
        card = cleanup_offer.OfferCard(self.root, targets, verify=self.verify, closer=self.closer,
                                       area=AREA, **kw)
        self.addCleanup(card.dismiss)
        return card

    def test_showing_the_card_closes_nothing(self):
        card = self.make([T(i) for i in range(1, 8)])
        self.settle(0.3)
        self.verify.assert_not_called()
        self.closer.assert_not_called()
        x, y = card.win.winfo_rootx(), card.win.winfo_rooty()
        self.assertGreater(x, 1000)                # bottom-right corner, inside the work area
        self.assertLessEqual(y + card.win.winfo_height(), 1040)

    def test_close_all_verifies_first_and_closes_what_stayed_idle(self):
        ts = [T(1), T(2), T(3)]
        card = self.make(ts)
        card.close_btn.invoke()
        self.wait_for(lambda: self.closer.called)
        self.verify.assert_called_once_with(ts)
        self.closer.assert_called_once_with(ts[1:])  # the one that woke up stays
        self.wait_for(lambda: "✓" in card.status.get())
        self.assertIn("⏸", card.status.get())

    def test_not_now_and_x_close_nothing(self):
        gone = mock.Mock()
        card = self.make([T(1)], on_gone=gone)
        card.later_btn.invoke()
        self.settle(0.2)
        self.assertTrue(card.gone)
        gone.assert_called_once()
        self.verify.assert_not_called()
        self.closer.assert_not_called()

    def test_choose_opens_the_checklist_without_closing(self):
        card = self.make([T(1, "claude", detail="שיחה  ·  שקט"), T(2)])
        with mock.patch("ghost_panel._open_checklist") as checklist:
            card.choose_btn.invoke()
        self.assertTrue(card.gone)
        kw = checklist.call_args.kwargs
        self.assertIs(kw["verify"], self.verify)
        self.assertIs(kw["closer"], self.closer)
        self.assertEqual(sum(len(items) for _t, items in kw["sections"]), 2)
        self.closer.assert_not_called()


class Timer(_TkTest):
    def test_a_check_with_findings_only_shows_a_card(self):
        found = [T(1), T(2)]
        cards, shown = [], []
        closer = mock.Mock()

        def card(root, targets):
            c = mock.Mock(gone=False)
            cards.append(targets)
            shown.append(c)
            return c

        with mock.patch.object(cleanup_offer, "settings", return_value=(True, 3.0)), \
             mock.patch("idle_cleanup.close_targets", closer):
            timer = cleanup_offer.OfferTimer(self.root, find=lambda: found, card=card, first_delay_sec=0)
            self.addCleanup(timer.stop)
            self.wait_for(lambda: cards)
            timer.check()                          # a card is still up: no second one
            timer._found(found)                    # nor from a search already under way
            self.settle(0.4)
        self.assertEqual(cards, [found])
        closer.assert_not_called()
        for name in ("close_all", "choose"):       # the card waits for the user's click
            getattr(shown[0], name).assert_not_called()

    def test_nothing_found_or_switched_off_shows_nothing(self):
        for enabled, found in ((True, []), (False, [T(1)])):
            with self.subTest(enabled=enabled):
                card = mock.Mock()
                find = mock.Mock(return_value=found)
                with mock.patch.object(cleanup_offer, "settings", return_value=(enabled, 3.0)):
                    timer = cleanup_offer.OfferTimer(self.root, find=find, card=card, first_delay_sec=0)
                    self.settle(0.5)
                    timer.stop()
                card.assert_not_called()
                self.assertEqual(find.called, enabled)

    def test_the_next_check_is_hours_away(self):
        with mock.patch.object(cleanup_offer, "settings", return_value=(False, 3.0)), \
             mock.patch.object(self.root, "after", return_value="job") as after:
            cleanup_offer.OfferTimer(self.root, find=mock.Mock(), card=mock.Mock()).check()
        delays = [c.args[0] for c in after.call_args_list]
        self.assertEqual(delays, [cleanup_offer.FIRST_CHECK_SEC * 1000, 3 * 3600 * 1000])


class NeverClosesOnItsOwn(unittest.TestCase):
    """The project rule: every close is the user's click. Only OfferCard's
    close_all and the checklist it opens may reach a closer."""

    def test_only_close_all_calls_the_closer(self):
        import ast
        tree = ast.parse((HERE / "cleanup_offer.py").read_text(encoding="utf-8"))
        callers = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Attribute) and node.attr == "_closer":
                        callers.add(fn.name)
                    if isinstance(node, ast.Name) and node.id in ("close_targets", "terminate_process"):
                        callers.add(fn.name)
        self.assertEqual(callers, {"__init__", "close_all", "work", "choose", "_default_close"})


if __name__ == "__main__":
    unittest.main()
