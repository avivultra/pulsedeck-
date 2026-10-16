"""Every process's name, CPU time, memory and start time in ONE system call
(Windows, 64-bit Python).

Why: psutil reads processes one at a time. For a process the current user
cannot open with full rights (services, the antivirus, other sessions --
about half of a typical Windows PC) psutil falls back to asking Windows for
the *whole* process table and picking one entry out of it, once for CPU times
and once for memory. Measured on the owner's PC: ~160 such processes, so a
sampler tick cost 650-830 ms of CPU and held the GIL long enough to freeze
the Tk windows for up to 1.2 s.

NtQuerySystemInformation(SystemProcessInformation) returns that table once,
with everything the sampler needs; psutil's own fallback uses the same call.
Nothing here opens a process, so access rights do not matter.
"""
from __future__ import annotations

import ctypes
import struct
import sys

SYSTEM_PROCESS_INFORMATION = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_EPOCH_DIFF_100NS = 116444736000000000      # 1601-01-01 -> 1970-01-01

# x64 layout of SYSTEM_PROCESS_INFORMATION (ntexapi.h).
_OFF_CREATE_USER_KERNEL = 32                # 3 x LARGE_INTEGER, 100 ns units
_OFF_NAME_LENGTH = 56                       # UNICODE_STRING.Length, bytes
_OFF_NAME_BUFFER = 64                       # UNICODE_STRING.Buffer
_OFF_PID = 80                               # UniqueProcessId
_OFF_WORKING_SET = 144                      # WorkingSetSize == psutil's rss
_ENTRY_MIN = 152

AVAILABLE = sys.platform == "win32" and struct.calcsize("P") == 8

_SPECIAL_NAMES = {0: "System Idle Process", 4: "System"}   # same as psutil


def parse(buf, size: int, read_name, boot_time: float = 0.0) -> list[tuple[int, str, float, float, int]]:
    """(pid, name, create_time, cpu_seconds, rss) for each entry in `buf`.

    `read_name(address, chars)` reads the UTF-16 image name the entry points
    to (ctypes.wstring_at in production; tests pass a reader over bytes).
    Processes with no start time (Idle, System) get `boot_time` (psutil says
    0.0 there; boot time is when they really started).
    """
    out = []
    off = 0
    while off + _ENTRY_MIN <= size:
        nxt, = struct.unpack_from("<I", buf, off)
        create, user, kernel = struct.unpack_from("<qqq", buf, off + _OFF_CREATE_USER_KERNEL)
        name_len, = struct.unpack_from("<H", buf, off + _OFF_NAME_LENGTH)
        name_ptr, = struct.unpack_from("<Q", buf, off + _OFF_NAME_BUFFER)
        pid, = struct.unpack_from("<Q", buf, off + _OFF_PID)
        rss, = struct.unpack_from("<Q", buf, off + _OFF_WORKING_SET)
        name = read_name(name_ptr, name_len // 2) if name_ptr and name_len else ""
        created = (create - _EPOCH_DIFF_100NS) / 1e7 if create > _EPOCH_DIFF_100NS else boot_time
        out.append((pid, name or _SPECIAL_NAMES.get(pid, "?"), created, (user + kernel) / 1e7, rss))
        if not nxt:
            break
        off += nxt
    return out


class Snapshotter:
    """Reads the process table; keeps its buffer between calls."""

    def __init__(self) -> None:
        if not AVAILABLE:
            raise OSError("needs Windows and 64-bit Python")
        self._query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
        self._query.restype = ctypes.c_ulong
        self._query.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                ctypes.POINTER(ctypes.c_ulong)]
        self._buf = ctypes.create_string_buffer(512 * 1024)
        import psutil
        self._boot_time = psutil.boot_time()

    def read(self) -> list[tuple[int, str, float, float, int]]:
        need = ctypes.c_ulong(0)
        for _ in range(5):
            status = self._query(SYSTEM_PROCESS_INFORMATION, self._buf, len(self._buf), ctypes.byref(need))
            if status == STATUS_INFO_LENGTH_MISMATCH:
                # The table can grow between the two calls: leave some slack.
                self._buf = ctypes.create_string_buffer(max(need.value, len(self._buf)) + 128 * 1024)
                continue
            if status != 0:
                raise OSError(f"NtQuerySystemInformation failed: 0x{status:08X}")
            return parse(self._buf, need.value, ctypes.wstring_at, self._boot_time)
        raise OSError("process table kept growing")
