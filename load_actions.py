"""Easing the load: lower a heavy program's priority, or close it the way
clicking X does. Nothing here runs on its own.

Both ways Claude helps go through this module, under the same rules:
  - "Ask Claude" (API key): Claude suggests, the window shows a button per
    suggestion, and the user confirms each one in a dialog;
  - Claude Code (/pulsedeck load, or the MCP tools): Claude asks in the chat
    before every action.

The rules:
  - one program per action, checked again right before it runs;
  - never touched: Windows itself, the antivirus, PulseDeck, code editors,
    terminals, Claude, and servers the user keeps running
    (config sweeper.server_cwds);
  - "lower" is undone by "restore", or by the program's next start;
  - "close" asks the program's windows to close, like clicking X, so unsaved
    work prompts as usual. Forcing is a separate step, refused unless a
    normal close of that program was tried in the last ten minutes.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from i18n import tr

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
STATE_PATH = PROJECT_DIR / "history" / "load_actions.json"
WINDOW_SEC = 90             # "the last minute": the history keeps 30 s roll-ups
TOP = 6                     # programs looked at, by CPU and by memory
CLOSE_WAIT_SEC = 6.0        # how long a normal close may take
FORCE_WITHIN_SEC = 600      # forcing needs a normal close tried this recently

# Whole programs that are never touched, by name (lower case, no ".exe").
# Anything else that runs from the Windows folder counts as Windows too.
NEVER = {
    "system": {"system", "system idle process", "idle", "registry", "memory compression", "secure system",
               "smss", "csrss", "wininit", "winlogon", "services", "lsass", "lsaiso", "svchost", "dwm",
               "explorer", "fontdrvhost", "audiodg", "spoolsv", "sihost", "taskhostw", "ctfmon",
               "runtimebroker", "wmiprvse", "dllhost", "searchhost", "searchindexer",
               "startmenuexperiencehost", "shellexperiencehost", "textinputhost", "applicationframehost",
               "trustedinstaller", "tiworker", "wudfhost", "systemd", "init", "kernel_task", "launchd"},
    "antivirus": {"msmpeng", "nissrv", "mpdefendercoreservice", "mpcopyaccelerator", "securityhealthservice",
                  "securityhealthsystray", "smartscreen", "malwarebytes", "mbam", "mbamservice", "mbamtray",
                  "avp", "avpui", "avastsvc", "avastui", "avgsvc", "avgui", "ekrn", "egui", "bdagent",
                  "bdservicehost", "vsserv", "mcshield", "mfemms", "ccsvchst", "nortonsecurity", "wrsa",
                  "savservice", "sophosui"},
    "pulsedeck": {"pulsedeck"},
    "editor": {"code", "code - insiders", "cursor", "windsurf", "devenv", "idea64", "pycharm64", "webstorm64",
               "rider64", "clion64", "goland64", "phpstorm64", "studio64", "sublime_text", "notepad++", "zed",
               "vim", "nvim", "gvim", "emacs"},
    "terminal": {"windowsterminal", "wt", "openconsole", "conhost", "cmd", "powershell", "pwsh", "bash", "sh",
                 "wsl", "wslhost", "wslservice", "mintty", "alacritty", "wezterm-gui", "tabby", "conemu64",
                 "conemu", "cmder"},
    "claude": {"claude"},
}
# One name, many unrelated programs (every Python script is "python"):
# these can be lowered, but never closed by name.
HOSTS = {"python", "pythonw", "py", "pyw", "node", "java", "javaw", "dotnet", "ruby", "php", "perl", "deno",
         "bun", "rundll32", "wscript", "cscript", "msiexec"}
_PYTHON = {"python", "pythonw", "py", "pyw"}

# What a reason code means, for people (Hebrew, English).
REASONS = {
    "system": ("חלק מ-Windows", "part of Windows"),
    "antivirus": ("האנטי-וירוס", "the antivirus"),
    "pulsedeck": ("PulseDeck עצמו", "PulseDeck itself"),
    "editor": ("עורך קוד", "a code editor"),
    "terminal": ("טרמינל", "a terminal"),
    "claude": ("Claude", "Claude"),
    "server": ("שרת שסימנת שיישאר פתוח", "a server you keep running"),
    "self": ("מי שמבקש עכשיו", "the session asking"),
    "no_access": ("אין הרשאה אליו", "no access to it"),
    "host": ("הרבה תוכנות שונות נקראות כך", "many different programs share this name"),
}

if os.name == "nt":
    _BELOW = psutil.BELOW_NORMAL_PRIORITY_CLASS
    _ABOVE_BELOW = {psutil.NORMAL_PRIORITY_CLASS, psutil.ABOVE_NORMAL_PRIORITY_CLASS, psutil.HIGH_PRIORITY_CLASS}

    def _can_lower(priority: int) -> bool:
        return priority in _ABOVE_BELOW          # never realtime, never already low
else:
    _BELOW = 10

    def _can_lower(priority: int) -> bool:
        return priority < _BELOW


def _short(name: str) -> str:
    return name[:-4] if name.lower().endswith(".exe") else name


def _fmt_bytes(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


# ------------------------------------------------------------------ what is running

def _table() -> list[tuple[int, str, float, float, int]]:
    """(pid, name, create_time, cpu_seconds, rss) of every process."""
    try:
        import win_processes
        if win_processes.AVAILABLE:
            return win_processes.Snapshotter().read()
    except OSError:
        log.debug("Native process table unavailable", exc_info=True)
    out = []
    for p in psutil.process_iter(["pid", "name", "create_time", "cpu_times", "memory_info"]):
        i = p.info
        ct, mi = i.get("cpu_times"), i.get("memory_info")
        out.append((i["pid"], i["name"] or "", i["create_time"] or 0.0,
                    (ct.user + ct.system) if ct else 0.0, mi.rss if mi else 0))
    return out


def _measure(seconds: float = 1.0, top: int = TOP) -> dict[str, list[dict]]:
    """Who uses the computer right now, for when the history has nothing
    (PulseDeck was not running). Same shape as ProcessHistory.who_was_busy."""
    before = {(pid, round(ct)): cpu for pid, _n, ct, cpu, _r in _table()}
    t0 = time.monotonic()
    time.sleep(seconds)
    rows = _table()
    dt = max(time.monotonic() - t0, 0.001)
    cores = psutil.cpu_count() or 1
    by: dict[str, list] = {}
    for pid, name, ct, cpu, rss in rows:
        if not pid:                                  # the idle "process" is free CPU
            continue
        used = max(0.0, cpu - before.get((pid, round(ct)), cpu))
        e = by.setdefault(_short(name), [0.0, 0, 0])
        e[0] += used / dt / cores * 100
        e[1] += rss
        e[2] += 1
    items = [{"name": n, "cpu": round(c, 1), "rss": r, "count": k} for n, (c, r, k) in by.items()]
    return {"cpu": sorted((i for i in items if i["cpu"] >= 0.1), key=lambda i: i["cpu"], reverse=True)[:top],
            "memory": sorted(items, key=lambda i: i["rss"], reverse=True)[:top]}


def _top_windows() -> dict[int, list[int]]:
    """pid -> its visible, titled top-level windows: what clicking X closes."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")             # own copy: argtypes stay local
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    out: dict[int, list[int]] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4) \
                and user32.GetWindowTextLengthW(hwnd) > 0:       # 4 = GW_OWNER: not a dialog of another
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.setdefault(pid.value, []).append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return out


def _post_close(hwnds: list[int]) -> None:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    for h in hwnds:
        user32.PostMessageW(h, 0x0010, 0, 0)     # WM_CLOSE: what the X button sends


def _alive(pid: int, create_time: float) -> bool:
    from alerts import _still_running
    return _still_running(pid, create_time)


class _Guard:
    """Which processes are never touched; built once per look."""

    def __init__(self) -> None:
        from pulsedeck_mcp import _protected_pids, _server_dirs
        self.mine = _protected_pids()            # this process and its parents (the agent, its terminal)
        self.servers = _server_dirs()
        self.windows_dir = os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows")).rstrip("\\/") + os.sep

    def why(self, pid: int, key: str) -> str | None:
        from pulsedeck_mcp import _cmdline, is_pulsedeck
        if pid in (0, 4):
            return "system"
        if pid in self.mine:
            return "self"
        try:
            p = psutil.Process(pid)
            cmd = _cmdline(p) or ""
            if key in _PYTHON and is_pulsedeck(cmd):
                return "pulsedeck"
            exe = p.exe()
        except psutil.NoSuchProcess:
            return "gone"
        except psutil.Error:
            return "no_access"
        if exe and os.path.normcase(exe).startswith(self.windows_dir):
            return "system"
        if self.servers:
            from ghost_sweeper import protected_dir_of
            try:
                cwd = p.cwd()
            except (psutil.Error, OSError):
                cwd = ""
            if protected_dir_of(cwd, cmd, self.servers):
                return "server"
        return None


# ------------------------------------------------------------------ programs

@dataclass
class Program:
    name: str                        # as the history keeps it: "chrome", no ".exe"
    cpu: float = 0.0                 # average % of the whole machine over the window
    rss: int = 0                     # peak memory, bytes
    count: int = 0
    targets: list = field(default_factory=list)    # (pid, create_time, exe name, rss) it may act on
    never: str | None = None         # why the whole program is never touched (a REASONS code)
    skipped: dict = field(default_factory=dict)    # reason -> how many of its processes are left out
    has_window: bool = False
    lowered: bool = False            # PulseDeck lowered it and it is not restored yet

    @property
    def key(self) -> str:
        return self.name.lower()

    def can(self) -> list[str]:
        if self.never or not self.targets:
            return []
        out = ["restore" if self.lowered else "lower"]
        if self.key not in HOSTS:
            out.append("close")
        return out


def _classify(prog: Program, rows, guard: _Guard, windows: dict[int, list[int]], state: dict) -> Program:
    for reason, names in NEVER.items():
        if prog.key in names:
            prog.never = reason
            return prog
    for pid, name, ct, _cpu, rss in rows:
        if _short(name).lower() != prog.key:
            continue
        prog.name = _short(name)                 # the real spelling ("MsMpEng", not "msmpeng")
        why = guard.why(pid, prog.key)
        if why == "gone":                        # exited a moment ago
            continue
        if why:
            prog.skipped[why] = prog.skipped.get(why, 0) + 1
        else:
            prog.targets.append((pid, ct, name, rss))
    if not prog.targets and prog.skipped:
        prog.never = max(prog.skipped, key=prog.skipped.get)
    prog.has_window = any(pid in windows for pid, *_ in prog.targets)
    live = {(pid, round(ct)) for pid, ct, *_ in prog.targets}
    prog.lowered = any((pid, round(ct)) in live for pid, ct, _p in state["lowered"].get(prog.key, []))
    return prog


def heavy_programs(window_sec: float = WINDOW_SEC, top: int = TOP, *, history=None) -> tuple[list[Program], str]:
    """The programs that used the computer most over the last `window_sec`,
    each with what PulseDeck may do about it. Returns (programs, source):
    source is "history" (PulseDeck's per-program history) or "measured" (a
    one-second measurement, when there is no recent history)."""
    now = time.time()
    busy: dict[str, list[dict]] = {"cpu": [], "memory": []}
    try:
        if history is None:
            from process_history import get_default_history
            history = get_default_history()
        busy = history.who_was_busy(now - window_sec, now, top)
    except Exception:
        log.exception("Process history unavailable")
    source = "history"
    if not busy["cpu"] and not busy["memory"]:
        busy, source = _measure(top=top), "measured"
    merged: dict[str, Program] = {}
    for item in busy["cpu"] + busy["memory"]:
        merged.setdefault(item["name"].lower(), Program(item["name"], item["cpu"], item["rss"], item["count"]))
    rows, guard, windows, state = _table(), _Guard(), _top_windows(), _load_state()
    progs = [_classify(p, rows, guard, windows, state) for p in merged.values()]
    return progs, source


def _current(name: str) -> tuple[Program, list]:
    """One program as it is right now, checked from scratch."""
    rows = _table()
    prog = _classify(Program(_short(name.strip())), rows, _Guard(), _top_windows(), _load_state())
    return prog, rows


# ------------------------------------------------------------------ state

def _load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"lowered": dict(data.get("lowered") or {}), "close_tried": dict(data.get("close_tried") or {})}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except OSError:
        log.exception("Could not save %s", STATE_PATH)


# ------------------------------------------------------------------ actions

@dataclass
class Result:
    action: str                      # lower | restore | close | force
    outcome: str
    name: str
    done: int = 0
    total: int = 0
    freed: int = 0
    reason: str | None = None        # a REASONS code, when refused

    @property
    def ok(self) -> bool:
        return self.outcome in ("lowered", "restored", "closed")

    @property
    def can_force(self) -> bool:
        """A normal close did not end it: forcing is the next, separate step."""
        return self.action == "close" and self.outcome in ("still_open", "background", "no_window")

    def text(self, english: bool = False) -> str:
        he, en = _describe(self)
        return en if english else tr(he, en)


def _describe(r: Result) -> tuple[str, str]:
    n = r.name
    if r.outcome == "refused":
        he, en = REASONS.get(r.reason or "", ("מוגן", "protected"))
        if r.reason == "host":
            return (f"לא סוגר את {n} לפי שם: {he}. אפשר רק להוריד לו עדיפות.",
                    f"Not closing {n} by name: {en}. It can only be lowered.")
        return f"את {n} לא נוגעים: {he}.", f"{n} is never touched: {en}."
    if r.outcome == "not_running":
        return f"{n} לא רץ עכשיו.", f"{n} is not running now."
    if r.outcome == "lowered":
        return (f"העדיפות של {n} ירדה ({r.done} תהליכים). הוא ממשיך לעבוד, ומפנה מקום כשהמעבד עמוס.",
                f"Lowered {n} ({r.done} process(es)). It keeps working and gives way when the CPU is busy.")
    if r.outcome == "already":
        return f"העדיפות של {n} כבר נמוכה.", f"{n} already runs at low priority."
    if r.outcome == "restored":
        return f"העדיפות של {n} חזרה למה שהייתה.", f"{n} is back to its normal priority."
    if r.outcome == "nothing":
        return f"PulseDeck לא הוריד עדיפות ל-{n}.", f"PulseDeck has not lowered {n}."
    if r.outcome == "closed":
        freed = f" ושוחררו בערך {_fmt_bytes(r.freed)}" if r.freed else ""
        freed_en = f", freeing about {_fmt_bytes(r.freed)}" if r.freed else ""
        return f"{n} נסגר{freed}.", f"{n} closed{freed_en}."
    if r.outcome == "still_open":
        return (f"{n} עדיין פתוח. אולי הוא שואל משהו (למשל אם לשמור), אז כדאי להציץ בחלון שלו.",
                f"{n} is still open; it may be asking something (save?). Have a look at its window.")
    if r.outcome == "background":
        return (f"החלון של {n} נסגר, אבל התוכנה ממשיכה לרוץ ברקע.",
                f"{n}'s window closed, but the program keeps running in the background.")
    if r.outcome == "no_window":
        return (f"ל-{n} אין חלון לסגור. הוא נסגר רק בכוח.",
                f"{n} has no window to close; only a forced close would end it.")
    if r.outcome == "not_tried":
        return (f"סגירה בכוח רק אחרי ניסיון לסגור את {n} כרגיל (בעשר הדקות האחרונות).",
                f"A forced close comes only after a normal close of {n} was tried (in the last ten minutes).")
    if r.outcome == "partly":
        return (f"נסגרו {r.done} מתוך {r.total} תהליכים של {n}. על השאר Windows לא נתן הרשאה.",
                f"Closed {r.done} of {r.total} {n} processes; Windows refused the rest.")
    if r.outcome == "denied":
        return f"Windows לא נתן לשנות את {n}.", f"Windows refused to change {n}."
    return r.outcome, r.outcome


def lower(name: str) -> Result:
    """Run every allowed process of the program at below-normal priority.
    It keeps working, and gives way when the CPU is busy."""
    prog, _rows = _current(name)
    if prog.never:
        return Result("lower", "refused", prog.name, reason=prog.never)
    if not prog.targets:
        return Result("lower", "not_running", prog.name)
    state = _load_state()
    keep = [rec for rec in state["lowered"].get(prog.key, [])]
    done = already = denied = 0
    for pid, ct, _exe, _rss in prog.targets:
        try:
            p = psutil.Process(pid)
            if abs(p.create_time() - ct) > 1.0:
                continue                             # the pid now belongs to someone else
            before = int(p.nice())
            if not _can_lower(before):
                already += 1
                continue
            p.nice(_BELOW)
            keep = [r for r in keep if r[0] != pid] + [[pid, ct, before]]
            done += 1
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, OSError):
            denied += 1
    if done:
        state["lowered"][prog.key] = keep
        _save_state(state)
        log.info("Lowered %s (%d process(es))", prog.name, done)
    outcome = "lowered" if done else "already" if already else "denied" if denied else "not_running"
    return Result("lower", outcome, prog.name, done=done, total=len(prog.targets))


def restore(name: str) -> Result:
    """Put back the priority `lower` changed."""
    key = _short(name.strip()).lower()
    state = _load_state()
    records = state["lowered"].pop(key, [])
    if not records:
        return Result("restore", "nothing", _short(name.strip()))
    done = 0
    for pid, ct, before in records:
        try:
            p = psutil.Process(pid)
            if abs(p.create_time() - ct) > 1.0:
                continue
            p.nice(before)
            done += 1
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, OSError):
            log.warning("Could not restore the priority of %s (PID %d)", name, pid)
    _save_state(state)
    return Result("restore", "restored" if done else "not_running", _short(name.strip()),
                  done=done, total=len(records))


def close(name: str, wait: float = CLOSE_WAIT_SEC) -> Result:
    """Ask the program's windows to close, like clicking X, and wait up to
    `wait` seconds. Never forces: unsaved work prompts as usual. Blocking -
    call it off the Tk thread."""
    prog, _rows = _current(name)
    if prog.never:
        return Result("close", "refused", prog.name, reason=prog.never)
    if prog.key in HOSTS:
        return Result("close", "refused", prog.name, reason="host")
    if not prog.targets:
        return Result("close", "not_running", prog.name)
    state = _load_state()
    state["close_tried"][prog.key] = time.time()  # forcing may follow, and only after this
    _save_state(state)
    windows = _top_windows()
    hwnds = [h for pid, *_ in prog.targets for h in windows.get(pid, [])]
    total = len(prog.targets)
    if not hwnds:
        return Result("close", "no_window", prog.name, total=total)
    log.info("Asking %s to close (%d window(s))", prog.name, len(hwnds))
    _post_close(hwnds)
    deadline = time.monotonic() + wait
    alive = list(prog.targets)
    while alive and time.monotonic() < deadline:
        time.sleep(0.25)
        alive = [t for t in alive if _alive(t[0], t[1])]
    gone = [t for t in prog.targets if t not in alive]
    freed = sum(t[3] for t in gone)
    if not alive:
        return Result("close", "closed", prog.name, done=total, total=total, freed=freed)
    still = _top_windows()
    outcome = "still_open" if any(t[0] in still for t in alive) else "background"
    return Result("close", outcome, prog.name, done=len(gone), total=total, freed=freed)


def force_close(name: str) -> Result:
    """End the program's processes at once (unsaved work is lost). Refused
    unless a normal close of it was tried in the last ten minutes: the user
    approves this as a second, separate step."""
    from alerts import terminate_process
    prog, _rows = _current(name)
    if prog.never:
        return Result("force", "refused", prog.name, reason=prog.never)
    if prog.key in HOSTS:
        return Result("force", "refused", prog.name, reason="host")
    if not prog.targets:
        return Result("force", "not_running", prog.name)
    state = _load_state()
    if time.time() - float(state["close_tried"].get(prog.key, 0)) > FORCE_WITHIN_SEC:
        return Result("force", "not_tried", prog.name)
    done = freed = 0
    for pid, ct, exe, rss in prog.targets:
        outcome = terminate_process(pid, exe, ct)
        if outcome in ("closed", "already_gone"):
            done += 1
            freed += rss if outcome == "closed" else 0
    state["close_tried"].pop(prog.key, None)
    _save_state(state)
    total = len(prog.targets)
    outcome = "closed" if done == total else "partly" if done else "denied"
    return Result("force", outcome, prog.name, done=done, total=total, freed=freed)


# ------------------------------------------------------------------ for Claude

def for_claude(progs: list[Program]) -> list[dict]:
    """The "Ask Claude" snapshot's view: names and numbers only."""
    out = []
    for p in progs:
        item = {"name": p.name, "cpu_percent_of_machine": round(p.cpu, 1),
                "memory_mb": round(p.rss / 1024 ** 2), "count": p.count, "has_window": p.has_window}
        can = p.can()
        if can:
            item["can"] = can
        else:
            item["never_touched"] = p.never or "not_running"
        out.append(item)
    return out


def _counted(skipped: dict) -> str:
    return ", ".join(f"{k} {REASONS.get(r, ('', r))[1]}" for r, k in skipped.items())


def report(progs: list[Program], source: str, window_sec: float = WINDOW_SEC) -> str:
    """Plain text for Claude Code (the /pulsedeck skill and the MCP tool)."""
    vm = psutil.virtual_memory()
    when = (f"over the last {window_sec:.0f} s (PulseDeck's history)" if source == "history"
            else "measured just now for 1 s (no recent history: PulseDeck is not running)")
    lines = [f"Memory now {vm.percent:.0f}% ({_fmt_bytes(vm.used)} of {_fmt_bytes(vm.total)}).",
             f"Heaviest programs {when}; CPU is the average share of the whole machine, memory the peak:"]
    for p in progs:
        head = f"- {p.name}" + (f" ×{p.count}" if p.count > 1 else "") + \
               f": CPU {p.cpu:.1f}%, memory {_fmt_bytes(p.rss)}"
        if p.has_window:
            head += ", has a window"
        can = p.can()
        if can:
            head += ". Can: " + ", ".join(can)
            if p.key in HOSTS:
                head += " (not close: " + REASONS["host"][1] + ")"
            if p.lowered:
                head += ". Lowered earlier by PulseDeck"
        elif len(p.skipped) > 1:                     # say each reason: "2 servers, 1 PulseDeck" - not a guess
            head += ". Never touched: " + _counted(p.skipped)
        else:
            head += ". Never touched: " + (REASONS.get(p.never or "", ("", p.never or "not running now"))[1])
        if p.skipped and can:
            head += ". Left out: " + _counted(p.skipped)
        lines.append(head)
    lines.append("Every action is one program and needs the user's explicit yes for it, in the chat. "
                 "lower <name> / restore <name> change priority (reversible); quit <name> closes it like "
                 "clicking X; force-quit <name> only after quit did not end it, with a second yes.")
    return "\n".join(lines)
