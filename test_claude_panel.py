"""Tests for how the Ask Claude window shows an answer: plain text, and
Hebrew lines that keep their order on Windows. Claude is mocked."""
from __future__ import annotations

import time
import unittest
from unittest import mock

import i18n
import claude_panel

ANSWER = ("**המחשב לא עמוס כרגע:** המעבד עובד ב-6.5% בלבד, והזיכרון תפוס ב-71%, כך שעדיין נשאר מקום.\n\n"
          "## מה אפשר לעשות\n"
          "- Claude פותח הרבה תהליכים: אצלך פתוחים 13 חלונות Claude, וגם PowerShell לכל אחד מהם, "
          "והם תופסים יחד כ-8.2GB מתוך הזיכרון שבשימוש.\n"
          "1. אם יש שיחות שכבר לא צריך, אפשר לסגור אותן מתוך אפליקציית Claude.")


class PlainTextTest(unittest.TestCase):
    def test_markdown_marks_are_dropped(self):
        text = claude_panel._plain(ANSWER)
        self.assertNotIn("**", text)
        self.assertNotIn("#", text)
        self.assertIn("המחשב לא עמוס כרגע: המעבד", text)
        self.assertIn("\n• Claude פותח", text)
        self.assertIn("\n1. אם יש", text)          # numbered lines stay

    def test_english_ui_leaves_wrapping_to_tk(self):
        i18n.set_language("en")
        self.assertEqual(claude_panel._lay_out("CPU is fine.\nNothing to do.", ("Segoe UI", 10), 50),
                         "CPU is fine.\nNothing to do.")


class SnapshotNowTest(unittest.TestCase):
    """The strip's menu has no window data of its own: the snapshot comes
    from the running sampler, folded by name, without starting anything."""

    def test_folds_the_running_sampler_by_name(self):
        from types import SimpleNamespace as NS

        import ai_advisor
        gb = 1024 ** 3
        procs = [NS(name="chrome.exe", cpu_percent=3.0, rss_bytes=gb), NS(name="chrome.exe", cpu_percent=1.0,
                 rss_bytes=gb), NS(name="node.exe", cpu_percent=0.5, rss_bytes=gb // 2)]
        snap = NS(cpu_percent=12.0, cpu_logical=8, ram_percent=71.0, ram_used=11 * gb, ram_total=16 * gb,
                  disk_percent=50.0, swap_percent=None, temp_celsius=None, battery_plugged=True)
        with mock.patch("process_monitor.peek_default_sampler", return_value=NS(snapshot=lambda: procs)), \
             mock.patch("process_monitor.get_default_sampler") as start, \
             mock.patch("monitor.collect_snapshot", return_value=snap), \
             mock.patch("live_chart._read_today_spikes", return_value=[]), \
             mock.patch("temperature_readings.read_gpu_temp_celsius", return_value=None):
            out = ai_advisor.snapshot_now("C:\\")
        start.assert_not_called()
        self.assertEqual(out["now"]["ram_percent"], 71.0)
        self.assertEqual(out["top_memory"][0], {"name": "chrome.exe", "memory_mb": 2048, "count": 2})


class AnswerLayoutTest(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"no display: {e}")
        self.root.withdraw()
        i18n.set_language("he")

    def tearDown(self):
        self.root.destroy()

    def _wait_for(self, cond, timeout=5.0):
        end = time.monotonic() + timeout
        while not cond():
            self.assertLess(time.monotonic(), end, "timed out")
            self.root.update()
            time.sleep(0.02)

    def test_every_hebrew_line_is_embedded_and_fits(self):
        import tkinter as tk
        from tkinter import font as tkfont

        import ai_advisor
        import load_actions
        with mock.patch.object(ai_advisor, "is_configured", return_value=True), \
             mock.patch.object(ai_advisor, "ask_claude", return_value=ANSWER), \
             mock.patch.object(load_actions, "context_for_claude", return_value={}, create=True), \
             mock.patch.object(load_actions, "heavy_programs", return_value=([], "history")):
            win = claude_panel.open_claude_panel(self.root, lambda: {})
            body = next(w for w in win.winfo_children() if isinstance(w, tk.Text))
            self._wait_for(lambda: "המחשב" in body.get("1.0", "end"))
            self.root.update()
            lines = body.get("1.0", "end-1c").split("\n")
            width = body.winfo_width()
            measure = tkfont.Font(font=body.cget("font")).measure
            self.assertNotIn("**", "\n".join(lines))
            hebrew = [ln for ln in lines if claude_panel._has_hebrew(ln)]
            self.assertGreater(len(hebrew), len(ANSWER.split("\n")))      # long lines were broken
            for ln in hebrew:
                self.assertTrue(ln.startswith(i18n.RLE) and ln.endswith(i18n.PDF), ln)
                self.assertLess(measure(ln), width - 2 * 14, ln)
            win.destroy()


if __name__ == "__main__":
    unittest.main()
