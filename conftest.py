"""Shared pytest setup.

Tk objects must be freed on the thread that made them. A widget one test left
in a reference cycle is otherwise freed by whichever thread next runs the
garbage collector — here, the sensor-refresher thread that
test_ghost_sweeper starts — and Tcl aborts the whole run ("Windows fatal
exception: code 0x80000003"). Collecting after every test, on the main
thread, frees them in the right place.
"""

import gc

import pytest


@pytest.fixture(autouse=True)
def _collect_on_the_main_thread():
    yield
    gc.collect()
