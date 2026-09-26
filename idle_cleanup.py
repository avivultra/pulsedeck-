"""'Clean all' — close everything that is provably idle, nothing that is active.

Two sources of candidates:

* Ghosts the sweeper judged FINISHED. STUCK (burning CPU), LEAKING (memory
  still moving) and WORKING (doing work / holding a connection) are never
  offered — the user's rule is "never touch anything active".
* Idle memory hogs: processes of the current user that hold at least
  `MIN_HOG_RSS` and have not used the CPU for `MIN_IDLE_SEC`. Windows itself,
  services, security software, anything with a window on screen (or whose
  parent has one), anything on the network, and the sweeper's own domain
  (dev runtimes, agents) are excluded.

Idleness is proven twice. Once from history — the background process sampler
records the last time each process used ≥1 % of a core. And once more at the
moment the user confirms (`verify_idle`): a fresh CPU/RAM/window/network
sample. A process that woke up in between drops out and is reported as
skipped. Nothing is ever closed without the user's click.

No tkinter here, so it stays testable without a display.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import psutil

from i18n import tr

log = logging.getLogger(__name__)

MIB = 1024 * 1024
MIN_HOG_RSS = 100 * MIB          # smaller processes are not worth a prompt
MIN_IDLE_SEC = 5 * 60.0          # "not active" = no CPU use for 5 minutes
VERIFY_SAMPLE_SEC = 3.0
# CPU seconds allowed during the verification sample. 0.05 s over 3 s is
# under 2 % of one core — a timer tick, not work.
VERIFY_CPU_EPSILON_SEC = 0.05
VERIFY_RSS_GROWTH_BYTES = 20 * MIB

# Never offered as idle hogs even though they run as the user. The Windows
# directory rule below already covers most OS processes; these live elsewhere.
KEEP_NAMES: frozenset[str] = frozenset({
    "msmpeng.exe", "mpdefendercoreservice.exe", "nissrv.exe",
    "securityhealthsystray.exe", "securityhealthservice.exe",
    "nvcontainer.exe", "nvdisplay.container.exe", "nvidia share.exe",
    "igfxem.exe", "igfxhk.exe", "igfxtray.exe",
    "onedrive.exe", "dropbox.exe", "googledrivefs.exe",   # sync mid-upload
    "keepass.exe", "1password.exe", "bitwarden.exe",       # password vaults
})


@dataclass(frozen=True)
class CleanupTarget:
    pid: int
    name: str
    create_time: float
    rss_bytes: int                  # whole family (same-name children included)
    kind: str                       # "ghost" | "idle"
    detail: str                     # Hebrew one-liner shown in the dialog
    member_pids: tuple[int, ...] = field(default_factory=tuple)
    ghost: object | None = None     # the GhostProcess, for the sweep log


# ---------------------------------------------------------------- windows

def visible_window_pids() -> set[int]:
    """PIDs that own a visible, titled, non-cloaked top-level window.

    A minimised window still counts — it is something the user has open.
    Tray-only apps own hidden windows and do not count.
    """
    if os.name != "nt":
        return set()
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return set()

    user32 = ctypes.windll.user32
    try:
        dwmapi = ctypes.windll.dwmapi
    except OSError:
        dwmapi = None
    pids: set[int] = set()
    DWMWA_CLOAKED = 14

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            if dwmapi is not None:
                cloaked = wintypes.DWORD(0)
                if dwmapi.DwmGetWindowAttribute(
                        hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked),
                        ctypes.sizeof(cloaked)) == 0 and cloaked.value:
                    return True     # UWP app suspended in the background
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.add(int(pid.value))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:
        log.debug("EnumWindows failed", exc_info=True)
    return pids


def _ancestor_pids(pid: int) -> list[int]:
    try:
        return [p.pid for p in psutil.Process(pid).parents()]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


def _self_and_ancestors() -> set[int]:
    me = os.getpid()
    return {me, *_ancestor_pids(me)}


def _network_pids() -> set[int]:
    """PIDs listening on a port or holding an established connection."""
    out: set[int] = set()
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.pid and c.status in (psutil.CONN_LISTEN, psutil.CONN_ESTABLISHED):
                out.add(c.pid)
    except (psutil.AccessDenied, OSError):
        log.debug("net_connections unavailable", exc_info=True)
    return out


def _established_pids() -> set[int]:
    out: set[int] = set()
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.pid and c.status == psutil.CONN_ESTABLISHED:
                out.add(c.pid)
    except (psutil.AccessDenied, OSError):
        log.debug("net_connections unavailable", exc_info=True)
    return out


def _windows_dir() -> str:
    return os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows")).rstrip("\\/") + os.sep


def _idle_for(info) -> float:
    """Seconds since the sampler last saw ≥1 % of a core (lower bound)."""
    if info.last_active_seconds_ago is not None:
        return float(info.last_active_seconds_ago)
    return float(info.observed_seconds)


def _fmt_minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return tr(f"{minutes} דק'", f"{minutes} min")
    return tr(f"{minutes // 60} שע' {minutes % 60} דק'",
              f"{minutes // 60} h {minutes % 60} min")


# ---------------------------------------------------------------- candidates

def ghost_targets(ghosts) -> list[CleanupTarget]:
    """FINISHED ghosts only. Every other verdict is some form of 'active'."""
    windowed = visible_window_pids()
    out: list[CleanupTarget] = []
    for g in ghosts:
        if g.verdict != "FINISHED" or g.pid in windowed:
            continue
        out.append(CleanupTarget(
            pid=g.pid, name=g.name, create_time=g.create_time,
            rss_bytes=g.rss_bytes, kind="ghost",
            detail=tr(f"נטוש · לא עשה כלום {_fmt_minutes(g.idle_for_seconds or 0)}",
                      f"Abandoned · idle for {_fmt_minutes(g.idle_for_seconds or 0)}"),
            member_pids=(g.pid,), ghost=g,
        ))
    return out


def idle_hog_targets(sampler, *, exclude_pids: set[int] | None = None,
                     min_rss: int = MIN_HOG_RSS,
                     min_idle: float = MIN_IDLE_SEC) -> tuple[list[CleanupTarget], str]:
    """(targets, note). `note` explains an empty list when that is useful."""
    from ghost_sweeper import INTERESTING_NAMES, NEVER_REPORT, _is_current_user
    from alerts import PROTECTED_NAMES

    if sampler is None:
        return [], tr("דוגם התהליכים לא פעיל, אז אי אפשר להוכיח שתהליך לא פעיל.",
                      "The process sampler is not running, so idleness cannot be proven.")
    snap = sampler.snapshot()
    if not snap:
        return [], tr("עדיין אין נתונים — נסה שוב בעוד דקה.",
                      "No data yet — try again in a minute.")
    by_pid = {p.pid: p for p in snap}
    longest_watch = max(p.observed_seconds for p in snap)
    if longest_watch < min_idle:
        left = int((min_idle - longest_watch) // 60) + 1
        return [], tr(f"המוניטור רץ פחות מ-{int(min_idle // 60)} דקות. "
                      f"בעוד כ-{left} דק' אפשר יהיה להוכיח מה לא פעיל.",
                      f"The monitor has been running for less than {int(min_idle // 60)} minutes. "
                      f"In about {left} min it will be able to prove what is idle.")

    exclude = set(exclude_pids or ()) | _self_and_ancestors()
    skip_names = INTERESTING_NAMES | NEVER_REPORT | PROTECTED_NAMES | KEEP_NAMES
    windowed = visible_window_pids()
    networked = _network_pids()
    win_dir = _windows_dir()
    out: list[CleanupTarget] = []

    for info in snap:
        low = info.name.lower()
        if info.pid in exclude or low in skip_names:
            continue
        if _idle_for(info) < min_idle:
            continue
        try:
            proc = psutil.Process(info.pid)
            parent = proc.parent()
            # A same-name child (browser renderer, Electron helper) belongs to
            # its parent's family and is judged with it, never on its own.
            if parent is not None and parent.name().lower() == low:
                continue
            family = [proc] + [c for c in proc.children(recursive=True)
                               if c.name().lower() == low]
            family_rss = sum(by_pid[m.pid].rss_bytes if m.pid in by_pid
                             else m.memory_info().rss for m in family)
            if family_rss < min_rss:
                continue
            if not _is_current_user(proc.username()):
                continue
            exe = os.path.normcase(proc.exe() or "")
            if not exe or exe.startswith(win_dir):
                continue
            member_pids = tuple(m.pid for m in family)
            # Every member must be idle too: a quiet browser shell with a busy
            # renderer is a busy browser.
            if any(_idle_for(by_pid[m]) < min_idle
                   for m in member_pids if m in by_pid):
                continue
            if any(m in networked for m in member_pids):
                continue
            if any(p in windowed for p in (*member_pids, *_ancestor_pids(info.pid))):
                continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        extra = (tr(f" (+{len(member_pids) - 1} תהליכי משנה)",
                    f" (+{len(member_pids) - 1} child processes)")
                 if len(member_pids) > 1 else "")
        out.append(CleanupTarget(
            pid=info.pid, name=info.name, create_time=proc.create_time(),
            rss_bytes=family_rss, kind="idle",
            detail=tr(f"לא השתמש במעבד לפחות {_fmt_minutes(_idle_for(info))}, "
                      f"בלי חלון ובלי רשת{extra}",
                      f"No CPU use for at least {_fmt_minutes(_idle_for(info))}, "
                      f"no window and no network{extra}"),
            member_pids=member_pids,
        ))
    out.sort(key=lambda t: t.rss_bytes, reverse=True)
    return out, ""


def claude_sessions_note(exclude_pids: set[int]) -> str:
    """Hebrew hint about open Claude Code sessions, which are never offered as
    idle hogs: a session waiting for your next message looks idle but is not."""
    count, rss = 0, 0
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            if proc.info["pid"] in exclude_pids:
                continue
            if (proc.info["name"] or "").lower() not in ("claude.exe", "claude"):
                continue
            if "claude-code" not in " ".join(proc.cmdline()).lower():
                continue
            count += 1
            rss += proc.info["memory_info"].rss if proc.info["memory_info"] else 0
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if count == 0:
        return ""
    return tr(f"ℹ עוד {count} שיחות Claude Code פתוחות (~{rss // MIB} MB) לא נכללות — "
              f"שיחה שמחכה לך נראית לא פעילה אבל היא בשימוש. רק שיחות שהמטאטא "
              f"זיהה כנטושות נכללות למעלה. את השאר עדיף לסגור מתוך אפליקציית Claude.",
              f"ℹ {count} more open Claude Code sessions (~{rss // MIB} MB) are not included — "
              f"a session waiting for you looks idle but is in use. Only sessions the sweeper "
              f"identified as abandoned are listed above. Close the rest from the Claude app.")


# ---------------------------------------------------------------- verification

def _cpu_and_rss(pid: int, create_time: float | None) -> tuple[float, int] | None:
    try:
        proc = psutil.Process(pid)
        if create_time is not None and abs(proc.create_time() - create_time) > 1.0:
            return None
        t = proc.cpu_times()
        return (t.user + t.system, proc.memory_info().rss)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def verify_idle(targets: list[CleanupTarget], sample_seconds: float = VERIFY_SAMPLE_SEC
                ) -> tuple[list[CleanupTarget], list[tuple[CleanupTarget, str]]]:
    """Re-prove idleness right now. Blocks for `sample_seconds` — run it off
    the Tk thread. Returns (still idle, [(woke up, Hebrew reason)])."""
    before: dict[int, tuple[float, int] | None] = {}
    for t in targets:
        for m in t.member_pids or (t.pid,):
            before[m] = _cpu_and_rss(m, t.create_time if m == t.pid else None)
    time.sleep(max(0.0, sample_seconds))
    windowed = visible_window_pids()
    established = _established_pids()

    ok: list[CleanupTarget] = []
    skipped: list[tuple[CleanupTarget, str]] = []
    for t in targets:
        members = t.member_pids or (t.pid,)
        if before.get(t.pid) is None or _cpu_and_rss(t.pid, t.create_time) is None:
            skipped.append((t, tr("כבר לא רץ", "no longer running")))
            continue
        cpu_used = 0.0
        rss_growth = 0
        for m in members:
            b = before.get(m)
            a = _cpu_and_rss(m, t.create_time if m == t.pid else None)
            if b is None or a is None:
                continue
            cpu_used += a[0] - b[0]
            rss_growth += a[1] - b[1]
        if cpu_used > VERIFY_CPU_EPSILON_SEC:
            skipped.append((t, tr("התעורר — משתמש במעבד עכשיו",
                                  "woke up — using the CPU now")))
        elif rss_growth > VERIFY_RSS_GROWTH_BYTES:
            skipped.append((t, tr("הזיכרון שלו גדל עכשיו",
                                  "its memory is growing now")))
        elif any(m in established for m in members):
            skipped.append((t, tr("יש לו חיבור רשת פעיל",
                                  "it has an active network connection")))
        elif any(m in windowed for m in members):
            skipped.append((t, tr("נפתח לו חלון", "it opened a window")))
        else:
            ok.append(t)
    return ok, skipped


def close_targets(targets: list[CleanupTarget]) -> list[tuple[CleanupTarget, str]]:
    """Terminate each target (main PID; same-name children follow it).
    Blocks — run off the Tk thread. Returns [(target, outcome)]."""
    from alerts import terminate_process

    results: list[tuple[CleanupTarget, str]] = []
    for t in targets:
        outcome = terminate_process(t.pid, t.name, t.create_time)
        if outcome == "closed" and len(t.member_pids) > 1:
            # Chromium/Electron helpers normally exit with their parent; give
            # them a moment, then close any straggler of the same family.
            time.sleep(0.5)
            for m in t.member_pids[1:]:
                try:
                    if psutil.Process(m).name().lower() == t.name.lower():
                        terminate_process(m, t.name, timeout=1.0)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        results.append((t, outcome))
        _log_close(t, outcome)
    return results


def _log_close(t: CleanupTarget, outcome: str) -> None:
    try:
        from ghost_sweeper import _get_sweep_logger
        _get_sweep_logger().info(
            "USER clean-all %s: %s pid=%d kind=%s rss=%dMB (%s)",
            outcome, t.name, t.pid, t.kind, t.rss_bytes // MIB, t.detail,
        )
    except Exception:
        log.exception("Could not write clean-all action to the sweep log")
