"""Background process sampler — one read of the whole process table per tick
(win_processes on Windows, psutil.process_iter elsewhere), CPU % from the
difference between ticks, and per-PID activity tracking."""
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
    observed_seconds: float = 0.0       # how long this sampler has been watching it

    @property
    def is_active_now(self) -> bool:
        return self.last_active_seconds_ago is not None and self.last_active_seconds_ago < 5.0


class ProcessSampler:
    """Periodically refreshes a snapshot of running processes (thread-safe)."""

    ACTIVE_CPU_THRESHOLD = 1.0   # raw cpu_percent above which we mark "active"

    def __init__(self,
                 refresh_seconds: float = 2.0,
                 idle_refresh_seconds: float = 5.0,
                 active_window_seconds: float = 30.0) -> None:
        # Adaptive sampling: faster when there's recent UI/alert activity,
        # slower when the system is idle. The default keeps the "live" feel
        # without the constant 2s overhead when nothing interesting is happening.
        self._active_refresh = max(0.5, float(refresh_seconds))
        self._idle_refresh = max(self._active_refresh, float(idle_refresh_seconds))
        self._active_window = max(5.0, float(active_window_seconds))
        self._last_activity_at = 0.0   # bumped by notify_activity()
        self._lock = threading.Lock()
        self._snapshot: list[ProcessInfo] = []
        self._cpu_count = max(1, psutil.cpu_count(logical=True) or 1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Per-PID activity bookkeeping
        self._last_active: dict[int, float] = {}
        self._create_time: dict[int, float] = {}
        self._first_seen: dict[int, float] = {}
        self._primed: set[int] = set()  # PIDs that had cpu_percent primed already
        # Optional per-program history (process_history.ProcessHistory); fed
        # from this thread after every tick, so it costs no extra scan.
        self.recorder = None
        # Windows: the whole table in one system call (see win_processes for
        # why psutil's per-process path cost ~0.7 s of CPU per tick).
        self._native = None
        self._cpu_prev: dict[int, tuple[float, float, float]] = {}   # pid -> (create, cpu s, t)
        try:
            import win_processes
            if win_processes.AVAILABLE:
                self._native = win_processes.Snapshotter()
        except Exception:
            log.warning("Native process table unavailable; using psutil", exc_info=True)

    def notify_activity(self) -> None:
        """Mark the sampler as 'active' (e.g. just after a spike or UI open).

        The next `active_window_seconds` worth of ticks will use the faster
        refresh rate. After the window elapses without further activity, the
        sampler slows down again.
        """
        self._last_activity_at = time.monotonic()

    def _current_refresh_seconds(self) -> float:
        if (time.monotonic() - self._last_activity_at) < self._active_window:
            return self._active_refresh
        return self._idle_refresh

    @property
    def _refresh_seconds(self) -> float:
        """Backward compatibility for any test that read this property."""
        return self._current_refresh_seconds()

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
            # Re-evaluate refresh rate on every loop — adaptive
            if self._stop.wait(self._current_refresh_seconds()):
                break

    def _native_rows(self):
        """(pid, name, cpu_raw, rss, create_time) from one table read, or None
        if the native read failed (then psutil is used from here on)."""
        try:
            entries = self._native.read()
        except Exception:
            log.warning("Native process table read failed; using psutil from now on", exc_info=True)
            self._native = None
            return None
        t = time.monotonic()
        prev, cur, rows = self._cpu_prev, {}, []
        for pid, name, created, cpu_s, rss in entries:
            cur[pid] = (created, cpu_s, t)
            p = prev.get(pid)
            if p is not None and abs(p[0] - created) > 0.5:
                # Same pid, different process: start it over as a new one.
                self._primed.discard(pid)
                self._last_active.pop(pid, None)
                p = None
            cpu_raw = 0.0 if p is None or t <= p[2] else max(0.0, (cpu_s - p[1]) / (t - p[2]) * 100.0)
            rows.append((pid, name, cpu_raw, rss, created))
        self._cpu_prev = cur
        return rows

    def _psutil_rows(self):
        attrs = ["pid", "name", "cpu_percent", "memory_info", "create_time"]
        for proc in psutil.process_iter(attrs):
            try:
                info = proc.info
                mem = info.get("memory_info")
                yield (int(info.get("pid") or 0), info.get("name") or "?",
                       float(info.get("cpu_percent") or 0.0),
                       int(getattr(mem, "rss", 0)) if mem is not None else None,
                       info.get("create_time"))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def _tick(self) -> None:
        now = time.time()
        live: list[ProcessInfo] = []
        seen: set[int] = set()

        rows = self._native_rows() if self._native is not None else None
        for pid, name, cpu_raw, rss, created in (rows if rows is not None else self._psutil_rows()):
            try:
                if pid <= 0:
                    continue
                seen.add(pid)

                # First sighting: the first CPU reading has nothing to be
                # compared with (always 0). Skip until the next tick.
                if pid not in self._primed:
                    self._primed.add(pid)
                    self._first_seen[pid] = now
                    if created is not None:
                        self._create_time[pid] = float(created)
                    continue

                if rss is None:
                    continue
                name = name[:64]

                # Activity tracking
                if cpu_raw >= self.ACTIVE_CPU_THRESHOLD:
                    self._last_active[pid] = now
                last_seen = self._last_active.get(pid)
                last_active_ago = (now - last_seen) if last_seen is not None else None

                ct = self._create_time.get(pid)
                if ct is None:
                    ct = float(created or now)
                    self._create_time[pid] = ct
                uptime = max(0.0, now - ct)

                live.append(ProcessInfo(
                    pid=pid, name=name,
                    cpu_percent=cpu_raw / self._cpu_count,
                    cpu_percent_raw=cpu_raw,
                    rss_bytes=rss,
                    process_uptime_seconds=uptime,
                    last_active_seconds_ago=last_active_ago,
                    observed_seconds=max(0.0, now - self._first_seen.get(pid, now)),
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # GC dead PIDs from caches
        dead = set(self._primed) - seen
        for pid in dead:
            self._primed.discard(pid)
            self._last_active.pop(pid, None)
            self._create_time.pop(pid, None)
            self._first_seen.pop(pid, None)

        with self._lock:
            self._snapshot = live
        if self.recorder is not None:
            try:
                self.recorder.offer(live, now)
            except Exception:
                log.exception("Process history recording failed")

    def _snapshot_copy(self) -> list[ProcessInfo]:
        with self._lock:
            return list(self._snapshot)

    def snapshot(self) -> list[ProcessInfo]:
        """Every process from the last tick (a copy; safe from any thread)."""
        return self._snapshot_copy()

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
            try:
                from process_history import get_default_history
                _default_sampler.recorder = get_default_history()
            except Exception:
                log.exception("Process history unavailable")
            _default_sampler.start()
        return _default_sampler


def peek_default_sampler() -> ProcessSampler | None:
    """The running sampler, or None. Never creates or starts one."""
    return _default_sampler


def sample_now(refresh_wait: float = 1.0) -> tuple[list[ProcessInfo], list[ProcessInfo]]:
    """One-shot synchronous sample (useful for tests / scripts)."""
    s = ProcessSampler(refresh_seconds=refresh_wait)
    s._tick()
    time.sleep(refresh_wait)
    s._tick()
    return s.top_by_cpu(5), s.top_by_rss(5)
