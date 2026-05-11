"""GPU sensor probes — vendor-agnostic chain.

Each probe is independent: if the relevant CLI isn't installed or the read
fails, the next probe in `GPU_PROBES` is tried. NVIDIA is always first, so
machines with NVIDIA cards keep behaving exactly as before.

Adding a new vendor = adding a new probe class to this file. Nothing else
in the codebase needs to change — `temperature_readings.read_gpu_temp_celsius()`
and `read_gpu_memory_mib()` already route through here.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GPUReading:
    temp_celsius: float | None
    mem_used_mib: int | None
    mem_total_mib: int | None
    source: str = ""   # which probe produced this reading (for diagnostics)


# ---------- Helpers ----------

def _no_window_flags() -> int:
    """Avoid flashing console windows on Windows when shelling out."""
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW
    return 0


def _run(cmd: list[str], timeout: float = 3.0) -> str | None:
    """Run a command silently. Returns stdout, or None on any failure."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=_no_window_flags(),
        )
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


# ---------- Probe interface ----------

class GPUProbe(ABC):
    name: str = "abstract"

    @abstractmethod
    def is_available(self) -> bool:
        """Quick (cheap) check: should this probe even be tried?"""

    @abstractmethod
    def read(self) -> GPUReading | None:
        """Return a reading or None on failure. Must not raise."""


# ---------- Vendor probes ----------

class NvidiaSmiProbe(GPUProbe):
    """NVIDIA via `nvidia-smi` — works on Windows, Linux, WSL."""
    name = "nvidia-smi"

    def is_available(self) -> bool:
        return shutil.which("nvidia-smi") is not None

    def read(self) -> GPUReading | None:
        out = _run([
            shutil.which("nvidia-smi"),
            "--query-gpu=temperature.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ])
        if not out:
            return None
        line = out.strip().splitlines()
        if not line:
            return None
        try:
            parts = [p.strip().replace(",", ".") for p in line[0].split(",")]
            temp = float(parts[0])
            used = int(float(parts[1]))
            total = int(float(parts[2]))
        except (ValueError, IndexError):
            return None
        if temp < -20 or temp > 120 or total <= 0:
            return None
        return GPUReading(temp, used, total, source=self.name)


class AmdSmiProbe(GPUProbe):
    """AMD via `amd-smi` (modern AMD tooling for ROCm 5.7+, Windows + Linux)."""
    name = "amd-smi"

    def is_available(self) -> bool:
        return shutil.which("amd-smi") is not None

    def read(self) -> GPUReading | None:
        # amd-smi metric -t for temperature, -m for memory; CSV format
        out = _run([shutil.which("amd-smi"), "metric", "-t", "-m", "--csv"])
        if not out:
            return None
        try:
            lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
            if len(lines) < 2:
                return None
            headers = [h.strip().lower() for h in lines[0].split(",")]
            values = [v.strip() for v in lines[1].split(",")]
            row = dict(zip(headers, values))
            # temperature column varies by amd-smi version: 'edge', 'temp_edge', 'temp', ...
            temp = None
            for k in ("temp_edge", "edge", "temp", "temperature"):
                if k in row and row[k] not in ("", "n/a", "na"):
                    try:
                        temp = float(row[k])
                        break
                    except ValueError:
                        pass
            used = None; total = None
            for k_used, k_total in [("vram_used", "vram_total"),
                                     ("mem_used", "mem_total"),
                                     ("used_vram", "total_vram")]:
                if k_used in row and k_total in row:
                    try:
                        used = int(float(row[k_used]))
                        total = int(float(row[k_total]))
                        break
                    except ValueError:
                        pass
            if temp is None and total is None:
                return None
            return GPUReading(temp, used, total, source=self.name)
        except Exception:
            log.exception("amd-smi parse failed")
            return None


class RocmSmiProbe(GPUProbe):
    """Older AMD tooling — `rocm-smi`, Linux-only."""
    name = "rocm-smi"

    def is_available(self) -> bool:
        return shutil.which("rocm-smi") is not None

    def read(self) -> GPUReading | None:
        out = _run([shutil.which("rocm-smi"), "--showtemp",
                    "--showmeminfo", "vram", "--csv"])
        if not out:
            return None
        try:
            temp = None; used = None; total = None
            # rocm-smi --csv output is messy; grep with regex
            m = re.search(r"Temperature.*?:?\s*([\d.]+)", out)
            if m:
                temp = float(m.group(1))
            m = re.search(r"VRAM Total Memory.*?(\d+)", out)
            if m:
                total = int(m.group(1)) // (1024 * 1024)
            m = re.search(r"VRAM Total Used Memory.*?(\d+)", out)
            if m:
                used = int(m.group(1)) // (1024 * 1024)
            if temp is None and total is None:
                return None
            return GPUReading(temp, used, total, source=self.name)
        except Exception:
            log.exception("rocm-smi parse failed")
            return None


class IntelXpuSmiProbe(GPUProbe):
    """Intel Arc / discrete GPUs via `xpu-smi`."""
    name = "xpu-smi"

    def is_available(self) -> bool:
        return shutil.which("xpu-smi") is not None

    def read(self) -> GPUReading | None:
        out = _run([shutil.which("xpu-smi"), "stats", "-d", "0", "-j"])
        if not out:
            return None
        try:
            import json
            data = json.loads(out)
            # xpu-smi json schema: device_level array of metric dicts
            temp = None; used = None; total = None
            for metric in data.get("device_level", []):
                if metric.get("metrics_type") == "XPUM_STATS_GPU_CORE_TEMPERATURE":
                    temp = float(metric.get("value", 0))
                elif metric.get("metrics_type") == "XPUM_STATS_MEMORY_USED":
                    used = int(metric.get("value", 0))
                elif metric.get("metrics_type") == "XPUM_STATS_MEMORY_TOTAL":
                    total = int(metric.get("value", 0))
            if temp is None and total is None:
                return None
            return GPUReading(temp, used, total, source=self.name)
        except Exception:
            log.exception("xpu-smi parse failed")
            return None


class LinuxSysfsGpuProbe(GPUProbe):
    """Vendor-agnostic Linux fallback — reads `/sys/class/drm/card*/device/hwmon/`.

    Works for AMD discrete cards, sometimes Intel iGPUs and dGPUs. No external
    tool required. Memory info is harder to extract here; we just return temp.
    """
    name = "linux-sysfs-gpu"

    def is_available(self) -> bool:
        if os.name != "posix":
            return False
        try:
            return any(Path("/sys/class/drm").glob("card*/device/hwmon/hwmon*/temp1_input"))
        except OSError:
            return False

    def read(self) -> GPUReading | None:
        try:
            for path in sorted(Path("/sys/class/drm").glob(
                    "card*/device/hwmon/hwmon*/temp1_input")):
                try:
                    raw = path.read_text(encoding="ascii").strip()
                    millideg = int(raw)
                    return GPUReading(
                        temp_celsius=millideg / 1000.0,
                        mem_used_mib=None, mem_total_mib=None,
                        source=self.name,
                    )
                except (OSError, ValueError):
                    continue
        except Exception:
            log.exception("linux-sysfs-gpu probe failed")
        return None


# Order matters: most precise / most common first. Falling through is cheap
# because is_available() is a single `shutil.which` or path-glob check.
GPU_PROBES: list[GPUProbe] = [
    NvidiaSmiProbe(),
    AmdSmiProbe(),
    RocmSmiProbe(),
    IntelXpuSmiProbe(),
    LinuxSysfsGpuProbe(),
]


def read_gpu() -> GPUReading | None:
    """Try probes in priority order; return the first successful reading."""
    for probe in GPU_PROBES:
        try:
            if not probe.is_available():
                continue
            result = probe.read()
            if result is not None:
                return result
        except Exception:
            log.exception("GPU probe %s raised", probe.name)
            continue
    return None
