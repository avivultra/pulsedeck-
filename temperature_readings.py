"""
Best-effort CPU / system temperature in Celsius.

- Linux / many laptops: psutil.sensors_temperatures()
- Windows: psutil is often empty; optional WMI MSAcpi_ThermalZoneTemperature
  (not present on all PCs). Results are cached to limit subprocess cost.
"""

from __future__ import annotations

import os
import subprocess
import time

import psutil

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
    """Representative temperature for logging / UI, or None if unknown."""
    global _cache_value, _last_read_mono
    now = time.monotonic()
    if _last_read_mono is not None and (now - _last_read_mono) < _CACHE_TTL_SEC:
        return _cache_value

    t = _from_psutil()
    if t is None and os.name == "nt":
        t = _from_windows_wmi()
    _cache_value = t
    _last_read_mono = now
    return t
