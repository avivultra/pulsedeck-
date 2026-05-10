"""
Append human-readable spike hints to history/spike_reports.md when metrics
jump sharply. Lists top processes by RSS (good RAM spike suspects).
"""

from __future__ import annotations

from pathlib import Path

import psutil

from monitor import Snapshot

RAM_JUMP_PCT = 6.0
CPU_JUMP_PCT = 12.0
CPU_HIGH_ABS = 88.0
RAM_HIGH_ABS = 92.0


def _fmt_mib(rss: int) -> str:
    return f"{rss / (1024 * 1024):.0f} MiB"


def _top_by_rss(limit: int = 8) -> list[tuple[str, int]]:
    rows: list[tuple[str, int, int]] = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = p.info
            mi = info.get("memory_info")
            if mi is None:
                continue
            rss = int(mi.rss)
            name = (info.get("name") or "?")[:48]
            pid = int(info.get("pid") or 0)
            rows.append((name, rss, pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError, AttributeError):
            continue
    rows.sort(key=lambda x: x[1], reverse=True)
    out: list[tuple[str, int]] = []
    seen_pid: set[int] = set()
    for name, rss, pid in rows:
        if pid in seen_pid:
            continue
        seen_pid.add(pid)
        out.append((name, rss))
        if len(out) >= limit:
            break
    return out


def detect_spike(prev: Snapshot, curr: Snapshot) -> tuple[bool, str, str]:
    """Return (is_spike, reason_text, trigger). trigger is 'cpu' | 'ram' | 'both' | ''."""
    ok, reason = _should_report(prev, curr)
    if not ok:
        return False, "", ""
    has_cpu = "CPU" in reason
    has_ram = "RAM" in reason
    if has_cpu and has_ram:
        trig = "both"
    elif has_cpu:
        trig = "cpu"
    else:
        trig = "ram"
    return True, reason, trig


def _should_report(prev: Snapshot, curr: Snapshot) -> tuple[bool, str]:
    d_ram = curr.ram_percent - prev.ram_percent
    d_cpu = curr.cpu_percent - prev.cpu_percent
    reasons: list[str] = []
    if d_ram >= RAM_JUMP_PCT:
        reasons.append(f"RAM עלה ב־{d_ram:+.1f}% (מ־{prev.ram_percent:.1f}% ל־{curr.ram_percent:.1f}%)")
    if d_ram <= -RAM_JUMP_PCT:
        reasons.append(f"RAM ירד ב־{d_ram:+.1f}% (מ־{prev.ram_percent:.1f}% ל־{curr.ram_percent:.1f}%)")
    if d_cpu >= CPU_JUMP_PCT:
        reasons.append(f"CPU עלה ב־{d_cpu:+.1f}% (מ־{prev.cpu_percent:.1f}% ל־{curr.cpu_percent:.1f}%)")
    if d_cpu <= -CPU_JUMP_PCT:
        reasons.append(f"CPU ירד ב־{d_cpu:+.1f}% (מ־{prev.cpu_percent:.1f}% ל־{curr.cpu_percent:.1f}%)")
    if curr.cpu_percent >= CPU_HIGH_ABS:
        reasons.append(f"CPU גבוה במיוחד: {curr.cpu_percent:.1f}%")
    if curr.ram_percent >= RAM_HIGH_ABS and d_ram >= 2.0:
        reasons.append(f"RAM גבוה: {curr.ram_percent:.1f}%")
    if not reasons:
        return False, ""
    return True, " · ".join(reasons)


def maybe_append_spike_report(prev: Snapshot, curr: Snapshot, report_path: Path) -> None:
    """Append a spike entry. `report_path` may be a directory or a file:
    - If a directory: write to `<dir>/spikes-YYYY-MM-DD.md` (per-day grouping).
    - If a file: append directly (legacy behavior).
    """
    ok, reason = _should_report(prev, curr)
    if not ok:
        return
    from datetime import datetime as _dt

    p = Path(report_path)
    if p.suffix == "" or p.is_dir():
        p.mkdir(parents=True, exist_ok=True)
        today = _dt.now().strftime("%Y-%m-%d")
        target = p / f"spikes-{today}.md"
    else:
        target = p
    target.parent.mkdir(parents=True, exist_ok=True)

    # If today's file is brand new, start with a date heading
    is_new_file = not target.exists() or target.stat().st_size == 0

    now_str = _dt.now().strftime("%H:%M:%S")
    top = _top_by_rss(8)
    parts: list[str] = []
    if is_new_file:
        parts.extend([
            f"# דוח חריגות — {_dt.now().strftime('%Y-%m-%d')}",
            "",
            "כל אירוע מתועד עם שעה, סיבה, ומועמדים אפשריים מבין התהליכים.",
            "",
            "---",
        ])
    parts.extend([
        "",
        f"## ⚠ {now_str} — {reason}",
        "",
        f"- **CPU:** {prev.cpu_percent:.1f}% → {curr.cpu_percent:.1f}%",
        f"- **RAM:** {prev.ram_percent:.1f}% → {curr.ram_percent:.1f}%",
    ])
    if curr.disk_percent is not None:
        parts.append(f"- **דיסק:** {curr.disk_percent:.1f}%")
    if curr.temp_celsius is not None:
        parts.append(f"- **טמפרטורה:** {curr.temp_celsius:.1f}°C")
    parts.extend(
        [
            "",
            "**תהליכים עם זיכרון (RSS) גבוה** (מועמדים עיקריים לעומס RAM):",
            "",
        ]
    )
    parts.extend(f"{i}. `{name}` — {_fmt_mib(rss)}" for i, (name, rss) in enumerate(top, 1))
    parts.extend(
        [
            "",
            "*סיכום אוטומטי — לעומסי CPU קצרים מומלץ גם Task Manager.*",
            "",
            "---",
        ]
    )
    with target.open("a", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")
