"""
Network throughput for the dock and the vault window, read off the Tk thread.

psutil.net_io_counters() asks Windows for the counters of every network
interface. Measured on the owner's machine (2026-11-03) that took a median
12 ms and up to 100 ms, and on 2026-10-25 a single call held the strip frozen
for 48 s (history/freeze.log). The dock's tick() runs on the Tk main thread,
so the read happens here instead, on one daemon thread.

THREADING CONTRACT — same as temperature_readings: `read_net_rates()` never
blocks. It returns the rates from the last finished read and wakes the reader
for the next one. Reads happen only while someone asks, at most one per
MIN_INTERVAL_SEC however many windows ask, so the dock and the vault window
share them. A read that hangs makes the answer stale: after STALE_SEC the
caller gets None and shows "…" rather than a number that is no longer true.
"""

from __future__ import annotations

import logging
import threading
import time

import psutil

log = logging.getLogger(__name__)

# At most one read this often. The dock asks every second, the vault window
# every two; both get the same reading.
MIN_INTERVAL_SEC = 0.5
# Older than this, the rates are not shown. Well above both windows' intervals.
STALE_SEC = 5.0
# Two reads further apart than this (a hung read, a sleeping machine) give an
# average over minutes, not the current rate, so they publish nothing.
MAX_GAP_SEC = 10.0


class NetReader:
    """Upload and download rates in bytes per second, from consecutive reads.

    `latest` is read by UI threads without a lock (a single attribute read of
    an immutable tuple, atomic under CPython). Everything else is touched only
    by the reader thread.
    """

    def __init__(self, counters=None, clock=time.monotonic, background: bool = True) -> None:
        self._counters = counters or psutil.net_io_counters
        self._clock = clock
        self._background = background      # False: no thread, the caller drives read_once()
        self._prev: tuple[float, int, int] | None = None
        self.latest: tuple[float, float, float] | None = None   # (when, up, down)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    def rates(self) -> tuple[float, float] | None:
        """(up, down) in bytes per second, or None while unknown. Non-blocking."""
        if self._background:
            self._ensure_thread()
            self._wake.set()
        latest = self.latest
        if latest is None or self._clock() - latest[0] > STALE_SEC:
            return None
        return latest[1], latest[2]

    def read_once(self) -> None:
        """One read and, from the read before it, new rates. Blocks: never
        call it on the Tk thread."""
        try:
            io = self._counters()
        except Exception:
            log.debug("Reading the network counters failed", exc_info=True)
            return
        if io is None:                     # psutil: no network interfaces at all
            return
        now = self._clock()
        prev, self._prev = self._prev, (now, io.bytes_sent, io.bytes_recv)
        if prev is None:
            return
        dt = now - prev[0]
        if not 0.05 <= dt <= MAX_GAP_SEC:
            return
        # A counter that went backwards was reset (an adapter came back up).
        self.latest = (now, max(0.0, (io.bytes_sent - prev[1]) / dt),
                       max(0.0, (io.bytes_recv - prev[2]) / dt))

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="net-reader", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            if self._stop.is_set():
                return
            self._wake.clear()
            self.read_once()
            if self._stop.wait(MIN_INTERVAL_SEC):
                return


_default = NetReader()


def read_net_rates() -> tuple[float, float] | None:
    """(upload, download) in bytes per second, or None while unknown.

    Non-blocking: see the module docstring."""
    return _default.rates()
