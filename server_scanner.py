"""Open-server scanner — find processes listening on TCP ports and offer
to shut them down (with confirmation, never automatically).

Typical catches: forgotten `npm run dev`, `next dev`, Django `runserver`,
`uvicorn`, XAMPP Apache/MySQL, etc.

Scan is on-demand (menu click) — no background thread, zero idle cost.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import psutil

log = logging.getLogger(__name__)

# Well-known dev/server ports → friendly hint shown next to the name
KNOWN_PORTS: dict[int, str] = {
    80: "HTTP", 443: "HTTPS", 3000: "React/Next dev", 3001: "dev server",
    4200: "Angular dev", 5000: "Flask", 5173: "Vite dev", 5432: "PostgreSQL",
    8000: "Django/uvicorn", 8080: "HTTP alt", 8888: "Jupyter",
    3306: "MySQL/MariaDB", 6379: "Redis", 27017: "MongoDB",
    1433: "SQL Server", 9000: "PHP-FPM", 8081: "dev server",
}

# Server-ish process names that are interesting even on unknown ports
SERVERISH_NAMES: frozenset[str] = frozenset({
    "node.exe", "node", "python.exe", "python", "pythonw.exe",
    "php.exe", "php", "httpd.exe", "httpd", "nginx.exe", "nginx",
    "mysqld.exe", "mysqld", "postgres.exe", "postgres", "redis-server",
    "java.exe", "java", "dotnet.exe", "dotnet", "ruby.exe", "ruby",
    "deno.exe", "deno", "bun.exe", "bun", "caddy.exe", "caddy",
    "mongod.exe", "mongod", "uvicorn", "gunicorn",
})

# System listeners we hide by default — killing them breaks Windows itself
SYSTEM_LISTENER_NAMES: frozenset[str] = frozenset({
    "svchost.exe", "services.exe", "lsass.exe", "wininit.exe", "system",
    "spoolsv.exe", "searchhost.exe",
})


@dataclass(frozen=True)
class ListeningServer:
    pid: int
    name: str
    ports: tuple[int, ...]
    cmdline_hint: str        # short human hint (e.g. "npm run dev", "uvicorn app:app")
    port_hint: str           # e.g. "Vite dev" for 5173
    is_system: bool


def _cmdline_hint(proc: psutil.Process) -> str:
    """Short, recognisable fragment of the command line."""
    try:
        parts = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""
    if not parts:
        return ""
    # Drop the interpreter path, keep the script/args essence
    tail = [p for p in parts[1:] if not p.startswith("-")][:3]
    joined = " ".join(os.path.basename(p) if os.sep in p or "/" in p else p
                       for p in tail)
    return joined[:60]


def scan_listening_servers(include_system: bool = False) -> list[ListeningServer]:
    """Return processes that have at least one LISTEN tcp socket.

    Grouped per PID with all their listening ports. Sorted: user dev servers
    first (known dev ports), then by port number.
    """
    by_pid: dict[int, set[int]] = {}
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        log.exception("net_connections failed (may need elevation on this OS)")
        return []

    for c in conns:
        if c.status != psutil.CONN_LISTEN or not c.pid:
            continue
        by_pid.setdefault(c.pid, set()).add(c.laddr.port)

    self_pid = os.getpid()
    servers: list[ListeningServer] = []
    for pid, ports in by_pid.items():
        if pid == self_pid:
            continue
        try:
            proc = psutil.Process(pid)
            name = (proc.name() or "?").strip()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

        is_system = name.lower() in SYSTEM_LISTENER_NAMES
        if is_system and not include_system:
            continue

        sorted_ports = tuple(sorted(ports))
        port_hint = next((KNOWN_PORTS[p] for p in sorted_ports if p in KNOWN_PORTS), "")
        servers.append(ListeningServer(
            pid=pid, name=name, ports=sorted_ports,
            cmdline_hint=_cmdline_hint(proc),
            port_hint=port_hint,
            is_system=is_system,
        ))

    def _sort_key(s: ListeningServer):
        dev_port = any(p in KNOWN_PORTS for p in s.ports)
        serverish = s.name.lower() in SERVERISH_NAMES
        return (not serverish, not dev_port, s.ports[0] if s.ports else 0)

    servers.sort(key=_sort_key)
    return servers


# ---------- UI panel ----------

def open_servers_panel(parent) -> "object":
    """Toplevel listing listening servers with per-row stop buttons."""
    import tkinter as tk
    from tkinter import messagebox

    from alerts import try_terminate  # reuses confirmation + protection logic

    BG, PANEL, PANEL_HI = "#0d1117", "#161b22", "#1c2230"
    BORDER, FG, DIM = "#30363d", "#e6edf3", "#7d8590"
    GREEN, BAD, BLUE = "#3fb950", "#ff5c6c", "#58a6ff"

    win = tk.Toplevel(parent) if parent is not None else tk.Tk()
    win.title("שרתים פתוחים")
    win.geometry("620x480")
    win.configure(bg=BG)
    try:
        win.attributes("-topmost", True)
    except tk.TclError:
        pass

    hdr = tk.Frame(win, bg=PANEL, padx=18, pady=12)
    hdr.pack(fill="x")
    tk.Label(hdr, text="🌐 שרתים פתוחים במחשב", bg=PANEL, fg=BLUE,
             font=("Segoe UI", 14, "bold"), anchor="e").pack(fill="x")
    subtitle_var = tk.StringVar(value="סורק…")
    tk.Label(hdr, textvariable=subtitle_var, bg=PANEL, fg=DIM,
             font=("Segoe UI", 9), anchor="e").pack(fill="x", pady=(2, 0))

    # Scrollable body
    body_holder = tk.Frame(win, bg=BG)
    body_holder.pack(fill="both", expand=True)
    canvas = tk.Canvas(body_holder, bg=BG, highlightthickness=0, bd=0)
    scrollbar = tk.Scrollbar(body_holder, orient="vertical", command=canvas.yview)
    body = tk.Frame(canvas, bg=BG)
    body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=body, anchor="nw", width=580)
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=8)
    scrollbar.pack(side="right", fill="y")

    def _stop_server(srv: ListeningServer) -> None:
        ports_str = ", ".join(str(p) for p in srv.ports[:5])
        # try_terminate already asks confirmation + blocks protected names
        if try_terminate(srv.pid, srv.name, win):
            _refresh()

    def _refresh() -> None:
        for child in body.winfo_children():
            child.destroy()
        servers = scan_listening_servers()
        subtitle_var.set(
            f"{len(servers)} תהליכים מאזינים לפורטים  ·  לחץ ✕ לכיבוי (עם אישור)"
        )
        if not servers:
            tk.Label(body, text="לא נמצאו שרתים פתוחים (או שנדרשת הרשאת מנהל לסריקה)",
                     bg=BG, fg=DIM, font=("Segoe UI", 10), pady=30).pack(fill="x")
            return

        for srv in servers:
            outer = tk.Frame(body, bg=BORDER)
            outer.pack(fill="x", pady=3, padx=4)
            inner = tk.Frame(outer, bg=PANEL_HI)
            inner.pack(fill="both", expand=True, padx=1, pady=1)
            stripe = tk.Frame(inner, bg=GREEN if srv.port_hint else BLUE, width=3)
            stripe.pack(side="left", fill="y")
            content = tk.Frame(inner, bg=PANEL_HI, padx=12, pady=8)
            content.pack(side="left", fill="both", expand=True)

            top = tk.Frame(content, bg=PANEL_HI)
            top.pack(fill="x")
            ports_str = ", ".join(str(p) for p in srv.ports[:6])
            if len(srv.ports) > 6:
                ports_str += f" (+{len(srv.ports) - 6})"
            tk.Label(top, text=f":{ports_str}", bg=PANEL_HI, fg=GREEN,
                     font=("Cascadia Mono", 11, "bold")).pack(side="left")
            tk.Label(top, text=srv.name, bg=PANEL_HI, fg=FG,
                     font=("Segoe UI", 11, "bold"), anchor="e"
                     ).pack(side="right", fill="x", expand=True)

            meta_bits = [f"PID {srv.pid}"]
            if srv.port_hint:
                meta_bits.append(srv.port_hint)
            if srv.cmdline_hint:
                meta_bits.append(srv.cmdline_hint)
            tk.Label(content, text="  ·  ".join(meta_bits), bg=PANEL_HI, fg=DIM,
                     font=("Segoe UI", 9), anchor="e").pack(fill="x", pady=(2, 0))

            action = tk.Frame(content, bg=PANEL_HI)
            action.pack(fill="x", pady=(5, 0))
            tk.Button(action, text="✕  כבה שרת", bg=PANEL_HI, fg=BAD,
                      font=("Segoe UI", 9, "bold"), relief="flat", bd=0,
                      activebackground=BAD, activeforeground="white",
                      cursor="hand2", padx=10, pady=2,
                      command=lambda s=srv: _stop_server(s)).pack(side="left")

    ftr = tk.Frame(win, bg=PANEL, padx=14, pady=10)
    ftr.pack(fill="x", side="bottom")
    tk.Button(ftr, text="🔄 רענן", bg=PANEL_HI, fg=FG,
              font=("Segoe UI", 9), relief="flat", bd=0, cursor="hand2",
              padx=12, pady=5, command=_refresh).pack(side="left")
    tk.Button(ftr, text="סגור", bg=PANEL_HI, fg=FG,
              font=("Segoe UI", 9), relief="flat", bd=0, cursor="hand2",
              padx=12, pady=5, command=win.destroy).pack(side="right")

    _refresh()
    return win
