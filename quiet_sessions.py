"""Quiet Claude Code conversations, and the shells they left open.

Every conversation opened since the PC started keeps a claude.exe of its own
(about half a gigabyte) until the Claude app quits, even when nobody has
written in it for a day. The owner used to reboot to get that memory back.
This module finds the ones that are provably quiet, so the user can close
them from PulseDeck. Closing one does not lose the conversation: the app
starts it again when it is opened.

A conversation is quiet when ALL of these hold:

* its transcript (~/.claude/projects/<project>/<session>.jsonl) has not
  changed for `QUIET_SEC`: every message, reply and tool result is written
  there, so nothing has happened in it for that long;
* it holds no established network connection: a conversation in the middle
  of a reply talks to Anthropic the whole time;
* it has no child process at work (a running shell command, a test run): only
  its own console host is allowed;
* it is not PulseDeck's own ancestor.

A conversation without a transcript cannot be proven quiet and is never
offered.

The Claude app also keeps one terminal (pwsh) per conversation, about 90 MB,
and that terminal outlives the conversation's process. A terminal is offered
with its conversation when no conversation that stays open shares its folder,
and on its own when no open conversation has its folder at all (one left by a
conversation that is already closed). It must be idle with nothing running
inside it, and older than `QUIET_SEC`.

Nothing here closes anything by itself: `find` lists, the user ticks, and
`verify` proves each one quiet again just before `close`. Titles and folders
are for the local dialog only; `summary` gives "Ask Claude" counts and
megabytes, never a name.

No tkinter here, so it stays testable without a display.
"""

from __future__ import annotations

import logging
import ntpath
import os
import time
from pathlib import Path

import psutil

import claude_sessions
from i18n import tr
from idle_cleanup import (MIB, CleanupTarget, _established_pids, _fmt_minutes,
                          _self_and_ancestors, close_targets, verify_idle)

log = logging.getLogger(__name__)

QUIET_SEC = 3600.0
SHELL_NAMES = frozenset({"pwsh.exe", "powershell.exe", "cmd.exe"})
# A conversation's own console host is not work; anything else under it is.
HARMLESS_CHILDREN = frozenset({"conhost.exe", "openconsole.exe"})


def _norm(path: str) -> str:
    return ntpath.normcase(path or "").rstrip("\\/")


def _read(proc: psutil.Process, attr: str, default=""):
    try:
        value = getattr(proc, attr)()
    except (psutil.Error, OSError):
        return default
    return value if value is not None else default


def _transcript_mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _session_detail(title: str, quiet_sec: float) -> str:
    quiet = _fmt_minutes(quiet_sec)
    if title:
        return tr(f"💬 {title}  ·  שקטה {quiet}", f"💬 {title}  ·  quiet for {quiet}")
    return tr(f"💬 שיחה בלי כותרת  ·  שקטה {quiet}", f"💬 Untitled conversation  ·  quiet for {quiet}")


def protected_settings() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(folders, title words) the user protects, from config.json "claude"."""
    try:
        import config as app_config
        from ghost_sweeper import normalize_dirs
        cfg = app_config.load_config().get("claude", {}) or {}
        words = tuple(str(w).strip().lower() for w in cfg.get("protected_chat_words") or [] if str(w).strip())
        return normalize_dirs(cfg.get("protected_chat_folders") or []), words
    except Exception:
        log.exception("Could not read the protected chats; protecting nothing extra")
        return (), ()


def _protected(cwd: str, title: str, dirs: tuple[str, ...], words: tuple[str, ...]) -> bool:
    from ghost_sweeper import protected_dir_of
    low = (title or "").lower()
    return protected_dir_of(cwd, "", dirs) is not None or any(w in low for w in words)


def find(now: float | None = None, min_quiet: float = QUIET_SEC,
         protected: tuple[tuple[str, ...], tuple[str, ...]] | None = None
         ) -> tuple[list[CleanupTarget], list[CleanupTarget]]:
    """(quiet conversations, their terminals), heaviest first. Blocking (it
    reads connections and a few transcripts' dates): call it off the Tk thread.
    `protected` = (folders, title words) never offered; default: config.json."""
    now = time.time() if now is None else now
    dirs, words = protected_settings() if protected is None else protected
    exclude = _self_and_ancestors()
    established = _established_pids()

    claude_procs: list[tuple[psutil.Process, str]] = []
    shells: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info["name"] or "").lower()
        if name in ("claude.exe", "claude"):
            claude_procs.append((proc, " ".join(_read(proc, "cmdline", []))))
        elif name in SHELL_NAMES:
            shells.append(proc)

    app_pids: set[int] = set()
    session_cwds: dict[int, str] = {}           # every open conversation
    sessions: list[CleanupTarget] = []
    for proc, cmd in claude_procs:
        if not claude_sessions.is_claude_code(proc.info["name"], _read(proc, "exe"), cmd):
            app_pids.add(proc.pid)
            continue
        cwd = _read(proc, "cwd")
        session_cwds[proc.pid] = _norm(cwd)
        if proc.pid in exclude or proc.pid in established:
            continue
        sid = claude_sessions.session_id(cmd)
        path = claude_sessions._find_transcript(sid) if sid else None
        mtime = _transcript_mtime(str(path)) if path else None
        if mtime is None or now - mtime < min_quiet:
            continue
        children = _read(proc, "children", [])
        if any(_read(c, "name").lower() not in HARMLESS_CHILDREN for c in children):
            continue
        title = claude_sessions.session_title(cmd) or ""
        if _protected(cwd, title, dirs, words):
            continue
        try:
            rss = proc.memory_info().rss
            created = proc.create_time()
        except (psutil.Error, OSError):
            continue
        sessions.append(CleanupTarget(
            pid=proc.pid, name=proc.info["name"], create_time=created, rss_bytes=rss,
            kind="claude", detail=_session_detail(title, now - mtime),
            member_pids=(proc.pid,), cwd=cwd, cmdline="",
            stamp_path=str(path), stamp_mtime=mtime))

    closing = {_norm(t.cwd) for t in sessions}
    staying = {c for pid, c in session_cwds.items() if pid not in {t.pid for t in sessions}}
    by_cwd = {_norm(t.cwd): t for t in sessions}
    terminals: list[CleanupTarget] = []
    for proc in shells:
        try:
            if proc.ppid() not in app_pids or proc.children():
                continue
            created = proc.create_time()
            if now - created < min_quiet:
                continue
            cwd = proc.cwd()
            rss = proc.memory_info().rss
        except (psutil.Error, OSError):
            continue
        key = _norm(cwd)
        if key in closing and key not in staying:
            owner = by_cwd[key].detail.split("  ·  ")[0].removeprefix("💬 ")
            detail = tr(f"🖥 המסוף של {owner}", f"🖥 The terminal of {owner}")
        elif key not in session_cwds.values():
            detail = tr("🖥 מסוף שנשאר משיחה שכבר נסגרה", "🖥 A terminal left by a conversation that is closed")
        else:
            continue
        terminals.append(CleanupTarget(
            pid=proc.pid, name=proc.info["name"], create_time=created, rss_bytes=rss,
            kind="terminal", detail=detail, member_pids=(proc.pid,), cwd=cwd, cmdline=""))

    sessions.sort(key=lambda t: t.rss_bytes, reverse=True)
    terminals.sort(key=lambda t: t.rss_bytes, reverse=True)
    return sessions, terminals


def verify(targets: list[CleanupTarget], sample_seconds: float | None = None
           ) -> tuple[list[CleanupTarget], list[tuple[CleanupTarget, str]]]:
    """Prove each one quiet again right now (3 s, see idle_cleanup.verify_idle),
    and that no conversation's transcript changed since the list was made."""
    kw = {} if sample_seconds is None else {"sample_seconds": sample_seconds}
    ok, skipped = verify_idle(targets, **kw)
    still: list[CleanupTarget] = []
    for t in ok:
        if t.stamp_path and _transcript_mtime(t.stamp_path) != t.stamp_mtime:
            skipped.append((t, tr("השיחה התעדכנה עכשיו", "the conversation just changed")))
        else:
            still.append(t)
    return still, skipped


def close(targets: list[CleanupTarget]) -> list[tuple[CleanupTarget, str]]:
    """Close what the user ticked and `verify` kept. Blocking."""
    return close_targets(targets)


def summary(found: tuple[list[CleanupTarget], list[CleanupTarget]] | None = None) -> dict:
    """For "Ask Claude": counts and megabytes only, never a name or a folder."""
    sessions, terminals = find() if found is None else found
    if not sessions and not terminals:
        return {}
    return {"quiet_sessions": len(sessions),
            "quiet_sessions_memory_mb": round(sum(t.rss_bytes for t in sessions) / MIB),
            "leftover_terminals": len(terminals),
            "leftover_terminals_memory_mb": round(sum(t.rss_bytes for t in terminals) / MIB),
            "quiet_for_over_minutes": int(QUIET_SEC // 60)}
