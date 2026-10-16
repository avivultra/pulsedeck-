"""PulseDeck's own footprint: how much CPU and memory the monitor itself uses.

A monitor that quietly eats the machine it watches defeats its purpose, so the
UI shows this number openly. Reading it is two cheap syscalls on our own
process — no scan, no thread.
"""

from __future__ import annotations

import os
import threading
import time

import psutil

_lock = threading.Lock()
_proc: psutil.Process | None = None
_last: tuple[float, float] | None = None   # (wall time, cpu seconds used)
_last_pct = 0.0
# CPU time is counted in OS ticks (~15 ms on Windows); over a shorter window
# the ratio is noise, so calls closer together than this reuse the last value.
_MIN_WINDOW_SEC = 0.5


def self_usage() -> tuple[float, int]:
    """(CPU % of the whole machine, resident memory in bytes).

    CPU is averaged since the previous call; the first call averages over the
    process lifetime. 100 % means every core busy, like Task Manager.
    """
    global _proc, _last, _last_pct
    with _lock:
        if _proc is None:
            _proc = psutil.Process(os.getpid())
        now = time.time()
        rss = _proc.memory_info().rss
        if _last is not None and now - _last[0] < _MIN_WINDOW_SEC:
            return _last_pct, rss
        ct = _proc.cpu_times()
        used = ct.user + ct.system
        if _last is None:
            since, used_before = _proc.create_time(), 0.0
        else:
            since, used_before = _last
        _last = (now, used)
        elapsed = now - since
        cores = psutil.cpu_count(logical=True) or 1
        pct = 0.0 if elapsed <= 0 else 100.0 * (used - used_before) / elapsed / cores
        _last_pct = min(100.0, max(0.0, pct))
        return _last_pct, rss


def format_self_usage(pct: float, rss: int) -> str:
    return f"CPU {pct:.1f}% · RAM {rss / (1024 * 1024):.0f} MB"
