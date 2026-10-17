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
key lives: each one gets an empty in-memory store.
"""

import gc

import pytest

import i18n
import secrets_store


@pytest.fixture(autouse=True)
def _start_in_hebrew():
    i18n.set_language("he")
    yield


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch):
    monkeypatch.setattr(secrets_store, "_backend", secrets_store.MemoryStore())
    yield


@pytest.fixture(autouse=True)
def _collect_on_the_main_thread():
    yield
    gc.collect()
