"""Tests for the Claude & Telegram setup window. No network (the checks are
fakes), no real credential store (conftest.py), no real clipboard and no real
config.json (both faked here)."""
from __future__ import annotations

import time
import tkinter as tk
import unittest
from unittest import mock

import i18n
import secrets_store

KEY = "sk-ant-api03-" + "a" * 24 + "WXYZ"
TOKEN = "123456789:AAH" + "x" * 32


class SetupWindowTest(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:                   # no display (CI without Xvfb)
            self.skipTest(f"no display: {e}")
        self.root.withdraw()
        i18n.set_language("he")
        self.clipboard = {"text": ""}
        self.cfg = {"telegram": {"chat_id": None, "chat_name": None}, "claude": {"monthly_usd": 5.0}}
        patches = [
            mock.patch.object(tk.Misc, "clipboard_get", lambda _w, **_k: self.clipboard["text"]),
            mock.patch.object(tk.Misc, "clipboard_clear", lambda _w, **_k: self.clipboard.update(text="")),
            mock.patch("config.load_config", side_effect=lambda *_a: self.cfg),
            mock.patch("config.save_config", side_effect=lambda cfg, *_a: self.cfg.update(cfg)),
            mock.patch("webbrowser.open"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        import claude_setup
        w = claude_setup._current["win"]
        if w is not None:
            w.close()
        self.root.destroy()

    def _open(self, on_change=None):
        import claude_setup
        claude_setup.open_setup(self.root, on_change)
        self.root.update()                         # mapped: only then do clicks arrive
        return claude_setup._current["win"]

    def _labels(self, w):
        for c in w.winfo_children():
            if isinstance(c, tk.Label):
                yield c
            yield from self._labels(c)

    def _text(self, frame):
        return " ".join(lab.cget("text") for lab in self._labels(frame))

    def _click(self, w, text):
        # The exact label: the steps above the button quote its name too.
        button = next(lab for lab in self._labels(w.win) if lab.cget("text") == i18n.bidi(text))
        button.event_generate("<ButtonRelease-1>")
        self.root.update()

    def _wait_for(self, cond, timeout=5.0):
        end = time.monotonic() + timeout
        while not cond():
            self.assertLess(time.monotonic(), end, "timed out")
            self.root.update()
            time.sleep(0.02)

    def test_a_working_key_is_saved_and_forgotten_everywhere_else(self):
        changed = mock.Mock()
        w = self._open(changed)
        self.assertEqual(w.key_entry.cget("show"), "•")          # never shown on screen
        self.clipboard["text"] = KEY + "\n"
        w._paste(w.key_entry)
        with mock.patch("ai_advisor.check_key", return_value=(True, "")) as check:
            self._click(w, "שמור ובדוק")
            self._wait_for(lambda: secrets_store.load(secrets_store.ANTHROPIC_KEY) == KEY)
        check.assert_called_once_with(KEY)
        self.assertEqual(w.key_entry.get(), "")                  # the field is empty
        self.assertEqual(self.clipboard["text"], "")             # and so is the clipboard
        changed.assert_called_once()
        self.assertIn("…WXYZ", self._text(w.key_status))
        self.assertNotIn(KEY, " ".join(lab.cget("text") for lab in self._labels(w.win)))

    def test_a_rejected_key_is_not_saved(self):
        w = self._open()
        w.key_entry.insert(0, KEY)
        with mock.patch("ai_advisor.check_key", return_value=(False, "Anthropic דחה את המפתח.")):
            self._click(w, "שמור ובדוק")
            self._wait_for(lambda: "דחה" in self._text(w.key_msg))
        self.assertIsNone(secrets_store.load(secrets_store.ANTHROPIC_KEY))
        self.assertEqual(w.key_entry.get(), KEY)                 # still there, to fix

    def test_deleting_asks_first(self):
        import claude_setup
        secrets_store.save(secrets_store.ANTHROPIC_KEY, KEY)
        w = self._open()
        answers = []
        with mock.patch.object(claude_setup, "confirm", side_effect=lambda *a, **k: answers.append(a[4])):
            self._click(w, "מחק מפתח")
        self.assertEqual(secrets_store.load(secrets_store.ANTHROPIC_KEY), KEY)
        answers[0](True)
        self.assertIsNone(secrets_store.load(secrets_store.ANTHROPIC_KEY))

    def test_telegram_connects_only_after_the_user_confirms_the_chat(self):
        import claude_setup
        w = self._open()
        w.tg_entry.insert(0, TOKEN)
        answers = []
        with mock.patch("telegram_notify.bot_name", return_value="@pd_bot"), \
             mock.patch("telegram_notify.find_chat", return_value=(7, "Aviv")), \
             mock.patch("telegram_notify.send") as send, \
             mock.patch.object(claude_setup, "confirm", side_effect=lambda *a, **k: answers.append(a)):
            self._click(w, "חבר ושלח בדיקה")
            self._wait_for(lambda: answers)
            self.assertIn("Aviv", answers[0][2])                # the name, to confirm
            self.assertIsNone(self.cfg["telegram"]["chat_id"])   # nothing chosen yet
            answers[0][4](True)
            self._wait_for(lambda: send.called and "נשלחה" in self._text(w.tg_msg))
        self.assertEqual(secrets_store.load(secrets_store.TELEGRAM_TOKEN), TOKEN)
        self.assertEqual(self.cfg["telegram"], {"chat_id": 7, "chat_name": "Aviv"})
        token, chat_id, text = send.call_args[0]
        self.assertEqual((token, chat_id), (TOKEN, 7))
        self.assertIn("החיבור הצליח", text)
        self.assertIn("Aviv", self._text(w.tg_status))

    def test_no_chat_yet_says_to_send_start(self):
        w = self._open()
        w.tg_entry.insert(0, TOKEN)
        with mock.patch("telegram_notify.bot_name", return_value="@pd_bot"), \
             mock.patch("telegram_notify.find_chat", return_value=None):
            self._click(w, "חבר ושלח בדיקה")
            self._wait_for(lambda: "/start" in self._text(w.tg_msg))
        self.assertEqual(secrets_store.load(secrets_store.TELEGRAM_TOKEN), TOKEN)   # no need to paste again

    def test_the_ask_window_asks_by_itself_once_a_key_is_saved(self):
        import os

        import ai_advisor
        import claude_panel
        import claude_setup
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(ai_advisor, "load_env"), \
             mock.patch.object(ai_advisor, "ask_claude", return_value="הכול תקין.") as ask, \
             mock.patch("load_actions.heavy_programs", return_value=([], "history")):
            panel = claude_panel.open_claude_panel(self.root, lambda: {})
            self.root.update()
            button = next(lab for lab in self._labels(panel) if "חבר מפתח" in lab.cget("text"))
            button.event_generate("<Button-1>")
            self.root.update()
            w = claude_setup._current["win"]
            self.assertIsNotNone(w)
            ask.assert_not_called()
            w.key_entry.insert(0, KEY)
            with mock.patch("ai_advisor.check_key", return_value=(True, "")):
                self._click(w, "שמור ובדוק")
                self._wait_for(lambda: ask.called)
        self.assertFalse(any("חבר מפתח" in lab.cget("text") for lab in self._labels(panel)))
        panel.destroy()

    def test_one_window_at_a_time(self):
        import claude_setup
        first = self._open()
        claude_setup.open_setup(self.root)
        self.assertIs(claude_setup._current["win"], first)


if __name__ == "__main__":
    unittest.main()
