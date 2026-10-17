"""Ask Claude what is going on with the computer.

Runs only when the user presses the button — nothing here touches the network
or costs anything in the background. What is sent is a small, readable
snapshot: current CPU / memory / disk / temperature, the heaviest programs
*by name* (same-name processes folded), and today's load events. Never command
lines, paths, window titles or user names.

Setup: put the key in a `.env` file next to monitor.py (see .env.example;
PulseDeck.exe: in its data folder, see app_paths):

    ANTHROPIC_API_KEY=sk-ant-...
    # optional
    PULSEDECK_CLAUDE_MODEL=claude-opus-5-5

Variables already set in the environment win over the file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date
from pathlib import Path

from app_paths import DATA_DIR, HISTORY_DIR, is_frozen
from i18n import tr

log = logging.getLogger(__name__)

ENV_PATH = DATA_DIR / ".env"
DEFAULT_MODEL = "claude-opus-5-5"
# ---- Spending limits. One button press = at most one request; these make
# sure nothing (a stuck button, a bug, a loop) can burn tokens without end.
_MAX_TOKENS = 2000              # per answer, thinking included (answers are short)
TIMEOUT_SEC = 60                # a request that hangs is abandoned, not retried
MIN_GAP_SEC = 5                 # at least this long between two requests
MAX_QUESTION_CHARS = 500
MAX_SNAPSHOT_CHARS = 8000
DEFAULT_DAILY_REQUESTS = 20     # override: PULSEDECK_CLAUDE_DAILY_REQUESTS
DEFAULT_DAILY_TOKENS = 60_000   # override: PULSEDECK_CLAUDE_DAILY_TOKENS
USAGE_PATH = HISTORY_DIR / "claude_usage.json"

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
If the snapshot is not enough to know, say so instead of guessing.

"load_last_minute" lists the programs that used the computer most over the \
last minute, with what PulseDeck can do about each ("can"): "lower" lowers \
its priority (it keeps working and gives way; reversible), "restore" undoes \
that, "close" closes it normally, like clicking X. Programs with \
"never_touched" are off limits. When acting on a program would clearly help, \
write your answer first, then call suggest_actions once, with at most three \
suggestions, safest first (prefer "lower" over "close"). Each one is one \
program and one action from its "can". Suggest nothing when all is fine. \
The user confirms every action; nothing runs on its own."""

# Claude may propose actions; the window turns each into a button the user
# confirms. Only names and actions from the snapshot's "can" lists survive.
SUGGEST_TOOL = {
    "name": "suggest_actions",
    "description": "Offer the user up to three one-click actions on programs from load_last_minute. "
                   "Each runs only after the user confirms it.",
    "input_schema": {
        "type": "object",
        "properties": {"actions": {"type": "array", "maxItems": 3, "items": {
            "type": "object",
            "properties": {"program": {"type": "string"},
                           "action": {"type": "string", "enum": ["lower", "restore", "close"]},
                           "why": {"type": "string", "description": "One short sentence, in the answer's language."}},
            "required": ["program", "action", "why"]}}},
        "required": ["actions"],
    },
}
MAX_SUGGESTIONS = 3


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


def missing_key_text() -> str:
    """How to add the key. From source, .env.example sits beside monitor.py;
    PulseDeck.exe has no such file, so name the exact folder instead."""
    if is_frozen():
        folder = ENV_PATH.parent
        return tr(
            "עדיין אין מפתח API. צור קובץ בשם .env בתיקייה הזאת:\n"
            f"{folder}\n"
            "וכתוב בו שורה אחת: ANTHROPIC_API_KEY= ואחריה המפתח שלך.",
            "No API key yet. Create a file named .env in this folder:\n"
            f"{folder}\n"
            "with one line: ANTHROPIC_API_KEY= followed by your key.")
    return tr(
        "עדיין אין מפתח API. צור קובץ .env ליד monitor.py עם השורה\n"
        "ANTHROPIC_API_KEY=sk-ant-...  (ראה .env.example)",
        "No API key yet. Create a .env file next to monitor.py with the line\n"
        "ANTHROPIC_API_KEY=sk-ant-...  (see .env.example)")


def model_name() -> str:
    load_env()
    return os.environ.get("PULSEDECK_CLAUDE_MODEL", "").strip() or DEFAULT_MODEL


# ---------------------------------------------------------------- budget

class Budget:
    """Daily caps on requests and tokens, kept in history/claude_usage.json.

    A request is counted *before* it is sent, so even requests that fail or
    hang use up the allowance: a bug that retries in a loop stops at the cap.
    """

    def __init__(self, path: Path | None = None, now=time.time) -> None:
        self.path = Path(path or USAGE_PATH)
        self.now = now
        self._lock = threading.Lock()

    @staticmethod
    def _limit(name: str, default: int) -> int:
        load_env()
        try:
            return max(0, int(os.environ.get(name, default)))
        except ValueError:
            return default

    @property
    def max_requests(self) -> int:
        return self._limit("PULSEDECK_CLAUDE_DAILY_REQUESTS", DEFAULT_DAILY_REQUESTS)

    @property
    def max_tokens(self) -> int:
        return self._limit("PULSEDECK_CLAUDE_DAILY_TOKENS", DEFAULT_DAILY_TOKENS)

    def _load(self) -> dict:
        today = date.fromtimestamp(self.now()).isoformat()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if data.get("day") != today:
            data = {"day": today, "requests": 0, "tokens": 0, "last": data.get("last", 0.0)}
        return data

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            log.exception("Could not save Claude usage to %s", self.path)

    def reserve(self) -> None:
        """Check every limit and count one request, or raise AdvisorError."""
        with self._lock:
            data = self._load()
            wait = MIN_GAP_SEC - (self.now() - float(data.get("last", 0.0)))
            if wait > 0:
                raise AdvisorError(tr(f"רגע — אפשר לשאול שוב בעוד {wait:.0f} שניות.",
                                      f"One moment — you can ask again in {wait:.0f} s."))
            if data["requests"] >= self.max_requests:
                raise AdvisorError(tr(
                    f"הגעת לתקרה היומית של {self.max_requests} שאלות. היא מתאפסת בחצות "
                    f"(אפשר לשנות ב-.env: PULSEDECK_CLAUDE_DAILY_REQUESTS).",
                    f"Daily limit of {self.max_requests} questions reached. It resets at midnight "
                    f"(change it in .env: PULSEDECK_CLAUDE_DAILY_REQUESTS)."))
            if data["tokens"] >= self.max_tokens:
                raise AdvisorError(tr(
                    f"הגעת לתקרה היומית של {self.max_tokens:,} טוקנים. היא מתאפסת בחצות "
                    f"(אפשר לשנות ב-.env: PULSEDECK_CLAUDE_DAILY_TOKENS).",
                    f"Daily limit of {self.max_tokens:,} tokens reached. It resets at midnight "
                    f"(change it in .env: PULSEDECK_CLAUDE_DAILY_TOKENS)."))
            data["requests"] += 1
            data["last"] = self.now()
            self._save(data)

    def add_tokens(self, n: int) -> None:
        with self._lock:
            data = self._load()
            data["tokens"] += max(0, int(n))
            self._save(data)

    def summary(self) -> str:
        with self._lock:
            data = self._load()
        return tr(f"היום: {data['requests']}/{self.max_requests} שאלות · "
                  f"{data['tokens'] / 1000:.1f}K/{self.max_tokens // 1000}K טוקנים",
                  f"Today: {data['requests']}/{self.max_requests} questions · "
                  f"{data['tokens'] / 1000:.1f}K/{self.max_tokens // 1000}K tokens")


_budget: Budget | None = None


def default_budget() -> Budget:
    global _budget
    if _budget is None:
        _budget = Budget()
    return _budget


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

def _allowed_actions(snapshot: dict) -> dict[str, tuple[str, list[str]]]:
    """lower-case name -> (name, its "can" list), from the snapshot."""
    out = {}
    for item in snapshot.get("load_last_minute") or []:
        if item.get("can"):
            out[str(item["name"]).lower()] = (str(item["name"]), list(item["can"]))
    return out


def _suggestions(content, allowed: dict) -> list[dict]:
    """The suggest_actions call, kept only where it names a program and an
    action the snapshot allows: the model cannot widen what the user sees."""
    out: list[dict] = []
    for block in content:
        if getattr(block, "type", None) != "tool_use" or getattr(block, "name", None) != SUGGEST_TOOL["name"]:
            continue
        for a in (getattr(block, "input", None) or {}).get("actions") or []:
            if not isinstance(a, dict):
                continue
            entry = allowed.get(str(a.get("program", "")).strip().lower())
            action = a.get("action")
            if entry is None or action not in entry[1]:
                continue
            if any(s["program"] == entry[0] for s in out):
                continue
            out.append({"program": entry[0], "action": action, "why": str(a.get("why") or "").strip()[:200]})
    return out[:MAX_SUGGESTIONS]


def ask_claude(snapshot: dict, question: str | None = None, *, language: str = "en",
               client=None, budget: Budget | None = None, actions_out: list | None = None) -> str:
    """Send the snapshot (and an optional question); return Claude's answer.

    When the snapshot has programs PulseDeck may act on and `actions_out` is
    given, Claude may propose actions: the allowed ones are appended to it as
    {"program", "action", "why"}. Nothing is run here.

    Blocking — call it from a worker thread, never the Tk thread. Raises
    AdvisorError with a user-facing message on any failure.
    """
    if not is_configured():
        raise AdvisorError(missing_key_text())
    try:
        import anthropic
    except ImportError as e:
        raise AdvisorError(tr("חסרה החבילה anthropic: pip install anthropic",
                                "The 'anthropic' package is missing: pip install anthropic")) from e

    lang_name = "Hebrew" if language == "he" else "English"
    snapshot_text = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))[:MAX_SNAPSHOT_CHARS]
    question = (question or "").strip()[:MAX_QUESTION_CHARS]
    user_text = "Snapshot:\n" + snapshot_text
    user_text += "\n\nQuestion: " + (question or "What is going on with my computer right now?")
    budget = budget or default_budget()
    allowed = _allowed_actions(snapshot) if actions_out is not None else {}
    extra = {"tools": [SUGGEST_TOOL]} if allowed else {}
    budget.reserve()            # raises when a limit is reached; counts the request first
    try:
        # No automatic retries and a hard timeout: one press, one attempt.
        client = client or anthropic.Anthropic(timeout=TIMEOUT_SEC, max_retries=0)
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
            **extra,
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
    except anthropic.APITimeoutError as e:
        raise AdvisorError(tr(f"Claude לא ענה תוך {TIMEOUT_SEC} שניות, והבקשה בוטלה.",
                                f"Claude did not answer within {TIMEOUT_SEC} s; the request was dropped.")) from e
    except anthropic.APIConnectionError as e:
        raise AdvisorError(tr("אין חיבור ל-Anthropic. בדוק את האינטרנט.",
                                "Could not reach Anthropic. Check the internet connection.")) from e
    except anthropic.APIStatusError as e:
        log.warning("Claude API error %s (request %s)", e.status_code,
                    getattr(e, "request_id", None))
        raise AdvisorError(tr(f"Claude החזיר שגיאה ({e.status_code}). נסה שוב מאוחר יותר.",
                                f"Claude returned an error ({e.status_code}). Try again later.")) from e

    usage = getattr(response, "usage", None)
    if usage is not None:
        budget.add_tokens((getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0))
    if response.stop_reason == "refusal":
        raise AdvisorError(tr("Claude סירב לענות על זה.", "Claude declined to answer this one."))
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    suggested = _suggestions(response.content, allowed) if allowed else []
    if actions_out is not None:
        actions_out.extend(suggested)
    if not text and suggested:
        text = tr("Claude מציע:", "Claude suggests:")
    if not text:
        raise AdvisorError(tr("Claude החזיר תשובה ריקה. נסה שוב.", "Claude returned an empty answer. Try again."))
    if response.stop_reason == "max_tokens":
        text += "\n…"
    return text
