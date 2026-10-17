"""What "Ask Claude" has cost this month, and the monthly cap ($5 by default).

An estimate made on this machine: each answer's tokens times Anthropic's list
price. The exact figure is in the Console (platform.claude.com → Usage), and
so is the hard cap: a workspace with a monthly spend limit, which Anthropic
itself enforces. This one stops PulseDeck *before* it sends a request that
could pass the cap, so the Console limit should never be the one that trips.

Each request is charged its worst case before it is sent — every input byte
counted as a token, the longest answer, the dearest model — and the answer's
real usage then replaces that. A request that never reached Anthropic gets
its charge back; one whose fate is unknown (a timeout, a dropped connection)
keeps it. A crash mid-request leaves the worst case counted: every error is
on the side of spending less.

history/claude_spend.json: {"days": {"2026-10-17": {"usd": 0.031, "requests": 1}}}
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from app_paths import HISTORY_DIR
from i18n import tr

log = logging.getLogger(__name__)

SPEND_PATH = HISTORY_DIR / "claude_spend.json"
DEFAULT_MONTHLY_USD = 5.0
KEEP_DAYS = 100                 # enough for this month, last month and a week back

# USD per million tokens (input, output) — platform.claude.com pricing, October 2026.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-mythos-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),   # prompts up to 100K tokens; ours stay under 10K
}
DEAREST = (10.0, 50.0)          # any model not listed above is priced at this
CACHE_WRITE = 2.0               # x the input price (the 1-hour write; 5-minute is 1.25)
CACHE_READ = 0.1
# Room for what the API adds around our text: message framing and the
# tool-use system prompt.
OVERHEAD_TOKENS = 1000

HEBREW_MONTHS = ("ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט",
                 "ספטמבר", "אוקטובר", "נובמבר", "דצמבר")
ENGLISH_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
                  "September", "October", "November", "December")


def month_name(month: int) -> str:
    return tr(HEBREW_MONTHS[month - 1], ENGLISH_MONTHS[month - 1])


def price(model: str | None) -> tuple[float, float]:
    """(input, output) USD per million tokens. A dated id such as
    claude-opus-5-5-20261001 is priced as its family; anything else unknown
    at the dearest price."""
    model = (model or "").strip().lower()
    for name, p in PRICES.items():
        if model == name or re.fullmatch(re.escape(name) + r"-\d{8}", model):
            return p
    return DEAREST


def cost_usd(model: str | None, usage) -> float:
    """What one answer cost, from the API's usage block."""
    p_in, p_out = price(model)

    def n(field: str) -> int:
        return max(0, int(getattr(usage, field, 0) or 0))

    tokens_in = (n("input_tokens") + CACHE_WRITE * n("cache_creation_input_tokens")
                 + CACHE_READ * n("cache_read_input_tokens"))
    return (tokens_in * p_in + n("output_tokens") * p_out) / 1_000_000


def worst_case_usd(prompt: str, max_output_tokens: int) -> float:
    """An upper bound on one request: a token is never shorter than a byte,
    and the dearest model answers at full length."""
    tokens_in = len(prompt.encode("utf-8")) + OVERHEAD_TOKENS
    return (tokens_in * DEAREST[0] + max_output_tokens * DEAREST[1]) / 1_000_000


def configured_cap() -> float:
    """config.json → claude.monthly_usd (default $5)."""
    try:
        import config
        value = float((config.load_config().get("claude") or {}).get("monthly_usd", DEFAULT_MONTHLY_USD))
    except (TypeError, ValueError):
        return DEFAULT_MONTHLY_USD
    except Exception:
        log.exception("Could not read the monthly cap; using $%s", DEFAULT_MONTHLY_USD)
        return DEFAULT_MONTHLY_USD
    return max(0.0, min(value, 1000.0))


def usd(amount: float) -> str:
    """$0.03, $4.85, $5 — cents always, unless it is a whole number of dollars."""
    return f"${amount:.0f}" if abs(amount - round(amount)) < 0.005 and amount >= 1 else f"${amount:.2f}"


class CapReached(Exception):
    """The next request could pass the monthly cap. The message is for the user."""


@dataclass
class Hold:
    """A request's worst-case charge, waiting for its real cost."""
    day: str
    usd: float
    done: bool = False


class Ledger:
    def __init__(self, path: Path | None = None, cap: Callable[[], float] = configured_cap,
                 now: Callable[[], float] = time.time) -> None:
        self.path = Path(path or SPEND_PATH)
        self.cap = cap
        self.now = now
        self._lock = threading.Lock()
        # Called with "spent" after a request's cost is known (or kept), and
        # with "cap" when a request is refused. Off the lock; errors are logged.
        self.listeners: list[Callable[[str], None]] = []

    # ------------------------------------------------------------ file

    def today(self) -> date:
        return date.fromtimestamp(self.now())

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        days = data.get("days")
        return {"days": days if isinstance(days, dict) else {}}

    def _save(self, data: dict) -> None:
        oldest = (self.today() - timedelta(days=KEEP_DAYS)).isoformat()
        data["days"] = {d: v for d, v in data["days"].items() if d >= oldest}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not save Claude spending to %s", self.path)

    @staticmethod
    def _add(data: dict, day: str, amount: float, requests: int = 0) -> None:
        entry = data["days"].setdefault(day, {"usd": 0.0, "requests": 0})
        entry["usd"] = round(max(0.0, float(entry.get("usd", 0.0)) + amount), 6)
        entry["requests"] = max(0, int(entry.get("requests", 0)) + requests)

    def _tell(self, event: str) -> None:
        for fn in list(self.listeners):
            try:
                fn(event)
            except Exception:
                log.exception("Spending listener failed")

    # ------------------------------------------------------------ requests

    def reserve(self, worst: float) -> Hold:
        """Charge `worst` now, or raise CapReached when it does not fit."""
        cap = self.cap()
        with self._lock:
            data = self._load()
            day = self.today()
            spent = self._month(data, day.isoformat()[:7])
            if spent + worst > cap:
                refused = True
            else:
                refused = False
                self._add(data, day.isoformat(), worst, requests=1)
                self._save(data)
        if refused:
            self._tell("cap")
            first = (day.replace(day=1) + timedelta(days=32)).replace(day=1)
            raise CapReached(tr(
                f"הגעת לתקרה החודשית של Claude: {usd(spent)} מתוך {usd(cap)}. "
                f"שאלה נוספת עלולה לעבור אותה, לכן היא לא נשלחה. "
                f"התקרה מתאפסת ב-1 ב{month_name(first.month)}.",
                f"Claude's monthly cap is reached: {usd(spent)} of {usd(cap)}. "
                f"One more question could pass it, so it was not sent. "
                f"It resets on {month_name(first.month)} 1."))
        return Hold(day.isoformat(), worst)

    def settle(self, hold: Hold, actual: float) -> None:
        """The answer came back: its real cost replaces the worst case."""
        if hold.done:
            return
        hold.done = True
        with self._lock:
            data = self._load()
            self._add(data, hold.day, max(0.0, actual) - hold.usd)
            self._save(data)
        self._tell("spent")

    def release(self, hold: Hold) -> None:
        """The request never reached Anthropic (or was refused there): no charge."""
        if hold.done:
            return
        hold.done = True
        with self._lock:
            data = self._load()
            self._add(data, hold.day, -hold.usd, requests=-1)
            self._save(data)

    def keep(self, hold: Hold) -> None:
        """Unknown whether Anthropic processed it: the worst case stays."""
        if hold.done:
            return
        hold.done = True
        self._tell("spent")

    # ------------------------------------------------------------ reading

    @staticmethod
    def _month(data: dict, month: str) -> float:
        return sum(float(v.get("usd", 0.0)) for d, v in data["days"].items() if d.startswith(month))

    def month_usd(self, month: str | None = None) -> float:
        """Spent in `month` ("2026-10"; default: this month)."""
        with self._lock:
            data = self._load()
        return self._month(data, month or self.today().isoformat()[:7])

    def totals(self, first: date, last: date) -> tuple[int, float]:
        """(questions, USD) from `first` to `last`, both included."""
        lo, hi = first.isoformat(), last.isoformat()
        with self._lock:
            data = self._load()
        picked = [v for d, v in data["days"].items() if lo <= d <= hi]
        return (sum(int(v.get("requests", 0)) for v in picked),
                sum(float(v.get("usd", 0.0)) for v in picked))

    def summary(self) -> str:
        return tr(f"החודש: {usd(self.month_usd())} מתוך {usd(self.cap())} (הערכה)",
                  f"This month: {usd(self.month_usd())} of {usd(self.cap())} (estimate)")


_ledger: Ledger | None = None
_ledger_lock = threading.Lock()


def default_ledger() -> Ledger:
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            _ledger = Ledger()
        return _ledger
