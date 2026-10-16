"""PulseDeck as an MCP server, so Claude Code (or any MCP client) can ask what
it left running and clean up after itself.

Started by the client over stdio, only for the length of a session: no
background service, nothing runs when no agent is connected. Speaks plain
JSON-RPC 2.0 (one message per line), no extra dependency.

Tools:
  pulsedeck_status        how the computer is doing, the heaviest programs
  pulsedeck_leftovers     processes running from a project folder (dev servers,
                          watchers, test runners), with ports, idle time, memory
  pulsedeck_port          who is listening on a TCP port
  pulsedeck_slow_at       who was using the computer around a past time
  pulsedeck_close         close ONE process that runs from the given folder;
                          system processes and PulseDeck are refused, and the
                          MCP client asks the user before every call

Register with Claude Code (from the project folder you work in):
    claude mcp add pulsedeck -- python C:\\path\\to\\pulsedeck_mcp.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

PULSEDECK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PULSEDECK_DIR))

SERVER_INFO = {"name": "pulsedeck", "version": "1.0"}
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
_GB = 1024 ** 3
# Never offered for closing, whatever folder they run from.
_NEVER_CLOSE = {"explorer.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
                "lsass.exe", "smss.exe", "dwm.exe", "svchost.exe", "msmpeng.exe", "system",
                "claude.exe", "claude", "code.exe", "cursor.exe", "windowsterminal.exe",
                "conhost.exe", "openconsole.exe", "powershell.exe", "pwsh.exe", "cmd.exe"}


def _fmt_bytes(n: int) -> str:
    return f"{n / _GB:.1f} GB" if n >= _GB else f"{n / 1024 ** 2:.0f} MB"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def _under(path: str, folder: str) -> bool:
    if not path:
        return False
    p, f = _norm(path), _norm(folder)
    return p == f or p.startswith(f + os.sep)


# PulseDeck's own entry points; a process running one of them IS PulseDeck.
_PULSEDECK_SCRIPTS = ("monitor.py", "tray_runner.py", "pulsedeck_mcp.py")


def is_pulsedeck(cmdline: str) -> bool:
    """The command line runs one of PulseDeck's own scripts from its folder."""
    text = (cmdline or "").replace("/", "\\").lower()
    home = str(PULSEDECK_DIR).replace("/", "\\").rstrip("\\").lower()
    return any(f"{home}\\{script}" in text for script in _PULSEDECK_SCRIPTS)


def _cmdline(p: psutil.Process) -> str | None:
    try:
        return " ".join(p.cmdline())
    except (psutil.Error, OSError):
        return None


def _protected_pids() -> set[int]:
    """This server and its ancestors (the agent, its terminal).

    PulseDeck itself is recognised by its command line (is_pulsedeck), not by
    pid: it is not an ancestor of this server.
    """
    out = {os.getpid()}
    try:
        for parent in psutil.Process().parents():
            out.add(parent.pid)
    except psutil.Error:
        pass
    return out


# ------------------------------------------------------------------ tools

def tool_status(_args: dict) -> str:
    from live_chart import _group_by_name, _verdict
    cpu = psutil.cpu_percent(interval=0.5)
    vm = psutil.virtual_memory()
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            mi = p.info["memory_info"]
            procs.append(type("P", (), {"pid": p.info["pid"], "name": p.info["name"] or "?",
                                        "cpu_percent": 0.0, "rss_bytes": mi.rss if mi else 0})())
        except (psutil.Error, AttributeError):
            continue
    top = sorted(_group_by_name(procs), key=lambda g: g[2], reverse=True)[:8]
    _color, verdict, _ = _verdict(cpu, vm.percent, None)
    lines = [f"{verdict}. CPU {cpu:.0f}%, memory {vm.percent:.0f}% "
             f"({_fmt_bytes(vm.used)} of {_fmt_bytes(vm.total)}).", "Heaviest by memory:"]
    lines += [f"- {n} ×{k}: {_fmt_bytes(r)}" if k > 1 else f"- {n}: {_fmt_bytes(r)}" for n, _c, r, k in top]
    return "\n".join(lines)


def leftovers(folder: str) -> list[dict]:
    """Processes whose working folder (or command line) is inside `folder`."""
    from ghost_sweeper import _connection_map
    listening, established = _connection_map()
    protected = _protected_pids()
    now = time.time()
    out = []
    for p in psutil.process_iter(["pid", "name", "create_time", "memory_info", "cpu_times"]):
        info = p.info
        pid = info["pid"]
        if pid in protected or not pid:
            continue
        try:
            cwd = p.cwd()
        except (psutil.Error, OSError):
            cwd = ""
        cmd = _cmdline(p)
        if cmd is None and not _under(cwd, folder):
            continue
        cmd = cmd or ""
        if not _under(cwd, folder) and _norm(folder) not in os.path.normcase(cmd):
            continue
        if is_pulsedeck(cmd):
            continue
        try:
            parent = p.parent()
            parent_name = parent.name() if parent else "?"
        except psutil.Error:
            parent_name = "?"
        ct = info.get("cpu_times")
        out.append({
            "pid": pid, "name": info["name"] or "?", "create_time": info["create_time"],
            "age_min": round((now - (info["create_time"] or now)) / 60, 1),
            "memory": info["memory_info"].rss if info.get("memory_info") else 0,
            "cpu_seconds": round((ct.user + ct.system) if ct else 0.0, 1),
            "listening": sorted(listening.get(pid, ())), "connections": established.get(pid, 0),
            "parent": parent_name, "cwd": cwd,
        })
    out.sort(key=lambda d: d["memory"], reverse=True)
    return out


def tool_leftovers(args: dict) -> str:
    folder = args.get("folder") or os.getcwd()
    if not os.path.isdir(folder):
        raise ValueError(f"Not a folder: {folder}")
    items = leftovers(folder)
    if not items:
        return f"Nothing is running from {folder}."
    lines = [f"{len(items)} process(es) running from {folder}:"]
    for d in items:
        bits = [f"pid {d['pid']}", f"{d['name']}", _fmt_bytes(d["memory"]),
                f"up {d['age_min']:.0f} min", f"{d['cpu_seconds']}s CPU total", f"parent {d['parent']}"]
        if d["listening"]:
            bits.append("listening on " + ", ".join(map(str, d["listening"])))
        if d["connections"]:
            bits.append(f"{d['connections']} open connection(s) — in use")
        lines.append("- " + ", ".join(bits) + f" (create_time {d['create_time']:.0f})")
    lines.append("To close one, call pulsedeck_close with its pid, create_time and this folder.")
    return "\n".join(lines)


def tool_port(args: dict) -> str:
    port = int(args["port"])
    from ghost_sweeper import _connection_map
    listening, _ = _connection_map()
    owners = [pid for pid, ports in listening.items() if port in ports]
    if not owners:
        return f"Nothing is listening on port {port}."
    lines = []
    for pid in owners:
        try:
            p = psutil.Process(pid)
            lines.append(f"pid {pid}: {p.name()}, cwd {p.cwd()}, started "
                         f"{datetime.fromtimestamp(p.create_time()):%H:%M}")
        except psutil.Error:
            lines.append(f"pid {pid}: (no access)")
    return "\n".join(lines)


def tool_slow_at(args: dict) -> str:
    from process_history import get_default_history
    when = args["time"].strip()
    try:
        t = datetime.fromisoformat(when)
    except ValueError:
        h, m = (int(x) for x in when.split(":")[:2])
        t = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    busy = get_default_history().around(t.timestamp(), window_sec=float(args.get("window_sec", 120)))
    if not busy["cpu"] and not busy["memory"]:
        return (f"No history around {t:%Y-%m-%d %H:%M}. History is kept for 7 days and only "
                "while PulseDeck is running.")
    lines = [f"Around {t:%Y-%m-%d %H:%M}:", "Most CPU (average % of the whole machine):"]
    lines += [f"- {i['name']}: {i['cpu']:.1f}%" for i in busy["cpu"]] or ["- (nothing busy)"]
    lines.append("Most memory (peak):")
    lines += [f"- {i['name']}: {_fmt_bytes(i['rss'])}" for i in busy["memory"]]
    return "\n".join(lines)


def tool_close(args: dict) -> str:
    from alerts import terminate_process
    pid, folder = int(args["pid"]), args.get("folder") or os.getcwd()
    create_time = float(args["create_time"]) if args.get("create_time") is not None else None
    try:
        if is_pulsedeck(_cmdline(psutil.Process(pid)) or ""):
            raise ValueError("That is PulseDeck itself; it is never closed from here.")
    except psutil.Error:
        pass
    match = next((d for d in leftovers(folder) if d["pid"] == pid), None)
    if match is None:
        raise ValueError(f"pid {pid} is not running from {folder}; refusing to close it.")
    if match["name"].lower() in _NEVER_CLOSE:
        raise ValueError(f"{match['name']} is protected; close it yourself if you really mean to.")
    if match["connections"]:
        raise ValueError(f"{match['name']} (pid {pid}) has {match['connections']} open connection(s) and "
                         "is in use; not closing it.")
    outcome = terminate_process(pid, match["name"], create_time)
    messages = {"closed": f"Closed {match['name']} (pid {pid}), freed about {_fmt_bytes(match['memory'])}.",
                "already_gone": f"{match['name']} (pid {pid}) had already exited.",
                "pid_reused": f"pid {pid} now belongs to a different process; left it alone.",
                "protected": f"{match['name']} is protected; not closed.",
                "denied": f"Windows refused to close {match['name']} (pid {pid}); it may need admin rights."}
    return messages.get(outcome, outcome)


TOOLS = {
    "pulsedeck_status": (tool_status, "How the computer is doing right now and the heaviest programs.",
                         {"type": "object", "properties": {}, "additionalProperties": False}),
    "pulsedeck_leftovers": (
        tool_leftovers,
        "List processes running from a project folder (dev servers, watchers, test runners you or the "
        "user started), with listening ports, open connections, memory and age. Call this before "
        "finishing a task to see what you left running.",
        {"type": "object", "properties": {"folder": {"type": "string", "description":
                                                     "Project folder; defaults to the server's working folder."}},
         "additionalProperties": False}),
    "pulsedeck_port": (tool_port, "Who is listening on a TCP port.",
                       {"type": "object", "properties": {"port": {"type": "integer"}}, "required": ["port"],
                        "additionalProperties": False}),
    "pulsedeck_slow_at": (
        tool_slow_at,
        "Who was using the CPU and memory around a past moment (from PulseDeck's 7-day per-program "
        "history). Time as 'HH:MM' (today) or ISO 'YYYY-MM-DDTHH:MM'.",
        {"type": "object", "properties": {"time": {"type": "string"}, "window_sec": {"type": "number"}},
         "required": ["time"], "additionalProperties": False}),
    "pulsedeck_close": (
        tool_close,
        "Close ONE process that runs from the given project folder (for example a dev server you "
        "started and no longer need). Refuses system processes, editors/terminals, PulseDeck, and "
        "anything with open connections. Pass create_time from pulsedeck_leftovers.",
        {"type": "object", "properties": {"pid": {"type": "integer"}, "create_time": {"type": "number"},
                                          "folder": {"type": "string"}},
         "required": ["pid"], "additionalProperties": False}),
}


# ------------------------------------------------------------------ JSON-RPC

def handle(msg: dict) -> dict | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:                     # notification (e.g. notifications/initialized)
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return ok({"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO,
                   "instructions": "PulseDeck knows what is running on this Windows PC. Before finishing "
                                   "a task, call pulsedeck_leftovers for the project folder and offer to "
                                   "close dev servers or watchers you started that are no longer needed."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": d, "inputSchema": s} for n, (_f, d, s) in TOOLS.items()]})
    if method == "tools/call":
        params = msg.get("params") or {}
        entry = TOOLS.get(params.get("name"))
        if entry is None:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "Unknown tool"}}
        try:
            text, is_error = entry[0](params.get("arguments") or {}), False
        except Exception as e:          # tool failures go back to the model as text
            text, is_error = f"Error: {e}", True
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
