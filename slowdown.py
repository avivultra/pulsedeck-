"""Find the moments the computer was slow, and say who was behind them.

Works on data PulseDeck already keeps: the per-second CPU/RAM history (as the
float arrays of live_chart's data layer) and the per-program history
(process_history). Pure functions, run only when someone asks.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from i18n import tr

CPU_BUSY = 70.0          # % of the whole machine
RAM_BUSY = 85.0          # % of memory in use
MIN_SECONDS = 30         # shorter bursts are not "slow", just busy
MERGE_GAP_SECONDS = 90   # two bursts closer than this are one episode

_EPOCH = datetime(1970, 1, 1)


def days_to_datetime(d: float) -> datetime:
    """live_chart's wall-clock days -> naive local datetime."""
    return _EPOCH + timedelta(days=float(d))


def find_episodes(t, cpu, ram, *, cpu_busy: float = CPU_BUSY, ram_busy: float = RAM_BUSY,
                  min_seconds: float = MIN_SECONDS, merge_gap: float = MERGE_GAP_SECONDS) -> list[dict]:
    """Stretches where CPU or memory stayed high.

    `t` is in wall-clock days (sorted), `cpu`/`ram` in percent (NaN allowed).
    Returns [{"start", "end" (days), "peak_cpu", "peak_ram", "kind"}], oldest
    first; kind is "cpu", "ram" or "both".
    """
    import numpy as np
    t = np.asarray(t, dtype=float)
    cpu = np.nan_to_num(np.asarray(cpu, dtype=float), nan=0.0)
    ram = np.nan_to_num(np.asarray(ram, dtype=float), nan=0.0)
    if len(t) == 0:
        return []
    busy = (cpu >= cpu_busy) | (ram >= ram_busy)
    edges = np.flatnonzero(np.diff(np.r_[False, busy, False].astype(int)))
    runs = list(zip(edges[::2], edges[1::2]))            # [start, end) indices
    gap_days = merge_gap / 86400
    merged: list[list[int]] = []
    for s, e in runs:
        if merged and t[s] - t[merged[-1][1] - 1] <= gap_days:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    out = []
    for s, e in merged:
        start, end = t[s], t[e - 1]
        if (end - start) * 86400 < min_seconds:
            continue
        pc, pr = float(cpu[s:e].max()), float(ram[s:e].max())
        hot_cpu, hot_ram = pc >= cpu_busy, pr >= ram_busy
        kind = "both" if hot_cpu and hot_ram else "cpu" if hot_cpu else "ram"
        out.append({"start": float(start), "end": float(end), "peak_cpu": pc, "peak_ram": pr, "kind": kind})
    return out


def describe(ep: dict, busy: dict | None) -> str:
    """One plain sentence for an episode, naming who was behind it."""
    start, end = days_to_datetime(ep["start"]), days_to_datetime(ep["end"])
    minutes = max(1, round((end - start).total_seconds() / 60))
    when = f"{start:%H:%M}–{end:%H:%M}"
    if ep["kind"] == "ram":
        what = tr(f"הזיכרון הגיע ל-{ep['peak_ram']:.0f}%", f"memory reached {ep['peak_ram']:.0f}%")
    elif ep["kind"] == "both":
        what = tr(f"המעבד הגיע ל-{ep['peak_cpu']:.0f}% והזיכרון ל-{ep['peak_ram']:.0f}%",
                  f"CPU reached {ep['peak_cpu']:.0f}% and memory {ep['peak_ram']:.0f}%")
    else:
        what = tr(f"המעבד הגיע ל-{ep['peak_cpu']:.0f}%", f"CPU reached {ep['peak_cpu']:.0f}%")
    dur = tr(f"{minutes} דק׳", f"{minutes} min")
    who = ""
    if busy:
        lst = busy["memory"] if ep["kind"] == "ram" else busy["cpu"]
        if ep["kind"] != "ram":
            lst = [i for i in lst if i["cpu"] >= 0.5]
        names = [f"{i['name']} ({i['cpu']:.0f}%)" if ep["kind"] != "ram" else i["name"] for i in lst[:3]]
        if names:
            who = tr(" · בעיקר ", " · mostly ") + ", ".join(names)
    return f"{when} ({dur}): {what}{who}"
