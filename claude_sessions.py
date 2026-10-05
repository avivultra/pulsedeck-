"""Name a Claude Code process by its conversation title.

Every open Claude Code conversation is a `claude.exe` that looks exactly like
every other one. To let the user see WHICH conversation a card is about, the
ghost panel shows the conversation's title next to the process name.

The title is read from the conversation's transcript file
(~/.claude/projects/<project>/<session-id>.jsonl), and NOTHING ELSE is:

* only lines that carry a title record are decoded at all; every other line is
  skipped as raw bytes without being parsed;
* only the title string leaves this module — never stored on disk, never
  written to any log, and cached in memory for a few minutes only;
* no session id on the command line, no transcript, or no title record means
  None, and the caller shows the folder alone.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

SESSION_FLAG_RE = re.compile(
    r"--(?:resume|session-id)(?:\s+|=)([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")

# Title records in the transcript. A user-chosen title wins over the one the
# app generated; the newest of each kind wins over older ones.
_CUSTOM_KEY = b'"custom-title"'
_AI_KEY = b'"ai-title"'

_BLOCK = 64 * 1024
# Title records are rewritten often, so the newest one sits near the end.
# Scanning further back than this is not worth the I/O.
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_TITLE_CHARS = 80
_CACHE_TTL_SEC = 300.0

_cache: dict[str, tuple[float, str | None]] = {}
_cache_lock = threading.Lock()


def is_claude_code(name: str, exe: str, cmdline: str) -> bool:
    """A Claude Code conversation process (not the Claude desktop app itself)."""
    if (name or "").lower() not in ("claude.exe", "claude"):
        return False
    text = f"{exe} {cmdline}".lower()
    return "claude-code" in text or SESSION_FLAG_RE.search(cmdline or "") is not None


def session_id(cmdline: str) -> str | None:
    m = SESSION_FLAG_RE.search(cmdline or "")
    return m.group(1).lower() if m else None


def projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def _find_transcript(sid: str) -> Path | None:
    root = projects_dir()
    try:
        for project in root.iterdir():
            candidate = project / f"{sid}.jsonl"
            if candidate.is_file():
                return candidate
    except OSError:
        return None
    return None


def _clean(title: object) -> str | None:
    if not isinstance(title, str):
        return None
    title = " ".join(title.split())          # no newlines / control whitespace
    if not title:
        return None
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1] + "…"
    return title


def _title_from_line(line: bytes, key: bytes, field: str) -> str | None:
    if key not in line:
        return None                          # never decoded
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("type") != key.strip(b'"').decode():
        return None
    return _clean(record.get(field))


def read_title(path: Path) -> str | None:
    """Newest custom title, else newest generated title, else None."""
    ai_title: str | None = None
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            carry = b""
            scanned = 0
            while pos > 0 and scanned < MAX_SCAN_BYTES:
                step = min(_BLOCK, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step) + carry
                scanned += step
                lines = chunk.split(b"\n")
                # The first piece may be the tail of a line that starts in the
                # previous block; keep it for the next round.
                carry = lines[0] if pos > 0 else b""
                complete = lines[1:] if pos > 0 else lines
                for line in reversed(complete):
                    custom = _title_from_line(line, _CUSTOM_KEY, "customTitle")
                    if custom:
                        return custom
                    if ai_title is None:
                        ai_title = _title_from_line(line, _AI_KEY, "aiTitle")
    except OSError:
        return None
    return ai_title


def session_title(cmdline: str) -> str | None:
    """Title of the conversation a Claude Code process belongs to, or None."""
    sid = session_id(cmdline)
    if sid is None:
        return None
    now = time.time()
    with _cache_lock:
        hit = _cache.get(sid)
        if hit is not None and now - hit[0] < _CACHE_TTL_SEC:
            return hit[1]
    path = _find_transcript(sid)
    title = read_title(path) if path is not None else None
    with _cache_lock:
        _cache[sid] = (now, title)
        if len(_cache) > 200:                # bounded: drop the oldest half
            for key, _v in sorted(_cache.items(), key=lambda kv: kv[1][0])[:100]:
                _cache.pop(key, None)
    return title
