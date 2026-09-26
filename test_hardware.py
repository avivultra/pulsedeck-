"""Tests for machine-feature detection."""
from __future__ import annotations

import unittest
from unittest import mock

import hardware


class FanButtonTest(unittest.TestCase):
    def setUp(self) -> None:
        hardware.lenovo_cooling_available.cache_clear()
        hardware.system_manufacturer.cache_clear()

    tearDown = setUp

    def test_config_true_or_false_wins(self) -> None:
        with mock.patch.object(hardware, "lenovo_cooling_available", return_value=False):
            self.assertTrue(hardware.fan_button_enabled({"hardware": {"fan_button": True}}))
        with mock.patch.object(hardware, "lenovo_cooling_available", return_value=True):
            self.assertFalse(hardware.fan_button_enabled({"hardware": {"fan_button": False}}))

    def test_auto_uses_detection(self) -> None:
        with mock.patch.object(hardware, "lenovo_cooling_available", return_value=True):
            self.assertTrue(hardware.fan_button_enabled({}))
            self.assertTrue(hardware.fan_button_enabled(None))
        with mock.patch.object(hardware, "lenovo_cooling_available", return_value=False):
            self.assertFalse(hardware.fan_button_enabled({"hardware": {"fan_button": "auto"}}))

    @mock.patch.object(hardware.os, "name", "nt")
    def test_non_lenovo_never_gets_the_button(self) -> None:
        with mock.patch.object(hardware, "system_manufacturer", return_value="dell inc."), \
             mock.patch.object(hardware, "_nerve_center_running", return_value=True):
            self.assertFalse(hardware.lenovo_cooling_available())

    @mock.patch.object(hardware.os, "name", "nt")
    def test_lenovo_needs_nerve_center(self) -> None:
        with mock.patch.object(hardware, "system_manufacturer", return_value="lenovo"), \
             mock.patch.object(hardware, "_nerve_center_running", return_value=False), \
             mock.patch.object(hardware, "_nerve_center_installed", return_value=False):
            self.assertFalse(hardware.lenovo_cooling_available())
        hardware.lenovo_cooling_available.cache_clear()
        with mock.patch.object(hardware, "system_manufacturer", return_value="lenovo"), \
             mock.patch.object(hardware, "_nerve_center_running", return_value=True):
            self.assertTrue(hardware.lenovo_cooling_available())


if __name__ == "__main__":
    unittest.main()
