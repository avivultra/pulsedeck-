"""
Best-effort CPU / system temperature in Celsius, plus GPU temp/VRAM.

THREADING CONTRACT — read this before changing anything here.

Every public reader in this module (`read_primary_temp_celsius`,
`read_gpu_temp_celsius`, `read_gpu_memory_mib`) is **non-blocking**: it
returns whatever is in the cache and returns immediately. It never spawns
a subprocess, never touches WMI, never waits on I/O.

The actual sensor reads — which DO spawn `powershell` / `nvidia-smi` and can
take hundreds of milliseconds (or hit a 4 s timeout) — happen on a single
daemon refresher thread. This matters because the dock's `tick()` runs on the
Tk main thread: any blocking call there freezes the whole UI, including
dragging, the right-click menu, and the fan button.

Cold start: the first call returns None (UI shows "—") while the refresher
fills the cache in the background. That is deliberate — a one-second "—" is
far better than a one-second freeze.

Failing probes back off: after 3 consecutive failures the retry interval grows
geometrically up to `_MAX_BACKOFF_SEC`. On machines where WMI simply does not
expose MSAcpi_ThermalZoneTemperature (common on laptops), this turns an endless
every-8-seconds PowerShell spawn into a handful of attempts and then silence.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

# Base refresh intervals. Sensors don't change fast enough to justify less.
_CPU_TTL_SEC = 8.0
_GPU_TTL_SEC = 10.0

# Backoff for probes that keep failing: after this many consecutive failures
# the effective TTL doubles per extra failure, capped at _MAX_BACKOFF_SEC.
_FAILURES_BEFORE_BACKOFF = 3
_MAX_BACKOFF_SEC = 900.0          # 15 minutes

# How often the refresher thread wakes up to see if anything is stale.
_REFRESHER_TICK_SEC = 1.0


class _SensorSlot:
    """One cached sensor value + its staleness / backoff bookkeeping.

    `value` is read by UI threads without a lock (single attribute read of an
    immutable object — atomic under CPython). Everything the refresher mutates
    for scheduling lives behind `_lock`.
    """

    def __init__(self, name: str, ttl: float, reader) -> None:
        self.name = name
        self.base_ttl = ttl
        self._reader = reader
        self.value = None
        self._last_attempt_mono: float | None = None
        self._failures = 0
        self._lock = threading.Lock()

    def effective_ttl(self) -> float:
        """TTL after applying failure backoff."""
        if self._failures < _FAILURES_BEFORE_BACKOFF:
            return self.base_ttl
        extra = self._failures - _FAILURES_BEFORE_BACKOFF + 1
        return min(self.base_ttl * (2 ** extra), _MAX_BACKOFF_SEC)

    def is_stale(self, now: float) -> bool:
        with self._lock:
            if self._last_attempt_mono is None:
                return True
            return (now - self._last_attempt_mono) >= self.effective_ttl()

    def refresh(self) -> None:
        """Run the (possibly slow) reader. Called ONLY on the refresher thread."""
        try:
            result = self._reader()
        except Exception:
            log.exception("Sensor %r raised during refresh", self.name)
            result = None

        with self._lock:
            self._last_attempt_mono = time.monotonic()
            if result is None:
                self._failures += 1
                if self._failures == _FAILURES_BEFORE_BACKOFF:
                    log.info(
                        "Sensor %r failed %d times; backing off (next attempts up "
                        "to %.0f s apart). This is normal on hardware that does "
                        "not expose the sensor.",
                        self.name, self._failures, _MAX_BACKOFF_SEC,
                    )
            else:
                self._failures = 0
        # Publish outside the lock — readers never take it.
        self.value = result


# ---------- The blocking readers (run on the refresher thread only) ----------

def _read_cpu_temp_blocking() -> float | None:
    from cpu_probes import read_cpu_temperature_celsius
    return read_cpu_temperature_celsius()


def _read_gpu_blocking():
    from gpu_probes import read_gpu
    return read_gpu()


_cpu_slot = _SensorSlot("cpu_temp", _CPU_TTL_SEC, _read_cpu_temp_blocking)
_gpu_slot = _SensorSlot("gpu", _GPU_TTL_SEC, _read_gpu_blocking)
_ALL_SLOTS = (_cpu_slot, _gpu_slot)


# ---------- Refresher thread ----------

_refresher_thread: threading.Thread | None = None
_refresher_lock = threading.Lock()
_refresher_stop = threading.Event()


def _refresher_loop() -> None:
    while not _refresher_stop.is_set():
        now = time.monotonic()
        for slot in _ALL_SLOTS:
            if _refresher_stop.is_set():
                return
            if slot.is_stale(now):
                slot.refresh()
        if _refresher_stop.wait(_REFRESHER_TICK_SEC):
            return


def _ensure_refresher() -> None:
    """Start the refresher thread once, lazily, on first sensor read."""
    global _refresher_thread
    if _refresher_thread is not None and _refresher_thread.is_alive():
        return
    with _refresher_lock:
        if _refresher_thread is not None and _refresher_thread.is_alive():
            return
        _refresher_stop.clear()
        _refresher_thread = threading.Thread(
            target=_refresher_loop, name="sensor-refresher", daemon=True
        )
        _refresher_thread.start()


def stop_sensor_refresher() -> None:
    """Signal the refresher to exit. Used on shutdown and in tests."""
    _refresher_stop.set()


def refresh_sensors_now() -> None:
    """Synchronously refresh every sensor. For --once / console / tests.

    This DOES block. Never call it from the Tk main thread.
    """
    for slot in _ALL_SLOTS:
        slot.refresh()


# ---------- Public, non-blocking readers ----------

def read_primary_temp_celsius() -> float | None:
    """Representative CPU temperature, or None if unknown/not yet read.

    Non-blocking: returns the cached value. See the module docstring.
    """
    _ensure_refresher()
    return _cpu_slot.value


def read_gpu_temp_celsius() -> float | None:
    """GPU die temperature (vendor-agnostic), or None. Non-blocking."""
    _ensure_refresher()
    reading = _gpu_slot.value
    return reading.temp_celsius if reading is not None else None


def read_gpu_memory_mib() -> tuple[int, int] | None:
    """GPU VRAM (used, total) in MiB, or None. Non-blocking."""
    _ensure_refresher()
    reading = _gpu_slot.value
    if reading is None or reading.mem_total_mib is None:
        return None
    return (reading.mem_used_mib or 0, reading.mem_total_mib)
