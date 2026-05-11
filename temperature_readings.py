"""
Best-effort CPU / system temperature in Celsius.

- Linux / many laptops: psutil.sensors_temperatures()
- Windows: optional NVIDIA GPU temp via `nvidia-smi` when installed (see `read_gpu_temp_celsius`).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

import psutil

log = logging.getLogger(__name__)

_CACHE_TTL_SEC = 8.0
_cache_value: float | None = None
_last_read_mono: float | None = None


def _from_psutil() -> float | None:
    try:
        data = psutil.sensors_temperatures()
    except (AttributeError, NotImplementedError, OSError):
        return None
    if not data:
        return None
    candidates: list[tuple[float, str, str]] = []
    for chip, entries in data.items():
        for e in entries:
            if e.current is None:
                continue
            label = (e.label or "").lower()
            candidates.append((float(e.current), chip.lower(), label))

    if not candidates:
        return None

    for temp, chip, label in candidates:
        blob = f"{chip} {label}"
        if any(
            k in blob
            for k in (
                "package",
                "edge",
                "tdie",
                "tctl",
                "cpu",
                "core",
                "k10temp",
                "zenpower",
            )
        ):
            return temp

    return max(candidates, key=lambda x: x[0])[0]


def _from_windows_wmi() -> float | None:
    if os.name != "nt":
        return None
    ps = (
        "$m = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
        "-ErrorAction SilentlyContinue; "
        "if (-not $m) { exit 2 }; "
        "($m | ForEach-Object { ($_.CurrentTemperature / 10.0) - 273.15 }) "
        "| Measure-Object -Maximum | Select-Object -ExpandProperty Maximum"
    )
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        val = float(text.splitlines()[-1])
    except ValueError:
        return None
    if val < -40 or val > 150:
        return None
    return val


def read_primary_temp_celsius() -> float | None:
    """Representative CPU temperature for logging / UI, or None if unknown.

    Routes through the cpu_probes chain (psutil → Windows WMI → Linux
    sysfs → macOS). Result is cached for `_CACHE_TTL_SEC` to avoid hammering
    sensors / spawning powershell every tick.
    """
    global _cache_value, _last_read_mono
    now = time.monotonic()
    if _last_read_mono is not None and (now - _last_read_mono) < _CACHE_TTL_SEC:
        return _cache_value

    try:
        from cpu_probes import read_cpu_temperature_celsius
        t = read_cpu_temperature_celsius()
    except Exception:
        log.exception("CPU temperature probe chain failed; falling back to None")
        t = None
    _cache_value = t
    _last_read_mono = now
    return t


# GPU readings — shared cache across temp + memory so the probe chain runs
# at most once every _GPU_CACHE_TTL seconds even if both functions are called.
_GPU_READING_CACHE = None       # gpu_probes.GPUReading | None
_GPU_READING_MONO: float | None = None
_GPU_CACHE_TTL = 5.0


def _get_cached_gpu_reading():
    """Return a recent GPUReading from the probe chain, or None.

    The chain is `gpu_probes.GPU_PROBES` — NVIDIA first, then AMD, Intel,
    then Linux sysfs. Each probe is cheap to *skip* (single `which()`),
    so machines with NVIDIA keep their original behaviour: NvidiaSmiProbe
    succeeds and the rest are never tried.
    """
    global _GPU_READING_CACHE, _GPU_READING_MONO
    now = time.monotonic()
    if _GPU_READING_MONO is not None and (now - _GPU_READING_MONO) < _GPU_CACHE_TTL:
        return _GPU_READING_CACHE
    try:
        from gpu_probes import read_gpu
        _GPU_READING_CACHE = read_gpu()
    except Exception:
        log.exception("GPU probe chain raised; caching None")
        _GPU_READING_CACHE = None
    _GPU_READING_MONO = now
    return _GPU_READING_CACHE


def read_gpu_temp_celsius() -> float | None:
    """GPU die temperature, vendor-agnostic. Returns None if no probe succeeds.

    Tries NVIDIA → AMD → Intel → Linux sysfs in order. Caches the underlying
    reading for 5 seconds.
    """
    reading = _get_cached_gpu_reading()
    return reading.temp_celsius if reading is not None else None


def read_gpu_memory_mib() -> tuple[int, int] | None:
    """GPU VRAM (used, total) in MiB, vendor-agnostic. None if not available.

    Returns None if the active probe didn't report memory (e.g. the Linux
    sysfs fallback only provides temperature).
    """
    reading = _get_cached_gpu_reading()
    if reading is None or reading.mem_total_mib is None:
        return None
    return (reading.mem_used_mib or 0, reading.mem_total_mib)
