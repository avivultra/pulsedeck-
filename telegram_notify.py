"""One-way Telegram messages: PulseDeck → the owner's own chat with his bot.

Adapted from the feature library's telegram-bot-notifier (stdlib only). What
changed: the token is checked before it goes into a URL, no error message or
log line ever carries it, and messages go out as plain text, so nothing in
them is read as markup.

Setup, by the user: @BotFather → /newbot → paste the token in PulseDeck →
send /start to the new bot → "Connect". find_chat() then reads who wrote to
the bot; the user confirms the name before anything is saved.

Every call is blocking network I/O: worker threads only, never the Tk thread.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from i18n import tr

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT_SEC = 8
MAX_TEXT = 4000                 # Telegram's limit is 4096 characters
_TOKEN = re.compile(r"\d{5,12}:[A-Za-z0-9_-]{30,64}")


class TelegramError(Exception):
    """A failure with a message that can be shown to the user as is."""


def looks_like_token(token: str) -> bool:
    return bool(_TOKEN.fullmatch(token or ""))


def _call(token: str, method: str, payload: dict | None = None) -> dict:
    """One Bot API call; its "result", or TelegramError."""
    if not looks_like_token(token):
        raise TelegramError(tr("זה לא נראה כמו טוקן של בוט. הוא נראה כך: 123456789:AAH…",
                               "That does not look like a bot token. It looks like 123456789:AAH…"))
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(API.format(token=token, method=method), data=data, method="POST" if data else "GET",
                  headers={"Content-Type": "application/json", "User-Agent": "PulseDeck"})
    try:
        with urlopen(req, timeout=TIMEOUT_SEC) as resp:
            body = json.loads(resp.read())
    except HTTPError as e:
        try:
            description = str(json.loads(e.read()).get("description", ""))
        except Exception:
            description = ""
        log.warning("Telegram %s failed: HTTP %s %s", method, e.code, description.replace(token, "***"))
        if e.code in (401, 404):
            raise TelegramError(tr("טלגרם לא מכיר את הטוקן הזה. העתק אותו שוב מ-@BotFather.",
                                   "Telegram does not know this token. Copy it again from @BotFather.")) from None
        if e.code == 403:
            raise TelegramError(tr("הבוט לא יכול לכתוב לך עדיין. שלח לו /start בטלגרם ונסה שוב.",
                                   "The bot cannot write to you yet. Send it /start in Telegram and try again.")) from None
        raise TelegramError(tr(f"טלגרם החזיר שגיאה ({e.code}). נסה שוב מאוחר יותר.",
                               f"Telegram returned an error ({e.code}). Try again later.")) from None
    except (URLError, OSError, ValueError) as e:
        log.warning("Telegram %s failed: %s", method, str(e).replace(token, "***"))
        raise TelegramError(tr("אין חיבור לטלגרם. בדוק את האינטרנט.",
                               "Could not reach Telegram. Check the internet connection.")) from None
    if not body.get("ok"):
        log.warning("Telegram %s refused: %s", method, str(body.get("description", "")).replace(token, "***"))
        raise TelegramError(tr("טלגרם סירב לבקשה.", "Telegram refused the request."))
    return body.get("result")


def bot_name(token: str) -> str:
    """The bot's @username: proves the token works."""
    me = _call(token, "getMe") or {}
    return "@" + str(me.get("username", "")) if me.get("username") else str(me.get("first_name", "bot"))


def find_chat(token: str) -> tuple[int, str] | None:
    """(chat id, display name) of the latest private chat that wrote to the
    bot, or None when nobody has yet. Groups and channels are ignored."""
    updates = _call(token, "getUpdates", {"limit": 100, "allowed_updates": ["message"]}) or []
    for update in reversed(updates):
        chat = (update.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private" and isinstance(chat.get("id"), int):
            name = " ".join(str(chat[k]) for k in ("first_name", "last_name") if chat.get(k))
            if chat.get("username"):
                name = f"{name} (@{chat['username']})" if name else "@" + str(chat["username"])
            return chat["id"], name or str(chat["id"])
    return None


def send(token: str, chat_id: int | str, text: str) -> None:
    """Send `text` as plain text; raises TelegramError."""
    _call(token, "sendMessage", {"chat_id": chat_id, "text": text[:MAX_TEXT],
                                 "disable_web_page_preview": True})
