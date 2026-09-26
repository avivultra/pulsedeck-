"""Two-language UI (Hebrew / English) without a translation catalogue.

Every user-facing string is written as `tr("עברית", "English")`. The Hebrew
text stays exactly where it always was, so a Hebrew-speaking user sees a
byte-for-byte identical UI; the English sits right next to it, so the two can
never drift apart the way a separate catalogue file does.

Language resolution (once, at first use):
  1. config.json → "ui": {"language": "he" | "en" | "auto"}   (default "auto")
  2. "auto" → the Windows display language (GetUserDefaultUILanguage),
     falling back to the POSIX locale elsewhere
  3. Hebrew if that language is Hebrew, English for everything else

Layout follows the language: Hebrew is right-to-left, so text anchors to the
east edge and rows are packed from the right. The `ANCHOR()`, `JUSTIFY()` and
`SIDE()` helpers return the right value for the active language — in Hebrew
they return exactly what the code used before this module existed.
"""

from __future__ import annotations

import locale
import logging
import os

log = logging.getLogger(__name__)

SUPPORTED = ("he", "en")
_HEBREW_LANG_ID = 0x0D          # PRIMARYLANGID of any Hebrew LANGID (0x040D)

_lang: str | None = None


def _system_language() -> str:
    """'he' or 'en' from the OS display language."""
    if os.name == "nt":
        try:
            import ctypes
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "he" if (lang_id & 0x3FF) == _HEBREW_LANG_ID else "en"
        except Exception:
            log.debug("GetUserDefaultUILanguage failed", exc_info=True)
    for value in (os.environ.get("LANG", ""), (locale.getlocale()[0] or "")):
        low = value.lower()
        if low.startswith(("he", "iw")) or "hebrew" in low:
            return "he"
    return "en"


def _resolve() -> str:
    configured = "auto"
    try:
        import config as app_config
        configured = str(app_config.load_config().get("ui", {}).get("language", "auto"))
    except Exception:
        log.debug("Could not read ui.language from config", exc_info=True)
    configured = configured.strip().lower()
    if configured in SUPPORTED:
        return configured
    return _system_language()


def language() -> str:
    global _lang
    if _lang is None:
        _lang = _resolve()
    return _lang


def set_language(lang: str | None) -> None:
    """Force a language ('he' / 'en'), or None to re-resolve. Takes effect for
    strings built after the call; open windows keep their text until reopened."""
    global _lang
    _lang = lang if lang in SUPPORTED else None


def is_rtl() -> bool:
    return language() == "he"


def tr(he: str, en: str) -> str:
    """The string for the active language."""
    return he if is_rtl() else en


# ---- layout helpers (Tk option values) ----

def ANCHOR() -> str:
    """Anchor for text at the reading-start edge."""
    return "e" if is_rtl() else "w"


def JUSTIFY() -> str:
    return "right" if is_rtl() else "left"


def SIDE() -> str:
    """pack(side=...) for the element that should come FIRST in reading order."""
    return "right" if is_rtl() else "left"


def SIDE_END() -> str:
    """pack(side=...) for the element that should come LAST in reading order."""
    return "left" if is_rtl() else "right"
