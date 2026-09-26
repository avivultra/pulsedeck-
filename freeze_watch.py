"""Black box for UI freezes.

The dock's Tk tick calls `beat()` about once a second. Each beat re-arms
`faulthandler.dump_traceback_later`; if no beat arrives for `timeout` seconds,
the stack of EVERY thread is written to `history/freeze.log`.

faulthandler is used instead of a Python watchdog thread because its timer is a
C thread that does not need the GIL. On 2026-09-22 the whole process froze —
the sweeper thread stopped logging too — which is exactly the case a Python
watchdog cannot see: it would be frozen alongside everything else.

Nothing here changes behaviour. It only records where the program was stuck,
so the next freeze can be diagnosed instead of guessed at.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import time
from pathlib import Path
from typing import TextIO

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 30.0
# A freeze log is a handful of stack dumps; if it ever grows past this, the
# old content is moved aside on the next start rather than kept forever.
_MAX_LOG_BYTES = 1024 * 1024

_fh: TextIO | None = None
_timeout = DEFAULT_TIMEOUT_SEC
_last_beat_mono: float | None = None


def start(path: Path, timeout: float = DEFAULT_TIMEOUT_SEC) -> None:
    """Open the freeze log and arm the first timer. Safe to call once."""
    global _fh, _timeout, _last_beat_mono
    if _fh is not None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _MAX_LOG_BYTES:
            path.replace(path.with_suffix(".log.old"))
        # Line-buffered text file kept open for the process lifetime:
        # faulthandler writes to its file descriptor directly.
        _fh = open(path, "a", encoding="utf-8", buffering=1)
        _fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} PulseDeck started, "
                  f"pid={os.getpid()}. A stack dump below this line means the "
                  f"UI stopped responding for {timeout:.0f}s. ---\n")
    except OSError:
        log.exception("Could not open freeze log %s", path)
        _fh = None
        return
    _timeout = float(timeout)
    _last_beat_mono = time.monotonic()
    faulthandler.dump_traceback_later(_timeout, repeat=False, file=_fh)


def beat() -> None:
    """Called from the UI tick. Re-arms the timer; notes recoveries."""
    global _last_beat_mono
    if _fh is None:
        return
    now = time.monotonic()
    if _last_beat_mono is not None and now - _last_beat_mono >= _timeout:
        # The dump above (if any) was a stall we survived — stamp it with a
        # wall-clock time, which faulthandler itself does not write.
        try:
            _fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} UI recovered "
                      f"after a {now - _last_beat_mono:.0f}s stall ---\n")
        except (OSError, ValueError):
            pass
    _last_beat_mono = now
    faulthandler.dump_traceback_later(_timeout, repeat=False, file=_fh)


def stop() -> None:
    """Disarm on a clean shutdown so closing the app never writes a dump."""
    global _fh
    faulthandler.cancel_dump_traceback_later()
    if _fh is not None:
        try:
            _fh.close()
        except OSError:
            pass
        _fh = None
