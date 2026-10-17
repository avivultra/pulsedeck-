"""Secrets — the Anthropic API key and the Telegram bot token — kept in
Windows Credential Manager instead of a file.

Windows encrypts them with the user's own login (DPAPI): another account on
the machine, a copy of PulseDeck's folder or a backup of it cannot read them.
They show in Control Panel → Credential Manager → Windows Credentials as
"PulseDeck/...", where they can be deleted too.

Nothing here logs a value, and errors never carry one. Elsewhere (Linux,
macOS) there is no store: load() returns None, save() raises
SecretStoreError, and the .env file stays the way in.

Tests swap `_backend` for a MemoryStore (conftest.py does it for every
test), so no test ever reads or writes the real store.
"""

from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger(__name__)

ANTHROPIC_KEY = "PulseDeck/anthropic_api_key"
TELEGRAM_TOKEN = "PulseDeck/telegram_bot_token"

MAX_CHARS = 1000            # CRED_MAX_CREDENTIAL_BLOB_SIZE is 2560 bytes (UTF-16)


class SecretStoreError(Exception):
    """A failure with a message that can be shown to the user as is."""


class MemoryStore:
    """In-memory stand-in: tests, and machines without a store."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def read(self, name: str) -> str | None:
        return self.items.get(name)

    def write(self, name: str, value: str) -> None:
        self.items[name] = value

    def delete(self, name: str) -> bool:
        return self.items.pop(name, None) is not None


class _WindowsStore:
    """CredReadW / CredWriteW / CredDeleteW through ctypes: no extra package."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2      # survives logoff; never roams to other PCs
    ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self._ct = ctypes
        self._CREDENTIAL = CREDENTIAL
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self._read = advapi.CredReadW
        self._read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self._read.restype = wintypes.BOOL
        self._write = advapi.CredWriteW
        self._write.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        self._write.restype = wintypes.BOOL
        self._delete = advapi.CredDeleteW
        self._delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._delete.restype = wintypes.BOOL
        self._free = advapi.CredFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = None

    def read(self, name: str) -> str | None:
        ct = self._ct
        out = ct.POINTER(self._CREDENTIAL)()
        if not self._read(name, self.CRED_TYPE_GENERIC, 0, ct.byref(out)):
            err = ct.get_last_error()
            if err != self.ERROR_NOT_FOUND:
                log.warning("Credential Manager read failed (error %s)", err)
            return None
        try:
            cred = out.contents
            blob = ct.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        finally:
            self._free(out)
        return blob.decode("utf-16-le")

    def write(self, name: str, value: str) -> None:
        ct = self._ct
        blob = value.encode("utf-16-le")
        buf = (ct.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = self._CREDENTIAL()
        cred.Type = self.CRED_TYPE_GENERIC
        cred.TargetName = name
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ct.cast(buf, ct.POINTER(ct.c_ubyte))
        cred.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = "PulseDeck"
        try:
            if not self._write(ct.byref(cred), 0):
                raise OSError(ct.get_last_error(), "CredWriteW failed")
        finally:
            ct.memset(buf, 0, len(blob))    # our copy; Windows keeps its encrypted one

    def delete(self, name: str) -> bool:
        ct = self._ct
        if self._delete(name, self.CRED_TYPE_GENERIC, 0):
            return True
        err = ct.get_last_error()
        if err != self.ERROR_NOT_FOUND:
            raise OSError(err, "CredDeleteW failed")
        return False


def _default_backend():
    if os.name == "nt":
        try:
            return _WindowsStore()
        except (OSError, AttributeError):
            log.exception("Windows Credential Manager is not reachable")
    return None


_backend = _default_backend()
_lock = threading.Lock()


def available() -> bool:
    """True when secrets can be stored here (Windows)."""
    return _backend is not None


def clean(value: str) -> str:
    """The pasted value, trimmed; raises SecretStoreError when it cannot be
    a key or a token (empty, spaces inside, or far too long)."""
    from i18n import tr
    value = (value or "").strip()
    if not value:
        raise SecretStoreError(tr("השדה ריק.", "The field is empty."))
    if any(ch.isspace() for ch in value):
        raise SecretStoreError(tr("יש רווח באמצע — כנראה הודבק משהו נוסף. העתק שוב רק את המפתח.",
                                  "There is a space inside it — something extra was pasted. "
                                  "Copy just the key again."))
    if len(value) > MAX_CHARS:
        raise SecretStoreError(tr("ארוך מדי בשביל מפתח.", "Too long to be a key."))
    return value


def load(name: str) -> str | None:
    """The stored secret, or None (none stored, or no store here)."""
    if _backend is None:
        return None
    try:
        with _lock:
            return _backend.read(name) or None
    except Exception:
        log.exception("Could not read %s", name)
        return None


def save(name: str, value: str) -> None:
    from i18n import tr
    value = clean(value)
    if _backend is None:
        raise SecretStoreError(tr("אין כאן מאגר מוצפן. השתמש בקובץ .env.",
                                  "No encrypted store here. Use the .env file."))
    try:
        with _lock:
            _backend.write(name, value)
    except Exception as e:
        log.error("Could not save %s (%s)", name, type(e).__name__)
        raise SecretStoreError(tr("Windows לא שמר את המפתח. נסה שוב.",
                                  "Windows did not save it. Try again.")) from None


def delete(name: str) -> bool:
    """Remove the secret; True when there was one."""
    if _backend is None:
        return False
    try:
        with _lock:
            return _backend.delete(name)
    except Exception:
        log.exception("Could not delete %s", name)
        return False


def hint(value: str | None) -> str:
    """The last four characters, as the Console shows keys: "…AbCd"."""
    return f"…{value[-4:]}" if value and len(value) >= 12 else ""
