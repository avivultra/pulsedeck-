"""Ghost Sweeper — find processes, dev servers and CLI agents that finished
their work and were never cleaned up.

The Janitor next door handles one specific artefact (conhost.exe clusters) and
the server scanner answers "what is listening right now". Neither has a sense
of TIME, which is the thing that actually distinguishes a working process from
an abandoned one. That is this module's whole job: it watches the same
processes across scans and reports what has stopped doing anything.

WHAT COUNTS AS A GHOST
----------------------
A candidate is flagged when it is old enough (`idle_minutes`, default 30) AND
at least one of these holds:

  ORPHAN       its parent process is gone — the terminal or agent that
               launched it exited and left it running
  DORMANT      it has burned essentially no CPU for the whole idle window
  IDLE_SERVER  it is listening on a TCP port with zero established
               connections for the whole idle window

WHY THE LINEAGE CACHE MATTERS
-----------------------------
Once a parent dies you can no longer ask the OS what it was called, and on
Windows its PID gets recycled — so a naive `Process(ppid).name()` will either
fail or, worse, confidently name a completely unrelated process. So every scan
records `pid -> (name, create_time)` for everything alive. When a parent later
disappears we still know it was `claude.exe`, and comparing create_times tells
us whether a live PID is the real parent or a recycled impostor.

NOTHING IS EVER KILLED AUTOMATICALLY. This module only observes and reports;
termination goes through `alerts.try_terminate`, which asks for confirmation
and refuses protected processes.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import threading
import time
from dataclasses import dataclass

import psutil

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- constants

# Only these names are considered on their own merits. Without this filter the
# report drowns in perfectly normal orphaned Windows processes (explorer.exe
# and friends are orphaned by design). Anything LISTENING on a port becomes a
# candidate regardless of name — see `_is_candidate`.
INTERESTING_NAMES: frozenset[str] = frozenset({
    # runtimes
    "node.exe", "node", "python.exe", "python", "pythonw.exe", "python3",
    "deno.exe", "deno", "bun.exe", "bun", "php.exe", "php",
    "java.exe", "java", "dotnet.exe", "dotnet", "ruby.exe", "ruby",
    "perl.exe", "perl", "go.exe", "go",
    # package managers / build tools
    "npm.exe", "npm", "yarn.exe", "yarn", "pnpm.exe", "pnpm",
    "cargo.exe", "cargo", "gradle.exe", "gradle", "webpack", "esbuild.exe",
    "tsc.exe", "vite.exe", "nodemon",
    # servers / infra
    "nginx.exe", "nginx", "httpd.exe", "httpd", "caddy.exe", "caddy",
    "uvicorn.exe", "uvicorn", "gunicorn", "ngrok.exe", "ngrok",
    "mysqld.exe", "mysqld", "postgres.exe", "postgres",
    "redis-server.exe", "redis-server", "mongod.exe", "mongod",
    # agents / editors that spawn long-lived helpers
    "claude.exe", "claude", "code.exe", "electron.exe",
    "cursor.exe", "ollama.exe", "ollama",
})

# Never reported. conhost belongs to the Janitor; the rest are OS plumbing
# whose "orphanhood" is meaningless.
NEVER_REPORT: frozenset[str] = frozenset({
    "conhost.exe",                      # janitor.py owns these
    "system", "system idle process", "registry", "memory compression",
    "svchost.exe", "services.exe", "lsass.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "smss.exe", "explorer.exe", "dwm.exe", "fontdrvhost.exe",
    "runtimebroker.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "ctfmon.exe", "taskhostw.exe", "sihost.exe",
    "spoolsv.exe", "audiodg.exe", "dllhost.exe", "wudfhost.exe",
})

# Ports we never treat as "an abandoned dev server" — killing these is an OS
# or security-software problem, not a cleanup.
SYSTEM_PORTS: frozenset[int] = frozenset({135, 137, 138, 139, 445, 5040, 49664,
                                          49665, 49666, 49667, 49668, 49669})

# A direct child of one of these is a Windows service, not something a user
# forgot about. Its "idleness" is its job description.
SERVICE_PARENTS: frozenset[str] = frozenset({
    "services.exe", "wininit.exe", "smss.exe", "svchost.exe",
})

# Dormancy alone is a weak signal — a database or a tray app sitting quietly is
# normal, not abandoned. So a process flagged ONLY for being dormant must have
# been dormant for this multiple of `idle_seconds` before we bother the user.
_DORMANT_ONLY_MULTIPLIER = 4.0

# CPU seconds a process must accumulate between two scans before we call it
# "active". Below this it is jitter (a timer waking up, a GC pass).
_CPU_ACTIVITY_EPSILON_SEC = 1.0

# Verdict thresholds, as a percentage of ONE core over the observation window.
_SPINNING_PCT = 25.0     # burning a quarter core while orphaned = stuck loop
_WORKING_PCT = 2.0       # anything above this is doing real (if light) work
_LEAK_DELTA_BYTES = 50 * 1024 * 1024   # RSS growth that counts as a leak


# ---------------------------------------------------------------- data model

@dataclass(frozen=True)
class GhostProcess:
    """Everything we know about one suspected ghost.

    Deliberately verbose: the whole point is that the user should be able to
    decide from this card alone, without opening Task Manager.
    """

    pid: int
    name: str
    exe: str
    cmdline: str
    cwd: str
    username: str

    create_time: float               # unix
    age_seconds: float

    # Lineage
    parent_pid: int
    parent_name: str                 # "?" when it died before we ever saw it
    parent_alive: bool
    orphaned_for_seconds: float | None    # None = not an orphan
    orphan_time_is_lower_bound: bool      # True = "at least this long"

    # Activity
    cpu_seconds_total: float         # CPU time consumed over its whole life
    cpu_percent_window: float        # % of one core, over our observation window
    cpu_percent_lifetime: float      # % of one core, over its whole life
    idle_for_seconds: float | None   # since last observed CPU activity
    idle_time_is_lower_bound: bool
    rss_bytes: int
    rss_delta_bytes: int             # growth since we first saw it
    num_threads: int

    # Network
    listening_ports: tuple[int, ...]
    established_connections: int

    # Conclusions
    reasons: tuple[str, ...]         # ORPHAN / DORMANT / IDLE_SERVER
    verdict: str                     # FINISHED / STUCK / LEAKING / WORKING
    verdict_detail: str              # one Hebrew sentence explaining the verdict

    observed_seconds: float          # how long the sweeper has watched this pid
    first_scan: bool                 # True = numbers are lower bounds

    @property
    def instance_key(self) -> str:
        """Stable identity for this exact process instance (survives PID reuse)."""
        return f"{self.pid}:{self.create_time:.0f}"

    @property
    def is_orphan(self) -> bool:
        return "ORPHAN" in self.reasons


@dataclass
class _Track:
    """Per-PID state carried between scans. Internal."""

    pid: int
    name: str
    create_time: float
    first_seen_wall: float
    cpu_seconds_at_first_seen: float
    rss_at_first_seen: int
    last_cpu_seconds: float
    last_cpu_activity_wall: float          # last time CPU time actually grew
    parent_pid: int
    parent_name: str
    parent_missing_since_wall: float | None = None
    idle_server_since_wall: float | None = None
    seen_scans: int = 1
    # True while the corresponding "since" timestamp is an estimate derived
    # from the process's own lifetime rather than something we watched happen.
    # Both flip to False the moment we observe the real event.
    idle_since_is_estimate: bool = False
    orphan_since_is_estimate: bool = False


# ---------------------------------------------------------------- formatting

def humanize_duration(seconds: float | None) -> str:
    """Hebrew duration string for the UI."""
    if seconds is None:
        return "—"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f} שניות"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.0f} דקות"
    hours = minutes / 60.0
    if hours < 24:
        whole = int(hours)
        rem_min = int(round((hours - whole) * 60))
        return f"{whole} שעות {rem_min} דקות" if rem_min else f"{whole} שעות"
    days = hours / 24.0
    return f"{days:.1f} ימים"


def humanize_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    mib = n / (1024 * 1024)
    if abs(mib) < 1024:
        return f"{mib:.0f} MiB"
    return f"{mib / 1024:.2f} GiB"


# ------------------------------------------------------------- sweep log
#
# The panel only ever shows a snapshot of right now. This log is what makes
# the sweeper answer questions weeks later: which project keeps leaking
# processes, whether ghosts accumulate or get cleaned up, which app is the
# repeat offender. English and greppable, matching janitor.log / monitor.log.

_sweep_logger: logging.Logger | None = None


def _get_sweep_logger() -> logging.Logger:
    """Lazy-init the rotating logger behind history/sweeper.log."""
    global _sweep_logger
    if _sweep_logger is not None:
        return _sweep_logger

    audit = logging.getLogger("ghost_sweeper.audit")
    audit.setLevel(logging.INFO)
    audit.propagate = False          # never bubble into monitor.log

    try:
        from logging.handlers import RotatingFileHandler

        from metric_history import DEFAULT_HISTORY_DIR
        DEFAULT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            DEFAULT_HISTORY_DIR / "sweeper.log",
            maxBytes=512_000, backupCount=3, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s — %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        audit.addHandler(handler)
    except Exception:
        log.exception("Could not initialize the sweep log")
    _sweep_logger = audit
    return audit


def _log_duration(seconds: float | None) -> str:
    """Compact ASCII duration for log lines: 45s / 12m / 3h20m / 2d4h."""
    if seconds is None:
        return "?"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h" if hours else f"{days}d"


# ------------------------------------------------- command-line redaction
#
# A ghost's full command line is the single most useful field on its card —
# it is what tells you which project the process belongs to. It is also where
# credentials end up: `ngrok http 8473 --basic-auth=user:hunter2`,
# `mysql -u root --password=...`, `psql postgres://user:pw@host/db`.
#
# Nothing here is ever written to disk or sent anywhere; this is purely about
# what appears on screen, so a screenshot or a screen-share cannot leak a
# secret. Redaction is applied at DISPLAY time, not at scan time, so the
# toggle takes effect immediately without re-scanning.

MASK = "••••••"

# Flag names whose value is a secret. Deliberately specific: a bare `--key`
# is usually a path to a .pem file, so it is matched only in compound forms
# (api-key, access-key, secret-key).
_SECRET_KEY = (
    r"(?:passwd|password|pwd|passphrase"
    r"|client[-_]?secret|secret[-_]?key|secret"
    r"|access[-_]?token|refresh[-_]?token|bearer|token"
    r"|api[-_]?key|apikey|access[-_]?key"
    r"|basic[-_]?auth|auth[-_]?token|credentials?|auth)"
)

_REDACTORS: tuple[tuple[re.Pattern[str], str], ...] = (
    # --password=VALUE   --token:VALUE   PASSWORD=VALUE
    (re.compile(rf"(?i)(\b-{{0,2}}{_SECRET_KEY}\s*[=:]\s*)(\S+)"), r"\1" + MASK),
    # --password VALUE  (space separated; requires a leading dash so that the
    # word "password" in a file path is not mistaken for a flag)
    (re.compile(rf"(?i)(\s--?{_SECRET_KEY}\s+)(?!-)(\S+)"), r"\1" + MASK),
    # scheme://user:password@host — mask only the password portion
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@]+)(@)"),
     r"\1" + MASK + r"\3"),
)


def redact_cmdline(text: str) -> str:
    """Mask passwords / tokens / API keys in a command line for display."""
    if not text:
        return text
    for pattern, replacement in _REDACTORS:
        text = pattern.sub(replacement, text)
    return text


def secrets_are_redacted() -> bool:
    """Whether the panel should mask secrets. Defaults to True."""
    try:
        import config as app_config
        cfg = app_config.load_config().get("sweeper", {}) or {}
        return bool(cfg.get("redact_secrets", True))
    except Exception:
        log.exception("Could not read redact_secrets; defaulting to masked")
        return True


def set_secrets_redacted(enabled: bool) -> None:
    """Persist the masking preference to config.json."""
    try:
        import config as app_config
        cfg = app_config.load_config()
        cfg.setdefault("sweeper", {})["redact_secrets"] = bool(enabled)
        app_config.save_config(cfg)
    except Exception:
        log.exception("Could not persist redact_secrets")


REASON_LABELS: dict[str, str] = {
    "ORPHAN": "יתום — ההורה נסגר",
    "DORMANT": "רדום — לא עשה כלום",
    "IDLE_SERVER": "שרת ללא לקוחות",
}

VERDICT_LABELS: dict[str, str] = {
    "FINISHED": "כנראה סיים",
    "STUCK": "תקוע",
    "LEAKING": "תופח בזיכרון",
    "WORKING": "עדיין עובד",
}

VERDICT_COLORS: dict[str, str] = {
    "STUCK": "#ff5c6c",
    "LEAKING": "#ebcb8b",
    "FINISHED": "#58a6ff",
    "WORKING": "#3fb950",
}


# ---------------------------------------------------------------- the scanner

class GhostSweeper:
    """Background thread that periodically looks for abandoned processes.

    Thread-safe. UI threads read via `get_ghosts()` / `count()`, which return
    a snapshot copy and never block on a scan.
    """

    def __init__(
        self,
        scan_interval_seconds: float = 1200.0,   # 20 minutes
        idle_seconds: float = 1800.0,            # 30 minutes
        ignored_names: frozenset[str] | None = None,
        ignored_instances: frozenset[str] | None = None,
    ) -> None:
        self._scan_interval = max(60.0, float(scan_interval_seconds))
        self._idle_seconds = max(60.0, float(idle_seconds))
        self._ignored_names = set(ignored_names or ())
        self._ignored_instances = set(ignored_instances or ())

        self._lock = threading.Lock()
        self._ghosts: list[GhostProcess] = []
        self._last_scan_wall: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._self_pid = os.getpid()

        # pid -> _Track, for processes we are actively following
        self._tracks: dict[int, _Track] = {}
        # pid -> (name, create_time) for EVERY process seen in the last scan.
        # This is what lets us name a parent after it has died.
        self._lineage: dict[int, tuple[str, float]] = {}
        self._scan_count = 0
        # instance_key -> (name, verdict, wall time first reported, cwd).
        # Drives the NEW / GONE lines in the sweep log; survives a process's
        # death, which its _Track does not.
        self._reported: dict[str, tuple[str, str, float, str]] = {}

    # ---- lifecycle ----

    def start(self) -> None:
        """Spawn the sweeper thread. Returns immediately.

        The first scan deliberately runs ON THE THREAD, not here. A full sweep
        costs several seconds on a busy machine (it stats every process and
        then reads cmdline/cwd/username for each candidate), and start() is
        called during monitor startup — doing it inline would delay the dock
        appearing by that whole time for no benefit.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ghost-sweeper",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # Scan once straight away so the dock badge has something to show,
        # then settle into the configured cadence.
        try:
            self._do_scan()
        except Exception:
            log.exception("Initial ghost sweep failed")
        while not self._stop.is_set():
            if self._stop.wait(self._scan_interval):
                break
            try:
                self._do_scan()
            except Exception:
                log.exception("Ghost sweep tick failed")

    def _do_scan(self) -> None:
        ghosts = self.scan()
        with self._lock:
            self._ghosts = ghosts
            self._last_scan_wall = time.time()
        try:
            self._record_sweep(ghosts)
        except Exception:
            log.exception("Writing the sweep log failed")

    def _record_sweep(self, ghosts: list[GhostProcess]) -> None:
        """Append this sweep to history/sweeper.log.

        Three kinds of line, because three different questions get asked weeks
        later: a per-sweep summary (how does the count trend?), a NEW line per
        ghost the first time it is reported (which project keeps leaking
        processes?), and a GONE line when one disappears (did it get cleaned up
        or did it linger for days?).
        """
        audit = _get_sweep_logger()
        counts: dict[str, int] = {}
        for g in ghosts:
            counts[g.verdict] = counts.get(g.verdict, 0) + 1
        breakdown = " ".join(f"{k.lower()}={counts.get(k, 0)}"
                             for k in ("FINISHED", "STUCK", "LEAKING", "WORKING"))
        audit.info("sweep: %d ghosts (%s)", len(ghosts), breakdown)

        now = time.time()
        current: dict[str, tuple[str, str, float, str]] = {}
        for g in ghosts:
            key = g.instance_key
            previous = self._reported.get(key)
            first_reported = previous[2] if previous else now
            current[key] = (g.name, g.verdict, first_reported, g.cwd)
            if previous is None:
                # Command lines go to disk ALWAYS masked. The panel checkbox
                # governs the screen only — a log file outlives the session and
                # is far more likely to be copied around.
                audit.info(
                    "NEW %s pid=%d verdict=%s reasons=%s parent=%s(%d,%s) "
                    "idle=%s age=%s cwd=%s cmd=%s",
                    g.name, g.pid, g.verdict, "+".join(g.reasons),
                    g.parent_name, g.parent_pid,
                    "alive" if g.parent_alive else "dead",
                    _log_duration(g.idle_for_seconds),
                    _log_duration(g.age_seconds),
                    g.cwd or "?", redact_cmdline(g.cmdline) or "?",
                )

        for key, (name, verdict, first_reported, cwd) in self._reported.items():
            if key not in current:
                audit.info("GONE %s (%s) after %s on the list, cwd=%s",
                           name, verdict, _log_duration(now - first_reported),
                           cwd or "?")
        self._reported = current

    def log_user_action(self, ghost: GhostProcess, action: str) -> None:
        """Record something the user did from the panel (close / ignore)."""
        try:
            _get_sweep_logger().info(
                "USER %s: %s pid=%d verdict=%s cwd=%s",
                action, ghost.name, ghost.pid, ghost.verdict, ghost.cwd or "?",
            )
        except Exception:
            log.exception("Could not write user action to the sweep log")

    # ---- ignore list ----

    def ignore_name(self, name: str) -> None:
        self._ignored_names.add(name.strip().lower())

    def ignore_instance(self, instance_key: str) -> None:
        self._ignored_instances.add(instance_key)

    def _is_ignored(self, name: str, instance_key: str) -> bool:
        return (name.lower() in self._ignored_names
                or instance_key in self._ignored_instances)

    # ---- candidate selection ----

    def _is_candidate(self, name: str, has_ports: bool) -> bool:
        low = name.lower()
        if low in NEVER_REPORT:
            return False
        return has_ports or low in INTERESTING_NAMES

    # ---- scanning ----

    def scan(self) -> list[GhostProcess]:
        """One full sweep. Safe to call from any thread; takes ~100 ms."""
        now = time.time()
        self._scan_count += 1
        first_scan = self._scan_count == 1

        listening, established = _connection_map()

        # Pass 1 — collect raw info for every process, and refresh the lineage
        # cache BEFORE we start asking questions about parents.
        raw: dict[int, dict] = {}
        for proc in psutil.process_iter(
            # NOTE: "username" is deliberately absent. On Windows each
            # username resolution is a LookupAccountSid round trip; including
            # it here took this loop from 1.7 s to 5.2 s across ~300
            # processes. It is fetched per-PID in _describe() instead, for
            # the handful that actually get flagged.
            ["pid", "name", "ppid", "create_time", "cpu_times",
             "memory_info", "num_threads"]
        ):
            try:
                info = proc.info
                pid = int(info.get("pid") or 0)
                if pid <= 0:
                    continue
                raw[pid] = info
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        new_lineage: dict[int, tuple[str, float]] = {
            pid: ((info.get("name") or "?"), float(info.get("create_time") or 0.0))
            for pid, info in raw.items()
        }
        # Merge: keep entries for processes that just died so we can still name
        # them as parents. Entries whose PID has been reused are overwritten by
        # the live one above, which is exactly what we want.
        merged_lineage = dict(self._lineage)
        merged_lineage.update(new_lineage)

        ghosts: list[GhostProcess] = []
        live_pids = set(raw.keys())

        for pid, info in raw.items():
            if pid == self._self_pid:
                continue
            name = (info.get("name") or "?").strip()
            ports = tuple(sorted(p for p in listening.get(pid, ())
                                 if p not in SYSTEM_PORTS))
            if not self._is_candidate(name, bool(ports)):
                continue

            try:
                ghost = self._analyse(pid, info, ports, established, raw,
                                      merged_lineage, now, first_scan)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                log.exception("Ghost analysis failed for PID %d", pid)
                continue
            if ghost is not None:
                ghosts.append(ghost)

        # Drop tracks for processes that are gone, and trim the lineage cache
        # so it cannot grow without bound over a long uptime.
        self._tracks = {p: t for p, t in self._tracks.items() if p in live_pids}
        self._lineage = _trim_lineage(merged_lineage, live_pids)

        # Worst first: stuck before finished, then longest-idle.
        order = {"STUCK": 0, "LEAKING": 1, "FINISHED": 2, "WORKING": 3}
        ghosts.sort(key=lambda g: (order.get(g.verdict, 9),
                                   -(g.idle_for_seconds or 0.0)))
        return ghosts

    def _analyse(self, pid: int, info: dict, ports: tuple[int, ...],
                 established: dict[int, int], raw: dict[int, dict],
                 lineage: dict[int, tuple[str, float]], now: float,
                 first_scan: bool) -> GhostProcess | None:
        """Decide whether one process is a ghost, and describe it if so."""
        name = (info.get("name") or "?").strip()
        create_time = float(info.get("create_time") or 0.0)
        age = max(0.0, now - create_time)

        cpu_times = info.get("cpu_times")
        cpu_total = 0.0
        if cpu_times is not None:
            cpu_total = float(getattr(cpu_times, "user", 0.0)
                              + getattr(cpu_times, "system", 0.0))
        mem = info.get("memory_info")
        rss = int(getattr(mem, "rss", 0)) if mem is not None else 0
        ppid = int(info.get("ppid") or 0)

        # Parent liveness is resolved first because the track seeds its
        # orphan timestamp from it on the very first sighting.
        parent_name, parent_alive = _resolve_parent(ppid, create_time, raw, lineage)

        track = self._update_track(pid, name, create_time, cpu_total, rss,
                                   ppid, parent_alive, lineage, now)

        # --- lineage / orphan analysis ---
        # All the "since when" timestamps live on the track and were seeded at
        # first sighting (see _update_track). Reading them here rather than
        # recomputing keeps the answer stable: a process reported as orphaned
        # for 3 hours must still say 3 hours on the next scan, not 20 minutes.
        if parent_alive:
            track.parent_missing_since_wall = None
            track.orphan_since_is_estimate = False
        elif track.parent_missing_since_wall is None:
            # The parent died while we were watching — an exact timestamp.
            track.parent_missing_since_wall = now
            track.orphan_since_is_estimate = False
        if parent_name != "?":
            track.parent_name = parent_name

        orphaned_for: float | None = None
        orphan_lower_bound = False
        if not parent_alive and track.parent_missing_since_wall is not None:
            orphaned_for = max(0.0, now - track.parent_missing_since_wall)
            orphan_lower_bound = track.orphan_since_is_estimate

        # --- activity analysis ---
        observed = max(0.0, now - track.first_seen_wall)
        cpu_in_window = max(0.0, cpu_total - track.cpu_seconds_at_first_seen)
        window_pct = (100.0 * cpu_in_window / observed) if observed > 1 else 0.0
        lifetime_pct = (100.0 * cpu_total / age) if age > 1 else 0.0

        idle_for = max(0.0, now - track.last_cpu_activity_wall)
        idle_lower_bound = track.idle_since_is_estimate

        established_n = established.get(pid, 0)

        # --- idle-server analysis ---
        if ports and established_n == 0:
            if track.idle_server_since_wall is None:
                track.idle_server_since_wall = now
        else:
            track.idle_server_since_wall = None

        # --- decide ---
        if age < self._idle_seconds:
            return None          # too young to have been abandoned

        # A Windows service launched by the SCM is idle by design. Reporting
        # one as "abandoned" is both wrong and dangerous to act on.
        if track.parent_name.lower() in SERVICE_PARENTS:
            return None

        reasons: list[str] = []
        if not parent_alive:
            reasons.append("ORPHAN")
        if (track.idle_server_since_wall is not None
                and (now - track.idle_server_since_wall) >= self._idle_seconds):
            reasons.append("IDLE_SERVER")
        if idle_for >= self._idle_seconds:
            # Dormancy on its own is weak evidence — plenty of healthy
            # processes do nothing for hours. It only stands alone once the
            # silence has gone on far longer than the base threshold.
            if reasons or idle_for >= self._idle_seconds * _DORMANT_ONLY_MULTIPLIER:
                reasons.append("DORMANT")
        if not reasons:
            return None

        instance_key = f"{pid}:{create_time:.0f}"
        if self._is_ignored(name, instance_key):
            return None

        rss_delta = rss - track.rss_at_first_seen
        verdict, detail = _classify(
            window_pct=window_pct, lifetime_pct=lifetime_pct,
            idle_for=idle_for, rss_delta=rss_delta,
            observed=observed, established=established_n, reasons=reasons,
        )

        # Expensive-but-valuable details, only for the few flagged PIDs.
        # Username in particular costs a LookupAccountSid round trip, which is
        # why it is not in the bulk process_iter above.
        exe, cmdline, cwd, username = _describe(pid)

        # Only ever report processes the user actually owns. Anything running
        # as SYSTEM / a service account / another user is either OS plumbing
        # or not ours to touch, and an unreadable owner means the same thing.
        if not _is_current_user(username):
            return None

        return GhostProcess(
            pid=pid, name=name, exe=exe, cmdline=cmdline, cwd=cwd,
            username=username,
            create_time=create_time, age_seconds=age,
            parent_pid=ppid, parent_name=track.parent_name,
            parent_alive=parent_alive,
            orphaned_for_seconds=orphaned_for,
            orphan_time_is_lower_bound=orphan_lower_bound,
            cpu_seconds_total=cpu_total,
            cpu_percent_window=window_pct,
            cpu_percent_lifetime=lifetime_pct,
            idle_for_seconds=idle_for,
            idle_time_is_lower_bound=idle_lower_bound,
            rss_bytes=rss, rss_delta_bytes=rss_delta,
            num_threads=int(info.get("num_threads") or 0),
            listening_ports=ports, established_connections=established_n,
            reasons=tuple(reasons), verdict=verdict, verdict_detail=detail,
            observed_seconds=observed,
            first_scan=first_scan or track.seen_scans <= 1,
        )

    def _update_track(self, pid: int, name: str, create_time: float,
                      cpu_total: float, rss: int, ppid: int,
                      parent_alive: bool,
                      lineage: dict[int, tuple[str, float]], now: float) -> _Track:
        """Create or advance the per-PID track, seeding honest first estimates.

        The subtle part is the FIRST sighting. A process that has been idle for
        six hours must not be reported as "idle for 0 seconds" just because we
        only started watching now — and on the next scan it must not say "idle
        for 20 minutes" either. So on first sighting we back-date the "since"
        timestamps from what the process itself tells us (its lifetime CPU
        average, its create_time) and mark them as estimates. From then on the
        numbers simply grow, and the estimate flags clear as soon as we observe
        the real events.
        """
        track = self._tracks.get(pid)
        # A PID can be reused by a different process; create_time disambiguates.
        if track is not None and abs(track.create_time - create_time) > 1.0:
            track = None

        if track is None:
            age = max(1.0, now - create_time)
            lifetime_pct = 100.0 * cpu_total / age
            # Burned almost no CPU across its whole life => it has effectively
            # been idle since it started. That is a lower bound, not a fact.
            idle_is_estimate = lifetime_pct < _WORKING_PCT
            last_activity = create_time if idle_is_estimate else now

            track = _Track(
                pid=pid, name=name, create_time=create_time,
                first_seen_wall=now,
                cpu_seconds_at_first_seen=cpu_total,
                rss_at_first_seen=rss,
                last_cpu_seconds=cpu_total,
                last_cpu_activity_wall=last_activity,
                parent_pid=ppid,
                parent_name=lineage.get(ppid, ("?", 0.0))[0],
                idle_since_is_estimate=idle_is_estimate,
            )
            if not parent_alive:
                # Already orphaned before we ever looked: the most we can say
                # is "at least as long as this process has been running".
                track.parent_missing_since_wall = create_time
                track.orphan_since_is_estimate = True
            self._tracks[pid] = track
            return track

        if cpu_total - track.last_cpu_seconds >= _CPU_ACTIVITY_EPSILON_SEC:
            # Observed real work: the timestamp is now a fact, not a guess.
            track.last_cpu_activity_wall = now
            track.idle_since_is_estimate = False
        track.last_cpu_seconds = cpu_total
        track.seen_scans += 1
        return track

    # ---- public read API ----

    def get_ghosts(self) -> list[GhostProcess]:
        with self._lock:
            return list(self._ghosts)

    def count(self) -> int:
        with self._lock:
            return len(self._ghosts)

    def last_scan_time(self) -> float | None:
        with self._lock:
            return self._last_scan_wall

    def trigger_rescan(self) -> None:
        try:
            self._do_scan()
        except Exception:
            log.exception("Manual ghost rescan failed")


# ---------------------------------------------------------------- helpers

def _connection_map() -> tuple[dict[int, set[int]], dict[int, int]]:
    """(pid -> listening ports, pid -> established connection count)."""
    listening: dict[int, set[int]] = {}
    established: dict[int, int] = {}
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        log.debug("net_connections unavailable to the sweeper", exc_info=True)
        return listening, established
    for c in conns:
        if not c.pid:
            continue
        if c.status == psutil.CONN_LISTEN:
            listening.setdefault(c.pid, set()).add(c.laddr.port)
        elif c.status == psutil.CONN_ESTABLISHED:
            established[c.pid] = established.get(c.pid, 0) + 1
    return listening, established


def _resolve_parent(ppid: int, child_create_time: float,
                    raw: dict[int, dict],
                    lineage: dict[int, tuple[str, float]]) -> tuple[str, bool]:
    """(parent name, parent is genuinely alive).

    A live PID matching `ppid` is only the real parent if it started BEFORE the
    child did. If it started after, the PID was recycled and the true parent is
    dead — the classic Windows trap this function exists to avoid.
    """
    if ppid <= 0:
        return ("?", False)

    live = raw.get(ppid)
    if live is not None:
        parent_create = float(live.get("create_time") or 0.0)
        if parent_create <= child_create_time + 1.0:
            return ((live.get("name") or "?").strip(), True)
        # PID reuse: this is a different, newer process wearing the same number.
        remembered = lineage.get(ppid)
        return ((remembered[0] if remembered else "?"), False)

    remembered = lineage.get(ppid)
    return ((remembered[0] if remembered else "?"), False)


def _classify(*, window_pct: float, lifetime_pct: float, idle_for: float | None,
              rss_delta: int, observed: float, established: int,
              reasons: list[str]) -> tuple[str, str]:
    """Turn the raw numbers into a verdict plus one explanatory sentence."""
    # Prefer the measured window; fall back to the lifetime average when we
    # have not been watching long enough for the window to mean anything.
    pct = window_pct if observed > 60 else lifetime_pct

    if established > 0 and pct < _SPINNING_PCT:
        # Someone is talking to it right now. It may well be orphaned, but it
        # is demonstrably still in use — which is the single most important
        # thing to tell the user before they close it.
        conns = ("חיבור רשת פעיל אחד" if established == 1
                 else f"{established} חיבורי רשת פעילים")
        return ("WORKING",
                f"יש לו {conns} כרגע — משהו עדיין משתמש בו. גם אם ההורה שלו "
                f"נסגר, סגירה עלולה לשבור משהו שרץ עכשיו.")

    if pct >= _SPINNING_PCT:
        return ("STUCK",
                f"שורף {pct:.0f}% מליבה ולא מסיים — התנהגות אופיינית "
                f"ללולאה תקועה, לא לעבודה אמיתית.")

    if rss_delta >= _LEAK_DELTA_BYTES and pct < _WORKING_PCT:
        return ("LEAKING",
                f"כמעט לא צורך CPU, אבל הזיכרון שלו גדל ב-{humanize_bytes(rss_delta)} "
                f"מאז שהתחלתי לעקוב — נראה כמו דליפת זיכרון.")

    if pct >= _WORKING_PCT:
        return ("WORKING",
                f"עדיין מבצע עבודה קלה ({pct:.1f}% מליבה) — ייתכן שהוא לא נטוש. "
                f"כדאי לבדוק לפני סגירה.")

    if "IDLE_SERVER" in reasons:
        return ("FINISHED",
                f"מאזין לפורט אבל אף אחד לא התחבר אליו כבר "
                f"{humanize_duration(idle_for)}, והוא לא ביצע שום עבודה — "
                f"שרת פיתוח שנשכח פתוח.")

    return ("FINISHED",
            f"לא ביצע שום עבודה כבר {humanize_duration(idle_for)}. "
            f"סיים את מה שהיה לו לעשות ונשאר תלוי באוויר.")


def _describe(pid: int) -> tuple[str, str, str, str]:
    """(exe, cmdline, cwd, username) — best effort, "" on AccessDenied.

    Called only for flagged PIDs: every one of these is a separate syscall
    that can block, so it is not worth paying for every process on the machine.
    """
    exe = cmdline = cwd = username = ""
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return (exe, cmdline, cwd, username)
    for attr, setter in (
        ("exe", lambda v: v or ""),
        ("cmdline", lambda v: " ".join(v or [])),
        ("cwd", lambda v: v or ""),
        ("username", lambda v: v or ""),
    ):
        try:
            value = setter(getattr(proc, attr)())
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            value = ""
        except Exception:
            log.debug("Could not read %s of PID %d", attr, pid, exc_info=True)
            value = ""
        if attr == "exe":
            exe = value
        elif attr == "cmdline":
            cmdline = value
        elif attr == "cwd":
            cwd = value
        else:
            username = value
    return (exe, cmdline, cwd, username)


_CURRENT_USER = getpass.getuser().lower()


def _is_current_user(username: str) -> bool:
    r"""True when `username` is the account this monitor runs as.

    psutil returns "DOMAIN\user" on Windows and a bare name elsewhere. An
    empty string means the lookup was denied, which in practice means a
    SYSTEM or service account — treated as "not ours".
    """
    if not username:
        return False
    tail = username.replace("/", "\\").split("\\")[-1].strip().lower()
    return tail == _CURRENT_USER


def _trim_lineage(lineage: dict[int, tuple[str, float]],
                  live_pids: set[int], keep_dead: int = 2000
                  ) -> dict[int, tuple[str, float]]:
    """Keep every live PID plus the most recently started dead ones.

    Dead entries are the valuable part (they name orphans' parents) but they
    must not accumulate forever on a machine that has been up for weeks.
    """
    dead = [(pid, entry) for pid, entry in lineage.items() if pid not in live_pids]
    if len(dead) <= keep_dead:
        return lineage
    dead.sort(key=lambda item: item[1][1], reverse=True)   # newest first
    trimmed = {pid: entry for pid, entry in lineage.items() if pid in live_pids}
    trimmed.update(dict(dead[:keep_dead]))
    return trimmed


# ---------------------------------------------------------------- singleton

_default_sweeper: GhostSweeper | None = None
_default_lock = threading.Lock()


def get_default_sweeper(*, scan_interval_seconds: float | None = None,
                        idle_seconds: float | None = None) -> GhostSweeper:
    """Return (and lazily create + start) the singleton sweeper."""
    global _default_sweeper
    with _default_lock:
        if _default_sweeper is None:
            interval, idle, names, instances = _config_values()
            _default_sweeper = GhostSweeper(
                scan_interval_seconds=(scan_interval_seconds
                                       if scan_interval_seconds is not None
                                       else interval),
                idle_seconds=(idle_seconds if idle_seconds is not None else idle),
                ignored_names=names,
                ignored_instances=instances,
            )
            _default_sweeper.start()
        return _default_sweeper


def peek_default_sweeper() -> GhostSweeper | None:
    """Return the singleton only if it was already started. Never creates one.

    The dock badge uses this so that merely drawing the dock cannot resurrect
    a sweeper the user disabled with --no-sweeper.
    """
    return _default_sweeper


def _config_values() -> tuple[float, float, frozenset[str], frozenset[str]]:
    try:
        import config as app_config
        cfg = (app_config.load_config().get("sweeper", {}) or {})
    except Exception:
        log.exception("Could not read sweeper config; using defaults")
        cfg = {}
    interval = float(cfg.get("scan_interval_minutes", 20) or 20) * 60.0
    idle = float(cfg.get("idle_minutes", 30) or 30) * 60.0
    names = frozenset(str(n).lower() for n in (cfg.get("ignored_names") or []))
    instances = frozenset(str(k) for k in (cfg.get("ignored_instances") or []))
    return interval, idle, names, instances


def persist_ignore(*, name: str | None = None,
                   instance_key: str | None = None) -> None:
    """Add an entry to the persistent ignore list in config.json."""
    try:
        import config as app_config
        cfg = app_config.load_config()
        sweeper = cfg.setdefault("sweeper", {})
        if name:
            lst = sweeper.setdefault("ignored_names", [])
            if name.lower() not in lst:
                lst.append(name.lower())
        if instance_key:
            lst = sweeper.setdefault("ignored_instances", [])
            if instance_key not in lst:
                lst.append(instance_key)
        app_config.save_config(cfg)
    except Exception:
        log.exception("Could not persist sweeper ignore entry")
