"""Health Janitor — detect zombie conhost.exe processes spawned by dev tools
(claude-code CLI, electron, node) and offer one-click cleanup with explicit
user confirmation. NEVER kills automatically.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil

log = logging.getLogger(__name__)


# ---------- Audit logger (separate file: history/janitor.log) ----------

_audit_logger: logging.Logger | None = None


def _get_audit_logger() -> logging.Logger:
    """Lazy-init the audit logger that writes to history/janitor.log."""
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger

    audit = logging.getLogger("janitor.audit")
    audit.setLevel(logging.INFO)
    audit.propagate = False  # don't bubble up to monitor.log

    try:
        from metric_history import DEFAULT_HISTORY_DIR
        DEFAULT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        path = DEFAULT_HISTORY_DIR / "janitor.log"
        handler = RotatingFileHandler(
            path, maxBytes=512_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s — %(message)s",
                                               datefmt="%Y-%m-%d %H:%M:%S"))
        audit.addHandler(handler)
    except Exception:
        log.exception("Could not initialize janitor audit log")
    _audit_logger = audit
    return audit


# ---------- ZombieGroup dataclass ----------

@dataclass(frozen=True)
class ZombieGroup:
    parent_name: str
    parent_pid: int
    zombie_pids: tuple[int, ...]
    total_rss_bytes: int       # captured at scan time, never re-read on click
    scanned_at: float          # unix_time when this group was produced

    @property
    def count(self) -> int:
        return len(self.zombie_pids)


# ---------- JanitorScanner ----------

DEFAULT_SUSPICIOUS_PARENTS: frozenset[str] = frozenset({
    "claude.exe", "electron.exe", "node.exe",
    "python.exe", "pythonw.exe", "code.exe",
})


class JanitorScanner:
    """Background thread that periodically scans for conhost zombies.

    Thread-safe: scan results are protected by a lock; consumers (UI) read
    via `get_groups()` / `count_total_zombies()`.
    """

    def __init__(
        self,
        scan_interval_seconds: float = 300.0,
        conhost_threshold: int = 20,
        suspicious_parents: frozenset[str] | None = None,
    ) -> None:
        self._scan_interval = max(30.0, float(scan_interval_seconds))
        self._threshold = max(2, int(conhost_threshold))
        self._suspicious = (suspicious_parents
                            if suspicious_parents is not None
                            else DEFAULT_SUSPICIOUS_PARENTS)
        self._lock = threading.Lock()
        self._groups: list[ZombieGroup] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._self_pid = os.getpid()

    # ---- Lifecycle ----

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # Run the first scan synchronously so the indicator has data right away
        try:
            self._do_scan()
        except Exception:
            log.exception("Initial janitor scan failed")
        self._thread = threading.Thread(target=self._run, name="janitor-scanner",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self._scan_interval):
                break
            try:
                self._do_scan()
            except Exception:
                log.exception("Janitor scan tick failed")

    # ---- Scanning ----

    def _do_scan(self) -> None:
        groups = self.scan()
        with self._lock:
            self._groups = groups

    def scan(self) -> list[ZombieGroup]:
        """One-shot scan; returns groups whose count >= threshold."""
        # parent_pid -> {parent_name, child_pids, total_rss}
        buckets: dict[int, dict] = {}
        now = time.time()

        for proc in psutil.process_iter(["pid", "name", "ppid", "memory_info"]):
            try:
                info = proc.info
                name = (info.get("name") or "").strip().lower()
                if name != "conhost.exe":
                    continue
                ppid = int(info.get("ppid") or 0)
                if ppid <= 0 or ppid == self._self_pid:
                    continue

                # Resolve parent name (cheap) — fall back to "unknown" if dead
                parent_name = ""
                try:
                    parent = psutil.Process(ppid)
                    parent_name = (parent.name() or "").strip().lower()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

                if parent_name not in self._suspicious:
                    continue

                pid = int(info.get("pid") or 0)
                if pid <= 0 or pid == self._self_pid:
                    continue

                mem = info.get("memory_info")
                rss = int(getattr(mem, "rss", 0)) if mem is not None else 0

                bucket = buckets.setdefault(ppid, {
                    "parent_name": parent_name,
                    "child_pids": [],
                    "rss_total": 0,
                })
                bucket["child_pids"].append(pid)
                bucket["rss_total"] += rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        groups: list[ZombieGroup] = []
        for ppid, b in buckets.items():
            if len(b["child_pids"]) >= self._threshold:
                groups.append(ZombieGroup(
                    parent_name=b["parent_name"],
                    parent_pid=ppid,
                    zombie_pids=tuple(b["child_pids"]),
                    total_rss_bytes=b["rss_total"],
                    scanned_at=now,
                ))
        # Sort by count descending — biggest zombie clusters first
        groups.sort(key=lambda g: g.count, reverse=True)
        return groups

    # ---- Public read API ----

    def get_groups(self) -> list[ZombieGroup]:
        with self._lock:
            return list(self._groups)

    def count_total_zombies(self) -> int:
        with self._lock:
            return sum(g.count for g in self._groups)

    def trigger_rescan(self) -> None:
        """Force an immediate scan (e.g. after killing a group)."""
        try:
            self._do_scan()
        except Exception:
            log.exception("Manual janitor rescan failed")

    # ---- Killing ----

    def kill_group(self, group: ZombieGroup) -> tuple[int, int]:
        """Kill all PIDs in the group. Returns (killed, total).

        Caller is responsible for getting user confirmation BEFORE calling this.
        Self-PID and protected names are guarded as defense-in-depth.
        """
        # Defense in depth — never kill self or protected names.
        try:
            from alerts import _is_protected
        except ImportError:
            def _is_protected(_pid, _name):
                return False

        killed = 0
        for pid in group.zombie_pids:
            if pid == self._self_pid:
                log.warning("Refusing to kill self-PID %d via janitor", pid)
                continue
            try:
                proc = psutil.Process(pid)
                pname = (proc.name() or "").strip().lower()
                if _is_protected(pid, pname):
                    log.warning("Skipping protected process %s (PID %d)", pname, pid)
                    continue
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except psutil.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
                killed += 1
            except psutil.NoSuchProcess:
                # Already gone — count as success since the goal is achieved
                killed += 1
            except (psutil.AccessDenied, OSError):
                log.exception("Failed to kill PID %d in zombie group", pid)

        # Audit
        try:
            audit = _get_audit_logger()
            freed_mib = group.total_rss_bytes / (1024 * 1024)
            audit.info(
                "killed %d/%d conhost zombies (parent %s PID %d, freed ~%.0f MiB)",
                killed, group.count, group.parent_name, group.parent_pid, freed_mib,
            )
        except Exception:
            log.exception("Audit logging failed")

        # Refresh internal state so the indicator updates promptly
        self.trigger_rescan()
        return killed, group.count


# ---------- Singleton ----------

_default_janitor: JanitorScanner | None = None
_default_lock = threading.Lock()


def get_default_janitor(*, scan_interval_seconds: float = 300.0,
                        conhost_threshold: int = 20) -> JanitorScanner:
    """Return (and lazily create+start) the singleton JanitorScanner."""
    global _default_janitor
    with _default_lock:
        if _default_janitor is None:
            _default_janitor = JanitorScanner(
                scan_interval_seconds=scan_interval_seconds,
                conhost_threshold=conhost_threshold,
            )
            _default_janitor.start()
        return _default_janitor


# ---------- Cleanup window UI ----------

def open_cleanup_panel(parent) -> "object":
    """Open the cleanup Toplevel listing zombie groups; returns the window."""
    import tkinter as tk
    from tkinter import messagebox

    janitor = get_default_janitor()
    janitor.trigger_rescan()

    # Theme matches alerts.py
    BG, PANEL, PANEL_HI = "#0d1117", "#161b22", "#1c2230"
    BORDER, FG, DIM = "#30363d", "#e6edf3", "#7d8590"
    AMBER, BAD = "#ebcb8b", "#ff5c6c"

    win = tk.Toplevel(parent) if parent is not None else tk.Tk()
    win.title("ניקוי תהליכים מיותרים")
    win.geometry("560x460")
    win.configure(bg=BG)
    try:
        win.attributes("-topmost", True)
    except tk.TclError:
        pass

    # Header
    hdr = tk.Frame(win, bg=PANEL, padx=18, pady=14)
    hdr.pack(fill="x")
    tk.Label(hdr, text="🧹 ניקוי תהליכים מיותרים", bg=PANEL, fg=AMBER,
             font=("Segoe UI", 14, "bold"), anchor="e").pack(fill="x")
    subtitle_var = tk.StringVar(value="")
    tk.Label(hdr, textvariable=subtitle_var, bg=PANEL, fg=DIM,
             font=("Segoe UI", 9), anchor="e").pack(fill="x", pady=(4, 0))

    # Body container
    body = tk.Frame(win, bg=BG, padx=14, pady=10)
    body.pack(fill="both", expand=True)

    def _kill_one(group: ZombieGroup) -> None:
        if not messagebox.askyesno(
            "אישור ניקוי",
            f"להרוג {group.count} תהליכי conhost מההורה {group.parent_name} "
            f"(PID {group.parent_pid})?",
            parent=win,
        ):
            return
        killed, total = janitor.kill_group(group)
        messagebox.showinfo(
            "ניקוי הושלם",
            f"נוקו {killed}/{total} תהליכים מ-{group.parent_name}",
            parent=win,
        )
        _refresh()

    def _kill_all() -> None:
        groups = janitor.get_groups()
        total = sum(g.count for g in groups)
        if total == 0:
            return
        if not messagebox.askyesno(
            "אישור ניקוי כללי",
            f"להרוג {total} תהליכי conhost מ-{len(groups)} הורים שונים?",
            parent=win,
        ):
            return
        total_killed = 0
        for g in groups:
            killed, _ = janitor.kill_group(g)
            total_killed += killed
        messagebox.showinfo("ניקוי הושלם",
                            f"נוקו {total_killed}/{total} תהליכים", parent=win)
        _refresh()

    def _refresh() -> None:
        for child in body.winfo_children():
            child.destroy()
        groups = janitor.get_groups()
        total = sum(g.count for g in groups)
        subtitle_var.set(f"{len(groups)} קבוצות  ·  {total} תהליכים בסך הכל")

        if not groups:
            tk.Label(body, text="✓ אין תהליכים מיותרים לזיהוי",
                     bg=BG, fg="#3fb950", font=("Segoe UI", 11),
                     pady=40).pack(fill="x")
            return

        for g in groups:
            outer = tk.Frame(body, bg=BORDER)
            outer.pack(fill="x", pady=4)
            inner = tk.Frame(outer, bg=PANEL_HI)
            inner.pack(fill="both", expand=True, padx=1, pady=1)
            stripe = tk.Frame(inner, bg=AMBER, width=3)
            stripe.pack(side="left", fill="y")
            content = tk.Frame(inner, bg=PANEL_HI, padx=12, pady=10)
            content.pack(side="left", fill="both", expand=True)

            top = tk.Frame(content, bg=PANEL_HI)
            top.pack(fill="x")
            tk.Label(top, text=f"{g.parent_name}  ·  PID {g.parent_pid}",
                     bg=PANEL_HI, fg=FG, font=("Segoe UI", 11, "bold"),
                     anchor="e").pack(side="right", fill="x", expand=True)

            mib = g.total_rss_bytes / (1024 * 1024)
            tk.Label(content,
                     text=f"{g.count} conhost.exe  ·  ~{mib:.0f} MiB",
                     bg=PANEL_HI, fg=DIM, font=("Segoe UI", 9),
                     anchor="e").pack(fill="x", pady=(2, 0))

            action = tk.Frame(content, bg=PANEL_HI)
            action.pack(fill="x", pady=(6, 0))
            tk.Button(action, text="✕  נקה הכל", bg=PANEL_HI, fg=BAD,
                      font=("Segoe UI", 9, "bold"), relief="flat", bd=0,
                      activebackground=BAD, activeforeground="white",
                      cursor="hand2", padx=10, pady=3,
                      command=lambda gr=g: _kill_one(gr)).pack(side="left")

    # Footer
    ftr = tk.Frame(win, bg=PANEL, padx=14, pady=10)
    ftr.pack(fill="x", side="bottom")
    tk.Button(ftr, text="🧹 נקה את הכל", bg=AMBER, fg=BG,
              font=("Segoe UI", 10, "bold"), relief="flat", bd=0,
              activebackground="#d4b06b", activeforeground=BG,
              cursor="hand2", padx=14, pady=6,
              command=_kill_all).pack(side="left")
    tk.Button(ftr, text="רענן", bg=PANEL_HI, fg=FG,
              font=("Segoe UI", 9), relief="flat", bd=0,
              activebackground=BORDER, activeforeground=FG,
              cursor="hand2", padx=12, pady=6,
              command=_refresh).pack(side="left", padx=(8, 0))
    tk.Button(ftr, text="סגור", bg=PANEL_HI, fg=FG,
              font=("Segoe UI", 9), relief="flat", bd=0,
              activebackground=BAD, activeforeground="white",
              cursor="hand2", padx=12, pady=6,
              command=win.destroy).pack(side="right")

    _refresh()
    return win
