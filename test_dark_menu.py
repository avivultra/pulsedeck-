"""Tests for the dark popup menu (dark_menu) and the strip's menu built on it.
Nothing real runs: every action is a mock."""
from __future__ import annotations

import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest import mock

import i18n
from dark_menu import DarkMenu, notice, place

AREA = (0, 0, 1920, 1040)          # a 1080p screen minus a 40 px taskbar


class PlaceTest(unittest.TestCase):
    def test_hebrew_opens_left_of_the_pointer_english_right(self):
        self.assertEqual(place(900, 300, 270, 400, AREA, rtl=True), (630, 300))
        self.assertEqual(place(900, 300, 270, 400, AREA, rtl=False), (900, 300))

    def test_flips_sideways_when_there_is_no_room(self):
        self.assertEqual(place(100, 300, 270, 400, AREA, rtl=True), (100, 300))
        self.assertEqual(place(1800, 300, 270, 400, AREA, rtl=False), (1530, 300))

    def test_opens_upwards_from_the_strip_on_the_taskbar(self):
        x, y = place(900, 1010, 270, 590, AREA, rtl=True)
        self.assertEqual(y, 1010 - 590)

    def test_always_inside_the_work_area(self):
        for px, py in ((0, 0), (1919, 1039), (960, 520), (5, 1030)):
            for rtl in (True, False):
                x, y = place(px, py, 270, 600, AREA, rtl)
                self.assertTrue(0 <= x and x + 270 <= 1920 and 0 <= y and y + 600 <= 1040, (px, py, rtl))


class _TkTest(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:                   # no display (CI without Xvfb)
            self.skipTest(f"no display: {e}")
        self.root.withdraw()
        i18n.set_language("he")

    def tearDown(self):
        i18n.set_language(None)
        self.root.destroy()

    def settle(self):
        self.root.update()

    def show(self, m):
        """Key and click events reach only a window that is on screen and
        has the keyboard focus, as the real popup does."""
        m.win.geometry("+40+40")
        m.win.deiconify()
        m.win.update()
        m.win.focus_force()
        m.win.update()
        m.bind_keys()


class DarkMenuTest(_TkTest):
    def test_a_command_closes_the_menu_then_runs(self):
        m = DarkMenu(self.root)
        cmd = mock.Mock()
        m.add_command("open", cmd)
        m.rows[0].activate()
        self.assertTrue(m.closed)
        cmd.assert_not_called()                    # after the menu is gone
        self.settle()
        cmd.assert_called_once()

    def test_a_switch_toggles_and_stays_open(self):
        m = DarkMenu(self.root)
        flip = mock.Mock()
        m.add_switch("pin", True, flip)
        m.rows[0].activate()
        m.rows[0].activate()
        self.assertEqual(flip.call_count, 2)
        self.assertFalse(m.closed)

    def test_a_stepper_stays_open_and_shows_the_new_value(self):
        m = DarkMenu(self.root)
        size = {"v": 100}
        m.add_stepper("font", lambda: size.update(v=size["v"] - 10), lambda: size.update(v=size["v"] + 10),
                      lambda: f"{size['v']}%")
        self.show(m)
        m._highlight(0)
        m.win.event_generate("<Key>", keysym="plus")
        m.win.event_generate("<Key>", keysym="plus")
        m.win.event_generate("<Key>", keysym="minus")
        self.assertEqual(size["v"], 110)
        shown = [w.cget("text") for w in m.rows[0].widgets if isinstance(w, tk.Label)]
        self.assertIn("110%", shown)
        self.assertFalse(m.closed)

    def test_a_choice_runs_with_its_code_and_closes(self):
        m = DarkMenu(self.root)
        pick = mock.Mock()
        m.add_choice("lang", [("auto", "auto"), ("he", "he"), ("en", "en")], "he", pick)
        self.show(m)
        seg = next(w for w in m.rows[0].widgets if isinstance(w, tk.Label) and w.cget("text") == "en")
        seg.event_generate("<ButtonRelease-1>")
        self.settle()
        pick.assert_called_once_with("en")
        self.assertTrue(m.closed)

    def test_keyboard_skips_headers_groups_and_separators(self):
        m = DarkMenu(self.root)
        a, b = mock.Mock(), mock.Mock()
        m.add_header("PulseDeck", "CPU 1%")
        m.add_group("windows")
        m.add_command("a", a)
        m.add_separator()
        m.add_command("b", b)
        self.show(m)
        for key in ("Down", "Down", "Return"):
            m.win.event_generate(f"<{key}>")
        self.settle()
        a.assert_not_called()
        b.assert_called_once()

    def test_escape_closes_without_running_anything(self):
        m = DarkMenu(self.root)
        cmd = mock.Mock()
        m.add_command("a", cmd)
        self.show(m)
        m.win.event_generate("<Escape>")
        self.settle()
        self.assertTrue(m.closed)
        cmd.assert_not_called()


class NoticeTest(_TkTest):
    def _labels(self, widget):
        for w in widget.winfo_children():
            if isinstance(w, tk.Label):
                yield w
            yield from self._labels(w)

    def test_shows_the_text_and_closes_on_escape(self):
        win = notice(self.root, "PulseDeck", "השפה תתחלף בהפעלה הבאה")
        self.assertIn("השפה תתחלף בהפעלה הבאה", [w.cget("text") for w in self._labels(win)])
        win.event_generate("<Escape>")
        self.settle()
        self.assertFalse(win.winfo_exists())

    def test_each_line_reads_in_its_own_direction(self):
        # Windows lays out a label in the direction of its first letter: an
        # English line inside a Hebrew label got its full stop on the left.
        win = notice(self.root, "PulseDeck", "השפה תתחלף בהפעלה הבאה של PulseDeck.\n"
                                             "The language will change next time.\nC:\\Users\\x\\a.lnk")
        anchors = {w.cget("text"): w.cget("anchor") for w in self._labels(win)}
        self.assertEqual(anchors["השפה תתחלף בהפעלה הבאה של PulseDeck."], "e")
        self.assertEqual(anchors["The language will change next time."], "w")
        self.assertEqual(anchors["C:\\Users\\x\\a.lnk"], "w")
        win.destroy()


class DockMenuTest(_TkTest):
    """The strip's menu changed its look, not its content: every action the
    native menu offered is still one click away and calls the same function."""

    def test_every_action_is_still_there(self):
        from dock_strip import build_dock_menu
        names = ["open_live_chart", "open_alerts", "open_servers", "open_janitor", "open_ghosts",
                 "open_history", "open_spikes", "toggle_pin", "font_smaller", "font_larger",
                 "set_language", "reset_position", "install_shortcut", "exit"]
        a = SimpleNamespace(usage="CPU 0.3% · RAM 98 MB", pinned=True, language="auto",
                            font_text=lambda: "100%", **{n: mock.Mock() for n in names})
        m = build_dock_menu(self.root, a)
        self.assertEqual(len(m.rows), 13)
        # Rows in order: 7 commands, pin switch, font stepper, language choice, 3 commands.
        for i, name in ((0, "open_live_chart"), (1, "open_alerts"), (2, "open_servers"),
                        (3, "open_janitor"), (4, "open_ghosts"), (5, "open_history"), (6, "open_spikes"),
                        (10, "reset_position"), (11, "install_shortcut"), (12, "exit")):
            m.closed = False                       # each run closes it; reuse the rows
            m.rows[i].activate()
            self.settle()
            getattr(a, name).assert_called_once()
        m2 = build_dock_menu(self.root, a)
        m2.rows[7].activate()
        a.toggle_pin.assert_called_once()
        self.show(m2)
        m2._highlight(8)
        m2.win.event_generate("<Key>", keysym="plus")
        m2.win.event_generate("<Key>", keysym="minus")
        a.font_larger.assert_called_once()
        a.font_smaller.assert_called_once()
        seg = next(w for w in m2.rows[9].widgets if isinstance(w, tk.Label) and w.cget("text") == "English")
        seg.event_generate("<ButtonRelease-1>")
        self.settle()
        a.set_language.assert_called_once_with("en")


class WarmUpTest(_TkTest):
    def test_every_icon_of_the_strip_menu_is_warmed_up(self):
        # An icon missing from ICONS costs ~20 ms on the first right-click.
        from dark_menu import ICONS
        from dock_strip import build_dock_menu
        a = SimpleNamespace(usage="", pinned=False, language="auto", font_text=lambda: "100%",
                            **{n: mock.Mock() for n in (
                                "open_live_chart", "open_alerts", "open_servers", "open_janitor",
                                "open_ghosts", "open_history", "open_spikes", "toggle_pin", "font_smaller",
                                "font_larger", "set_language", "reset_position", "install_shortcut", "exit")})
        m = build_dock_menu(self.root, a)
        icons = {r.widgets[1].cget("text") for r in m.rows} - {"Aa"}
        self.assertTrue(icons)
        self.assertEqual(icons - set(ICONS), set())
        m.close()


if __name__ == "__main__":
    unittest.main()
