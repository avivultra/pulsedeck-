"""Tests for the encrypted key store. The real Windows Credential Manager is
never touched: conftest.py gives every test an empty in-memory store."""
from __future__ import annotations

import unittest
from unittest import mock

import secrets_store


class SecretsStoreTest(unittest.TestCase):
    def test_save_load_delete(self):
        name = secrets_store.ANTHROPIC_KEY
        self.assertIsNone(secrets_store.load(name))
        secrets_store.save(name, "  sk-ant-api03-abcdefghijklmnop \n")    # pasted with spaces
        self.assertEqual(secrets_store.load(name), "sk-ant-api03-abcdefghijklmnop")
        self.assertTrue(secrets_store.delete(name))
        self.assertIsNone(secrets_store.load(name))
        self.assertFalse(secrets_store.delete(name))

    def test_what_cannot_be_a_key_is_refused(self):
        for bad in ("", "   ", "sk-ant one", "x" * (secrets_store.MAX_CHARS + 1)):
            with self.subTest(bad=bad[:20]), self.assertRaises(secrets_store.SecretStoreError):
                secrets_store.save(secrets_store.ANTHROPIC_KEY, bad)
        self.assertIsNone(secrets_store.load(secrets_store.ANTHROPIC_KEY))

    def test_without_a_store_nothing_is_saved_and_nothing_breaks(self):
        with mock.patch.object(secrets_store, "_backend", None):
            self.assertFalse(secrets_store.available())
            self.assertIsNone(secrets_store.load(secrets_store.ANTHROPIC_KEY))
            with self.assertRaises(secrets_store.SecretStoreError):
                secrets_store.save(secrets_store.ANTHROPIC_KEY, "sk-ant-x")
            self.assertFalse(secrets_store.delete(secrets_store.ANTHROPIC_KEY))

    def test_a_failing_store_never_shows_the_value(self):
        broken = mock.Mock()
        broken.write.side_effect = OSError(5, "denied sk-ant-secret-value")
        broken.read.side_effect = OSError(5, "denied")
        with mock.patch.object(secrets_store, "_backend", broken), \
             self.assertLogs("secrets_store", "DEBUG") as logs:
            with self.assertRaises(secrets_store.SecretStoreError) as ctx:
                secrets_store.save(secrets_store.ANTHROPIC_KEY, "sk-ant-secret-value")
            self.assertIsNone(secrets_store.load(secrets_store.ANTHROPIC_KEY))
        self.assertNotIn("sk-ant-secret-value", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertNotIn("sk-ant-secret-value", "\n".join(logs.output))

    def test_hint_shows_only_the_last_four(self):
        self.assertEqual(secrets_store.hint("sk-ant-api03-abcdWXYZ"), "…WXYZ")
        self.assertEqual(secrets_store.hint("short"), "")
        self.assertEqual(secrets_store.hint(None), "")


if __name__ == "__main__":
    unittest.main()
