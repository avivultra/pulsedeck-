"""Per-program history: who was using the computer, minute by minute.

The CSV history keeps system totals every second; this keeps *programs*
(same-name processes folded) so a later question like "who slowed the PC at
15:02 yesterday?" has an answer.

Cost: the process sampler already walks the process table every 2-5 s on its
own thread; this only folds that list by name (~0.5 ms) and, every
ROLLUP_SEC, writes at most 23 programs to a small SQLite file (one
executemany in WAL mode). No extra scan, nothing on the Tk thread. At most
~4 MB per day; days older than RETENTION_DAYS are pruned once a day.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

ROLLUP_SEC = 30.0
RETENTION_DAYS = 7          # same as the per-second CSV
TOP_BY_MEMORY = 15          # kept per roll-up...
TOP_BY_CPU = 8              # ...plus the busiest programs above CPU_KEEP
CPU_KEEP = 1.0              # % of the whole machine
_HIDDEN = {"system idle process", "idle", "registry", "memory compression"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts    INTEGER NOT NULL,   -- unix seconds, end of the roll-up window
    name  TEXT    NOT NULL,   -- program name, '.exe' stripped
    cpu   REAL    NOT NULL,   -- average % of the whole machine over the window
    rss   INTEGER NOT NULL,   -- peak resident memory over the window, bytes
    count INTEGER NOT NULL    -- peak number of processes
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
CREATE INDEX IF NOT EXISTS samples_name_ts ON samples (name, ts);
"""


def _short(name: str) -> str:
    return name[:-4] if name.lower().endswith(".exe") else name


class ProcessHistory:
    """Accumulates sampler ticks and writes per-program roll-ups."""

    def __init__(self, db_path: Path, rollup_sec: float = ROLLUP_SEC,
                 retention_days: int = RETENTION_DAYS) -> None:
        self.db_path = Path(db_path)
        self.rollup_sec = rollup_sec
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._acc: dict[str, list] = {}       # name -> [cpu_sum, ticks, rss_max, count_max]
        self._ticks = 0
        self._window_start: float | None = None
        self._last_prune = 0.0
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------ storage
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------ recording
    def offer(self, procs, now: float | None = None) -> None:
        """Called by the sampler after each tick with its ProcessInfo list."""
        now = time.time() if now is None else now
        with self._lock:
            if self._window_start is None:
                self._window_start = now
            per_tick: dict[str, list] = {}
            for p in procs:
                low = p.name.lower()
                if low in _HIDDEN:
                    continue
                g = per_tick.setdefault(_short(p.name), [0.0, 0, 0])
                g[0] += p.cpu_percent
                g[1] += p.rss_bytes
                g[2] += 1
            for name, (cpu, rss, count) in per_tick.items():
                a = self._acc.setdefault(name, [0.0, 0, 0, 0])
                a[0] += cpu
                a[2] = max(a[2], rss)
                a[3] = max(a[3], count)
            self._ticks += 1
            if now - self._window_start >= self.rollup_sec:
                self._flush(now)

    def _flush(self, now: float) -> None:
        ticks = max(1, self._ticks)
        rows = [(name, a[0] / ticks, a[2], a[3]) for name, a in self._acc.items()]
        self._acc.clear()
        self._ticks = 0
        self._window_start = now
        by_mem = sorted(rows, key=lambda r: r[2], reverse=True)
        by_cpu = sorted((r for r in rows if r[1] >= CPU_KEEP), key=lambda r: r[1], reverse=True)
        keep = {r[0] for r in by_mem[:TOP_BY_MEMORY]} | {r[0] for r in by_cpu[:TOP_BY_CPU]}
        ts = int(now)
        out = [(ts, name, round(cpu, 2), rss, count) for name, cpu, rss, count in rows if name in keep]
        try:
            db = self._db()
            with db:
                db.executemany("INSERT INTO samples (ts, name, cpu, rss, count) VALUES (?,?,?,?,?)", out)
            if now - self._last_prune > 86400:
                self._last_prune = now
                with db:
                    db.execute("DELETE FROM samples WHERE ts < ?", (ts - self.retention_days * 86400,))
        except sqlite3.Error:
            log.exception("Could not write process history to %s", self.db_path)

    # ------------------------------------------------------------ queries
    def who_was_busy(self, t0: float, t1: float, limit: int = 5) -> dict[str, list[dict]]:
        """The heaviest programs between t0 and t1 (unix seconds).

        Returns {"cpu": [...], "memory": [...]}, each item
        {"name", "cpu", "rss", "count"}: cpu is the average over the samples
        in range, rss and count the peak.
        """
        q = """SELECT name, AVG(cpu), MAX(rss), MAX(count) FROM samples
               WHERE ts BETWEEN ? AND ? GROUP BY name"""
        with self._lock:
            try:
                rows = self._db().execute(q, (int(t0), int(t1))).fetchall()
            except sqlite3.Error:
                log.exception("Process history query failed")
                return {"cpu": [], "memory": []}
        items = [{"name": n, "cpu": round(c or 0.0, 1), "rss": int(r or 0), "count": int(k or 0)}
                 for n, c, r, k in rows]
        return {
            "cpu": sorted((i for i in items if i["cpu"] >= 0.1), key=lambda i: i["cpu"], reverse=True)[:limit],
            "memory": sorted(items, key=lambda i: i["rss"], reverse=True)[:limit],
        }

    def around(self, t: float, window_sec: float = 90, limit: int = 5) -> dict[str, list[dict]]:
        """Who was busy around a moment (e.g. a load event)."""
        return self.who_was_busy(t - window_sec, t + window_sec, limit)

    def series(self, name: str, t0: float, t1: float) -> list[tuple[int, float, int]]:
        """(ts, cpu, rss) for one program between t0 and t1."""
        with self._lock:
            try:
                return self._db().execute(
                    "SELECT ts, cpu, rss FROM samples WHERE name = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                    (_short(name), int(t0), int(t1))).fetchall()
            except sqlite3.Error:
                log.exception("Process history query failed")
                return []


_default: ProcessHistory | None = None
_default_lock = threading.Lock()


def get_default_history() -> ProcessHistory:
    global _default
    with _default_lock:
        if _default is None:
            from metric_history import DEFAULT_HISTORY_DIR
            _default = ProcessHistory(DEFAULT_HISTORY_DIR / "processes.sqlite")
        return _default
