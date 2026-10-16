"""Tests for the two-language UI helper."""
from __future__ import annotations

import unittest
from unittest import mock

import i18n


class I18nTest(unittest.TestCase):
    def tearDown(self) -> None:
        # Other test modules assert Hebrew text; leave the language as they expect.
        i18n.set_language("he")

    def test_tr_picks_active_language(self) -> None:
        i18n.set_language("he")
        self.assertEqual(i18n.tr("שלום", "Hello"), "שלום")
        i18n.set_language("en")
        self.assertEqual(i18n.tr("שלום", "Hello"), "Hello")

    def test_layout_helpers_follow_direction(self) -> None:
        i18n.set_language("he")
        self.assertEqual((i18n.ANCHOR(), i18n.JUSTIFY(), i18n.SIDE(), i18n.SIDE_END()),
                         ("e", "right", "right", "left"))
        i18n.set_language("en")
        self.assertEqual((i18n.ANCHOR(), i18n.JUSTIFY(), i18n.SIDE(), i18n.SIDE_END()),
                         ("w", "left", "left", "right"))

    def test_config_choice_wins_over_system(self) -> None:
        with mock.patch("config.load_config", return_value={"ui": {"language": "en"}}), \
             mock.patch.object(i18n, "_system_language", return_value="he"):
            i18n.set_language(None)
            self.assertEqual(i18n.language(), "en")

    def test_auto_follows_system(self) -> None:
        with mock.patch("config.load_config", return_value={"ui": {"language": "auto"}}), \
             mock.patch.object(i18n, "_system_language", return_value="en"):
            i18n.set_language(None)
            self.assertEqual(i18n.language(), "en")

    def test_unknown_value_falls_back_to_system(self) -> None:
        with mock.patch("config.load_config", return_value={"ui": {"language": "fr"}}), \
             mock.patch.object(i18n, "_system_language", return_value="he"):
            i18n.set_language(None)
            self.assertEqual(i18n.language(), "he")

    def test_unreadable_config_falls_back_to_system(self) -> None:
        with mock.patch("config.load_config", side_effect=OSError("boom")), \
             mock.patch.object(i18n, "_system_language", return_value="en"):
            i18n.set_language(None)
            self.assertEqual(i18n.language(), "en")

    def test_bidi_embeds_hebrew_lines_only(self) -> None:
        # Windows draws Tk text left to right; the embedding makes a mixed
        # Hebrew line read right to left. English is left exactly as it was.
        i18n.set_language("he")
        self.assertEqual(i18n.bidi("01:57 · מעבד 82%"), "‫01:57 · מעבד 82%‬")
        self.assertEqual(i18n.bidi(""), "")
        i18n.set_language("en")
        self.assertEqual(i18n.bidi("01:57 · CPU 82%"), "01:57 · CPU 82%")

    def test_set_language_rejects_unsupported(self) -> None:
        with mock.patch.object(i18n, "_resolve", return_value="en"):
            i18n.set_language("fr")
            self.assertEqual(i18n.language(), "en")


if __name__ == "__main__":
    unittest.main()
