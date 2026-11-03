"""Shared pytest setup.

Tk objects must be freed on the thread that made them. A widget one test left
in a reference cycle is otherwise freed by whichever thread next runs the
garbage collector — here, the sensor-refresher thread that
test_ghost_sweeper starts — and Tcl aborts the whole run ("Windows fatal
exception: code 0x80000003"). Collecting after every test, on the main
thread, frees them in the right place.

Every test also starts in Hebrew. The test modules set it once, at import,
so a test that switched to "auto" changed the language for every test after
it: Hebrew on the owner's PC, English on GitHub's Windows runners, where
seven sweeper tests then failed.

No test reaches the real Windows Credential Manager, where the owner's API
key lives: each one gets an empty in-memory store. Nor the real spending
ledger and Telegram report state: each test gets its own, in a temp folder.

Every temp folder a test makes lands in that test's tmp_path. The tests are
unittest classes, which cannot take tmp_path as an argument, and many call
tempfile.mkdtemp() without removing the result: each run left dozens of
tmp* folders (spend.json, usage.json, .env...) in %TEMP%, 2,787 by
2026-11-03, slowing the owner's logon. Pointing tempfile at tmp_path lets
pytest delete them, keeping only the last three runs.
"""

import gc
import tempfile

import pytest

import claude_spend
import i18n
import secrets_store
import spend_reports


@pytest.fixture(autouse=True)
def _start_in_hebrew():
    i18n.set_language("he")
    yield


@pytest.fixture(autouse=True)
def _temp_folders_in_tmp_path(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    yield


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch):
    monkeypatch.setattr(secrets_store, "_backend", secrets_store.MemoryStore())
    yield


@pytest.fixture(autouse=True)
def _no_real_spending(monkeypatch, tmp_path):
    ledger = claude_spend.Ledger(tmp_path / "claude_spend.json", cap=lambda: 5.0)
    monkeypatch.setattr(claude_spend, "_ledger", ledger)
    monkeypatch.setattr(spend_reports, "_reporter", spend_reports.Reporter(
        ledger, send=lambda _text: False, connected=lambda: False,
        state_path=tmp_path / "telegram_reports.json", background=False))
    yield


@pytest.fixture(autouse=True)
def _collect_on_the_main_thread():
    yield
    gc.collect()
