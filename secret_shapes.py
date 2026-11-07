"""What a secret looks like when it is passed bare, with no flag name in front
of it: the issuer's prefix plus enough random characters.

Shared by the on-screen / log redaction (ghost_sweeper.redact_cmdline) and the
pre-push scan (secret_scan.py). Standard library only, so the git hook runs
without PulseDeck's dependencies.
"""
from __future__ import annotations

import re

_TAIL = r"(?![A-Za-z0-9_\-])"

NAMED_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern)) for name, pattern in (
        ("Anthropic/OpenAI key", r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}"),
        ("GitHub token", r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}" + _TAIL),
        ("GitHub fine-grained token", r"\bgithub_pat_[A-Za-z0-9_]{30,}" + _TAIL),
        ("Slack token", r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
        ("AWS access key id", r"\bAKIA[0-9A-Z]{16}\b"),
        ("Google API key", r"\bAIza[0-9A-Za-z_\-]{35}" + _TAIL),
        ("JWT", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
        ("Telegram bot token", r"(?<![0-9])[0-9]{8,10}:[A-Za-z0-9_\-]{35}" + _TAIL),
        ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ))

SHAPES: tuple[re.Pattern[str], ...] = tuple(p for _name, p in NAMED_SHAPES)
