"""Background process sampler — uses psutil.process_iter batching for speed,
maintains warm cache for cpu_percent priming, and tracks per-PID activity."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import psutil

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    cpu_percent: float          # normalized to all cores (0-100 across the machine)
    cpu_percent_raw: float      # psutil raw (can exceed 100 on multi-core)
    rss_bytes: int
    process_uptime_seconds: float       # how long the process has been running
    last_active_seconds_ago: float | None  # None == not seen active since sampler start

    @property
    def is_active_now(self) -> bool:
        return self.last_active_seconds_ago is not None and self.last_active_seconds_ago < 5.0


class ProcessSampler:
    """Periodically refreshes a snapshot of running processes (thread-safe)."""

    ACTIVE_CPU_THRESHOLD = 1.0   # raw cpu_percent above which we mark "active"

    def __init__(self, refresh_seconds: float = 2.0) -> None:
        self._refresh_seconds = max(0.5, float(refresh_seconds))
        self._lock = threading.Lock()
        self._snapshot: list[ProcessInfo] = []
        self._cpu_count = max(1, psutil.cpu_count(logical=True) or 1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Per-PID activity bookkeeping
        self._last_active: dict[int, float] = {}
        self._create_time: dict[int, float] = {}
        self._primed: set[int] = set()  # PIDs that had cpu_percent primed already

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._tick()  # prime
        self._thread = threading.Thread(target=self._run, name="process-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("Process sampler tick failed")
            if self._stop.wait(self._refresh_seconds):
                break

    def _tick(self) -> None:
        now = time.time()
        live: list[ProcessInfo] = []
        seen: set[int] = set()

        attrs = ["pid", "name", "cpu_percent", "memory_info", "create_time"]
        for proc in psutil.process_iter(attrs):
            try:
                info = proc.info
                pid = int(info.get("pid") or 0)
                if pid <= 0:
                    continue
                seen.add(pid)

                # First sighting: process_iter primed cpu_percent internally,
                # but the very first reading is always 0. Skip until next tick.
                if pid not in self._primed:
                    self._primed.add(pid)
                    ct = info.get("create_time")
                    if ct is not None:
                        self._create_time[pid] = float(ct)
                    continue

                cpu_raw = float(info.get("cpu_percent") or 0.0)
                mem = info.get("memory_info")
                if mem is None:
                    continue
                rss = int(getattr(mem, "rss", 0))
                name = (info.get("name") or "?")[:64]

                # Activity tracking
                if cpu_raw >= self.ACTIVE_CPU_THRESHOLD:
                    self._last_active[pid] = now
                last_seen = self._last_active.get(pid)
                last_active_ago = (now - last_seen) if last_seen is not None else None

                ct = self._create_time.get(pid)
                if ct is None:
                    ct = float(info.get("create_time") or now)
                    self._create_time[pid] = ct
                uptime = max(0.0, now - ct)

                live.append(ProcessInfo(
                    pid=pid, name=name,
                    cpu_percent=cpu_raw / self._cpu_count,
                    cpu_percent_raw=cpu_raw,
                    rss_bytes=rss,
                    process_uptime_seconds=uptime,
                    last_active_seconds_ago=last_active_ago,
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # GC dead PIDs from caches
        dead = set(self._primed) - seen
        for pid in dead:
            self._primed.discard(pid)
            self._last_active.pop(pid, None)
            self._create_time.pop(pid, None)

        with self._lock:
            self._snapshot = live

    def _snapshot_copy(self) -> list[ProcessInfo]:
        with self._lock:
            return list(self._snapshot)

    def top_by_cpu(self, n: int = 5) -> list[ProcessInfo]:
        snap = self._snapshot_copy()
        snap.sort(key=lambda p: p.cpu_percent_raw, reverse=True)
        return snap[: max(0, n)]

    def top_by_rss(self, n: int = 5) -> list[ProcessInfo]:
        snap = self._snapshot_copy()
        snap.sort(key=lambda p: p.rss_bytes, reverse=True)
        return snap[: max(0, n)]


_default_sampler: ProcessSampler | None = None
_default_lock = threading.Lock()


def get_default_sampler() -> ProcessSampler:
    global _default_sampler
    with _default_lock:
        if _default_sampler is None:
            _default_sampler = ProcessSampler()
            _default_sampler.start()
        return _default_sampler


def sample_now(refresh_wait: float = 1.0) -> tuple[list[ProcessInfo], list[ProcessInfo]]:
    """One-shot synchronous sample (useful for tests / scripts)."""
    s = ProcessSampler(refresh_seconds=refresh_wait)
    s._tick()
    time.sleep(refresh_wait)
    s._tick()
    return s.top_by_cpu(5), s.top_by_rss(5)
