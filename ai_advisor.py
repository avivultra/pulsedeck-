"""Ask Claude what is going on with the computer.

Runs only when the user presses the button — nothing here touches the network
or costs anything in the background. What is sent is a small, readable
snapshot: current CPU / memory / disk / temperature, the heaviest programs
*by name* (same-name processes folded), and today's load events. Never command
lines, paths, window titles or user names.

Setup: put the key in a `.env` file next to monitor.py (see .env.example):

    ANTHROPIC_API_KEY=sk-ant-...
    # optional
    PULSEDECK_CLAUDE_MODEL=claude-opus-5-5

Variables already set in the environment win over the file.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from i18n import tr

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"
DEFAULT_MODEL = "claude-opus-5-5"
_MAX_TOKENS = 4000

_SYSTEM_PROMPT = """\
You are the assistant inside PulseDeck, a small performance monitor on a \
Windows PC. The user pressed "Ask Claude" because they want to understand \
what their computer is doing right now — usually "why is it slow?".

You get a JSON snapshot: current load, the heaviest programs by name (same-name \
processes are summed; "count" is how many), and today's load events with the \
programs that were suspected at the time.

Answer in {language}, in plain words for a non-technical person:
1. One sentence: is everything fine, or what is the problem.
2. The likely cause, naming the program(s) from the snapshot. Say what a \
program is if the name is cryptic (e.g. MsMpEng.exe is the Windows Defender \
antivirus).
3. Up to three concrete things to do, safest first. Never suggest closing \
Windows system processes or the antivirus; when closing a program is the \
answer, say to close it normally (save work first), not to kill it.

Keep it short — under 150 words unless the user asks a specific question. \
If the snapshot is not enough to know, say so instead of guessing."""


class AdvisorError(Exception):
    """A failure with a message that can be shown to the user as is."""


# ---------------------------------------------------------------- setup

def load_env(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from `path` (default: .env) into os.environ.

    Variables already in the environment win.

    Minimal on purpose — no extra dependency: blank lines and # comments are
    skipped, `export ` prefixes and matching quotes are stripped.
    """
    try:
        text = (path or ENV_PATH).read_text(encoding="utf-8-sig")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def is_configured() -> bool:
    load_env()
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def model_name() -> str:
    load_env()
    return os.environ.get("PULSEDECK_CLAUDE_MODEL", "").strip() or DEFAULT_MODEL


# ---------------------------------------------------------------- snapshot

def build_snapshot(snap, groups, events, *, gpu_temp: float | None = None,
                   top_n: int = 8) -> dict:
    """The data sent to Claude. Names and numbers only.

    `snap` is a monitor.Snapshot, `groups` the (name, cpu %, rss, count)
    tuples from live_chart._group_by_name, `events` today's spike events as
    parsed by live_chart._read_today_spikes.
    """
    gib = 1024 ** 3

    def _round(v, nd=1):
        return None if v is None else round(float(v), nd)

    by_cpu = sorted(groups, key=lambda g: g[1], reverse=True)[:top_n]
    by_ram = sorted(groups, key=lambda g: g[2], reverse=True)[:top_n]
    return {
        "now": {
            "cpu_percent": _round(snap.cpu_percent),
            "cpu_logical_cores": snap.cpu_logical,
            "ram_percent": _round(snap.ram_percent),
            "ram_used_gb": _round(snap.ram_used / gib),
            "ram_total_gb": _round(snap.ram_total / gib),
            "disk_percent": _round(snap.disk_percent),
            "swap_percent": _round(snap.swap_percent),
            "cpu_temp_c": _round(snap.temp_celsius, 0),
            "gpu_temp_c": _round(gpu_temp, 0),
            "on_battery": None if snap.battery_plugged is None else not snap.battery_plugged,
        },
        "top_cpu": [{"name": n, "cpu_percent_of_machine": round(c, 1), "count": k}
                    for n, c, _r, k in by_cpu if c >= 0.1],
        "top_memory": [{"name": n, "memory_mb": round(r / 1024 ** 2), "count": k}
                       for n, _c, r, k in by_ram],
        "load_events_today": [
            {"time": e.get("time"), "reason": e.get("reason"),
             "suspects": [name for name, _size in e.get("suspects", [])[:3]]}
            for e in events[-10:]
        ],
    }


# ---------------------------------------------------------------- request

def ask_claude(snapshot: dict, question: str | None = None, *, language: str = "en",
               client=None) -> str:
    """Send the snapshot (and an optional question); return Claude's answer.

    Blocking — call it from a worker thread, never the Tk thread. Raises
    AdvisorError with a user-facing message on any failure.
    """
    if not is_configured():
        raise AdvisorError(tr(
            "עדיין אין מפתח API. צור קובץ .env ליד monitor.py עם השורה\n"
            "ANTHROPIC_API_KEY=sk-ant-...  (ראה .env.example)",
            "No API key yet. Create a .env file next to monitor.py with the line\n"
            "ANTHROPIC_API_KEY=sk-ant-...  (see .env.example)"))
    try:
        import anthropic
    except ImportError as e:
        raise AdvisorError(tr("חסרה החבילה anthropic: pip install anthropic",
                                "The 'anthropic' package is missing: pip install anthropic")) from e

    lang_name = "Hebrew" if language == "he" else "English"
    user_text = "Snapshot:\n" + json.dumps(snapshot, ensure_ascii=False, indent=1)
    user_text += "\n\nQuestion: " + (question.strip() if question and question.strip()
                                    else "What is going on with my computer right now?")
    try:
        client = client or anthropic.Anthropic()
        # Server-side fallback: if a safety classifier declines, the API
        # re-runs the request on Anthropic's recommended model instead of
        # returning a refusal.
        response = client.beta.messages.create(
            model=model_name(),
            max_tokens=_MAX_TOKENS,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=_SYSTEM_PROMPT.format(language=lang_name),
            messages=[{"role": "user", "content": user_text}],
        )
    except anthropic.AuthenticationError as e:
        raise AdvisorError(tr("המפתח נדחה. בדוק את ANTHROPIC_API_KEY בקובץ .env.",
                                "The API key was rejected. Check ANTHROPIC_API_KEY in .env.")) from e
    except anthropic.PermissionDeniedError as e:
        raise AdvisorError(tr("למפתח הזה אין הרשאה למודל.",
                                "This API key is not allowed to use the model.")) from e
    except anthropic.NotFoundError as e:
        raise AdvisorError(tr(f"המודל {model_name()} לא נמצא. בדוק את PULSEDECK_CLAUDE_MODEL.",
                                f"Model not found: {model_name()}. Check PULSEDECK_CLAUDE_MODEL.")) from e
    except anthropic.RateLimitError as e:
        raise AdvisorError(tr("יותר מדי בקשות כרגע — נסה שוב בעוד דקה.",
                                "Too many requests right now — try again in a minute.")) from e
    except anthropic.APIConnectionError as e:
        raise AdvisorError(tr("אין חיבור ל-Anthropic. בדוק את האינטרנט.",
                                "Could not reach Anthropic. Check the internet connection.")) from e
    except anthropic.APIStatusError as e:
        log.warning("Claude API error %s (request %s)", e.status_code,
                    getattr(e, "request_id", None))
        raise AdvisorError(tr(f"Claude החזיר שגיאה ({e.status_code}). נסה שוב מאוחר יותר.",
                                f"Claude returned an error ({e.status_code}). Try again later.")) from e

    if response.stop_reason == "refusal":
        raise AdvisorError(tr("Claude סירב לענות על זה.", "Claude declined to answer this one."))
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise AdvisorError(tr("Claude החזיר תשובה ריקה. נסה שוב.", "Claude returned an empty answer. Try again."))
    if response.stop_reason == "max_tokens":
        text += "\n…"
    return text
