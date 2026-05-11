"""CPU temperature probes — graceful chain across OS / vendor / sensor type.

The existing logic in `temperature_readings.py` used psutil first and Windows
WMI as a fallback. This module formalises the pattern as a probe chain and
adds Linux sysfs (`/sys/class/thermal/`) and a macOS hint, so machines that
psutil doesn't cover have another shot.
"""
from __future__ import annotations

import logging
import os
import platform
from abc import ABC, abstractmethod
from pathlib import Path

log = logging.getLogger(__name__)


class CPUTempProbe(ABC):
    name: str = "abstract"

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def read(self) -> float | None: ...


class PsutilProbe(CPUTempProbe):
    """psutil.sensors_temperatures() — works on Linux, macOS, some Windows."""
    name = "psutil"

    def is_available(self) -> bool:
        try:
            import psutil
            return hasattr(psutil, "sensors_temperatures")
        except ImportError:
            return False

    def read(self) -> float | None:
        try:
            import psutil
            temps = psutil.sensors_temperatures()
            if not temps:
                return None
            # Priority of known sensor groups
            for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz",
                        "zenpower", "it8728", "nct6798"):
                if key in temps and temps[key]:
                    return float(temps[key][0].current)
            # Fall back to the first available group
            first = next(iter(temps.values()))
            if first:
                return float(first[0].current)
        except (AttributeError, NotImplementedError, OSError, ValueError):
            return None
        return None


class WindowsWmiProbe(CPUTempProbe):
    """Windows WMI MSAcpi_ThermalZoneTemperature — only works with admin
    and certain motherboards. Tolerates failure silently."""
    name = "windows-wmi"

    def is_available(self) -> bool:
        return os.name == "nt"

    def read(self) -> float | None:
        try:
            import subprocess
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "(Get-CimInstance -Namespace root/wmi -ClassName "
                    "MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue "
                    "| Select-Object -First 1).CurrentTemperature",
                ],
                capture_output=True, text=True, timeout=4,
                creationflags=flags,
            )
        except (OSError, FileNotFoundError):
            return None
        if proc.returncode != 0:
            return None
        raw = (proc.stdout or "").strip().replace(",", ".")
        if not raw:
            return None
        try:
            kelvin_tenths = float(raw)
        except ValueError:
            return None
        # Sentinel: BIOS sometimes returns absurd values when no sensor
        if kelvin_tenths < 2500 or kelvin_tenths > 4500:
            return None
        return (kelvin_tenths / 10.0) - 273.15


class LinuxThermalZoneProbe(CPUTempProbe):
    """Direct read from /sys/class/thermal/thermal_zone*/temp.

    Covers many machines where psutil's sensor grouping misses the CPU. We
    look for zones with type 'x86_pkg_temp', 'cpu', or 'coretemp'."""
    name = "linux-thermal-zone"

    def is_available(self) -> bool:
        if os.name != "posix":
            return False
        try:
            return any(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
        except OSError:
            return False

    def read(self) -> float | None:
        try:
            preferred_types = {"x86_pkg_temp", "cpu-thermal", "coretemp",
                                "soc_thermal", "cpu_thermal"}
            zones = sorted(Path("/sys/class/thermal").glob("thermal_zone*"))
            # First pass: preferred zone types
            for z in zones:
                ztype = (z / "type").read_text(encoding="ascii").strip().lower()
                if ztype in preferred_types:
                    raw = (z / "temp").read_text(encoding="ascii").strip()
                    return int(raw) / 1000.0
            # Fallback: any zone
            for z in zones:
                try:
                    raw = (z / "temp").read_text(encoding="ascii").strip()
                    val = int(raw) / 1000.0
                    if 0 < val < 120:
                        return val
                except (OSError, ValueError):
                    continue
        except Exception:
            log.exception("linux-thermal-zone probe failed")
        return None


class MacosPowermetricsProbe(CPUTempProbe):
    """macOS — `powermetrics` is the official path. Usually requires sudo
    so we just return None unless run elevated. Kept as a placeholder so
    the chain has a macOS entry."""
    name = "macos-powermetrics"

    def is_available(self) -> bool:
        return platform.system() == "Darwin"

    def read(self) -> float | None:
        # Honest fallback — no reliable non-sudo CPU temp on macOS.
        # Listed for documentation; a future contributor can wire SMC or
        # IOReport here.
        return None


# Order matters: psutil first (cheapest + most reliable when it works).
CPU_PROBES: list[CPUTempProbe] = [
    PsutilProbe(),
    WindowsWmiProbe(),
    LinuxThermalZoneProbe(),
    MacosPowermetricsProbe(),
]


def read_cpu_temperature_celsius() -> float | None:
    """Run probes in order; return the first non-None reading."""
    for probe in CPU_PROBES:
        try:
            if not probe.is_available():
                continue
            val = probe.read()
            if val is not None:
                return val
        except Exception:
            log.exception("CPU probe %s raised", probe.name)
            continue
    return None
