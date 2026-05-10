"""
Best-effort CPU / system temperature in Celsius.

- Linux / many laptops: psutil.sensors_temperatures()
- Windows: optional NVIDIA GPU temp via `nvidia-smi` when installed (see `read_gpu_temp_celsius`).
"""

from __future__ import annotations

import os
import shutil
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


_GPU_CACHE_VAL: float | None = None
_GPU_CACHE_MONO: float | None = None
_GPU_CACHE_TTL = 15.0

_GPU_MEM_CACHE: tuple[int, int] | None = None
_GPU_MEM_MONO: float | None = None
_GPU_MEM_TTL = 5.0


def read_gpu_memory_mib() -> tuple[int, int] | None:
    """NVIDIA VRAM (used, total) in MiB via nvidia-smi; None if unavailable."""
    global _GPU_MEM_CACHE, _GPU_MEM_MONO
    now = time.monotonic()
    if _GPU_MEM_MONO is not None and (now - _GPU_MEM_MONO) < _GPU_MEM_TTL:
        return _GPU_MEM_CACHE

    exe = shutil.which("nvidia-smi")
    if not exe:
        _GPU_MEM_CACHE = None
        _GPU_MEM_MONO = now
        return None

    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [exe, "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        _GPU_MEM_CACHE = None
        _GPU_MEM_MONO = now
        return None
    if proc.returncode != 0:
        _GPU_MEM_CACHE = None
        _GPU_MEM_MONO = now
        return None

    line = (proc.stdout or "").strip().splitlines()
    if not line:
        _GPU_MEM_CACHE = None
        _GPU_MEM_MONO = now
        return None
    try:
        parts = [p.strip() for p in line[0].split(",")]
        used = int(float(parts[0]))
        total = int(float(parts[1]))
    except (ValueError, IndexError):
        _GPU_MEM_CACHE = None
        _GPU_MEM_MONO = now
        return None
    if total <= 0:
        _GPU_MEM_CACHE = None
        _GPU_MEM_MONO = now
        return None

    _GPU_MEM_CACHE = (used, total)
    _GPU_MEM_MONO = now
    return _GPU_MEM_CACHE


def read_gpu_temp_celsius() -> float | None:
    """NVIDIA GPU die temperature via nvidia-smi when installed; else None."""
    global _GPU_CACHE_VAL, _GPU_CACHE_MONO
    now = time.monotonic()
    if _GPU_CACHE_MONO is not None and (now - _GPU_CACHE_MONO) < _GPU_CACHE_TTL:
        return _GPU_CACHE_VAL

    exe = shutil.which("nvidia-smi")
    if not exe:
        _GPU_CACHE_VAL = None
        _GPU_CACHE_MONO = now
        return None

    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [
                exe,
                "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        _GPU_CACHE_VAL = None
        _GPU_CACHE_MONO = now
        return None
    if proc.returncode != 0:
        _GPU_CACHE_VAL = None
        _GPU_CACHE_MONO = now
        return None
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        _GPU_CACHE_VAL = None
        _GPU_CACHE_MONO = now
        return None
    try:
        val = float(line[0].strip().replace(",", "."))
    except ValueError:
        _GPU_CACHE_VAL = None
        _GPU_CACHE_MONO = now
        return None
    if val < -20 or val > 120:
        _GPU_CACHE_VAL = None
        _GPU_CACHE_MONO = now
        return None
    _GPU_CACHE_VAL = val
    _GPU_CACHE_MONO = now
    return val
