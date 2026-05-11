# PulseDeck — Real-time System Performance Monitor

> A lightweight Python desktop monitor that watches your CPU, RAM, disk, GPU, network,
> battery, and temperatures in real time. Detects load spikes and surfaces the actual
> culprits — with a safe, confirmation-only kill button. Designed to run quietly in the
> background without weighing the machine down.

![Built with Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)

---

## Why this exists

Windows Task Manager is great when you're already in trouble. PulseDeck is for the
30 seconds **before** that — when your machine starts to stutter and you want to know,
without clicking, *what's actually eating the CPU right now*.

Three core promises:

1. **Real-time data** — second-by-second sampling, written to a CSV you can analyse later.
2. **Spike alerts** — when CPU/RAM jumps sharply, a gentle bottom-right toast shows the
   top processes responsible. Click for a full window with Kill buttons.
3. **Low overhead** — adaptive sampling (slows down when idle), batched `psutil` calls,
   incremental CSV reads in the live chart. Typical footprint: ~80 MB RAM, well under
   1% CPU on modern hardware.

---

## Features

### Monitoring
- **CPU, RAM, Disk** (any drive — `--disk E:\`), **Swap**, **Network** (up/down rate)
- **Temperatures**: CPU (psutil + Windows WMI fallback) and **NVIDIA GPU** (via `nvidia-smi`)
- **VRAM** usage on NVIDIA cards
- **Battery** percent and AC status
- **System uptime**

### UI modes (can be combined)
- **Console** — text dashboard in a terminal
- **Dock** — slim floating panel pinned above the taskbar; drag to reposition,
  right-click for menu, resize the font, pin/unpin
- **Tray** — minimal system-tray icon with tooltip
- **Live chart window** — embedded matplotlib chart with a time-window selector
  (5 min / 15 min / 1 h / 6 h / 24 h / all), pan/zoom toolbar, optional archive
  inclusion for long-range views

### Alerts
- **Spike detection** on configurable thresholds (default: 12% CPU jump, 6% RAM jump
  within one sample)
- **Toast notification** in the bottom-right that auto-dismisses in 8 seconds; click
  it for the full detail window
- **Full alert window** lists Top 5 CPU + Top 5 RAM processes with:
  - Activity indicator (active now / active X minutes ago / background)
  - Process uptime
  - **Kill button** — requires `Yes` confirmation; system-critical processes
    (`svchost`, `csrss`, `winlogon`, `lsass`, ...) and the monitor itself are
    protected and cannot be killed
- **Cooldown** (default 5 min) prevents toast spam
- **Right-click on toast** → snooze for 15 / 30 / 60 minutes
- **Mute list** — silence noisy known apps via `config.json`

### Health Janitor (Windows-focused)
- Background scanner that detects accumulated `conhost.exe` zombie groups
  (a common artefact when CLI tools like `claude-code`, `electron`, `node` spawn
  many short-lived shells without cleanup)
- Dock badge `🧹 N` appears only when zombies are detected
- One-click cleanup window — never kills automatically; every action is logged to
  `history/janitor.log` for audit

### History
- **CSV log** every second to `history/regular/metrics.csv`
- **Weekly rotation** — rows older than 7 days move to `metrics-YYYY-WW.csv`
  archive files; older than 12 weeks are pruned
- **Spike log per day** in `history/spikes/spikes-YYYY-MM-DD.md` (Markdown,
  human-readable, with timestamp + reason + top processes)
- **Application log** in `history/monitor.log` (rotated, configurable level)

---

## Quick start

```bash
git clone https://github.com/<your-user>/pulsedeck.git
cd pulsedeck
pip install -r requirements.txt

# Run with dock + tray + history logging
python monitor.py --dock --history --tray

# One-shot snapshot (no loop)
python monitor.py --once

# Console mode, custom disk
python monitor.py --disk E:\

# Save current flags as defaults
python monitor.py --dock --tray --history --save-config
```

On first run, `config.json` is created from `config.example.json`-style defaults
and lives next to `monitor.py`.

### Windows launchers

| File | Use case |
|------|----------|
| `Start-Monitor-Hidden.vbs` | Recommended — runs in the background, no console window |
| `Start-Monitor.bat`        | Same, but a console window stays open |
| `Start-Monitor-Debug.bat`  | Verbose logging visible in a console window |

To launch at every boot, drop a shortcut to `Start-Monitor-Hidden.vbs` into
`shell:startup` (Win+R → `shell:startup` → Enter → drag the shortcut in).

---

## CLI reference

| Flag | Default | Notes |
|------|---------|-------|
| `--dock` / `--no-dock` | from config | Floating panel above the taskbar |
| `--tray` / `--no-tray` | from config | System tray icon |
| `--history` / `--no-history` | from config | Append to CSV |
| `--alerts` / `--no-alerts` | true | Show spike toasts |
| `--alert-cooldown SEC` | 300 | Min seconds between toasts |
| `--janitor` / `--no-janitor` | true | conhost zombie scanner |
| `--disk PATH` | system drive | Drive to monitor (`E:\`, `/mnt/data`) |
| `--interval SEC` | 1.0 | Loop sample period |
| `--once` | off | One snapshot, exit |
| `--tray-interval SEC` | 5 | Tray tooltip refresh period |
| `--log-level LEVEL` | WARNING | DEBUG / INFO / WARNING / ERROR |
| `--weeks-to-keep N` | 12 | CSV archive retention |
| `--save-config` | — | Persist current args to `config.json` |

---

## Configuration

All preferences live in `config.json`. CLI flags override config values.
See [`config.example.json`](config.example.json) for the full schema.

Key sections:
- `ui` — which UIs to launch by default
- `spike.cpu_threshold` / `ram_threshold` — sensitivity of spike detection
- `alerts.cooldown_seconds`, `muted_processes`, `sound_enabled`
- `dock.x`/`y`/`font_scale`/`pinned` — remembered between sessions
- `janitor.conhost_threshold_per_parent` — minimum group size to flag
- `rotation.weeks_to_keep` — archive retention

---

## Architecture

```
monitor.py            ← entry point; sets up logging, config, dispatchers
├── config.py         ← config.json load/save
├── dependencies.py   ← startup checks for psutil / pystray / matplotlib / PIL
├── metric_history.py ← CSV append, weekly rotation, archive pruning
├── temperature_readings.py  ← psutil + WMI + nvidia-smi
│
├── dock_strip.py     ← Tk dock UI (drag/resize/pin/font-scale)
├── tray_runner.py    ← pystray system-tray icon
├── live_chart.py     ← Tk + matplotlib live chart window
│
├── process_monitor.py ← background sampler; per-PID activity & RSS
├── spike_reporter.py  ← spike detection + per-day markdown log
├── alerts.py          ← AlertEvent, dispatcher (cooldown/snooze/mute),
│                        toast, full alert window, safe try_terminate
└── janitor.py         ← conhost zombie scanner + cleanup panel
```

Threading model: one daemon thread per long-lived service (process sampler,
janitor scanner), all results read by the Tk main thread via `root.after()`.
No multiprocessing, no asyncio — keeps the dependency surface small.

---

## Performance

Adaptive sampling is the centerpiece:
- **Process sampler**: 2 s tick during the 30 s after any alert (so process data
  is fresh when the user opens a popup), 5 s tick when idle
- **Janitor scanner**: 5 min tick (configurable), with cached parent-name lookup
- **Live chart**: incremental CSV reading — only the tail since the last refresh
  is parsed (~86× faster than a full re-read of a 1.5 MB file)
- **Dock**: `place_window()` runs only when the dock actually moves; topmost
  re-assertion throttled to every 5 ticks (~5 s)

Typical footprint: **~80 MB RAM, < 1 % CPU** on a modern desktop.

---

## Testing

```bash
pytest test_monitor.py
```

38 unit tests cover: config load/save, CSV rotation logic, archive pruning,
dependency validation, alert formatters, protected-process guards, spike
detection, cooldown gating, mute-list suppression, snooze, oscillation dedup,
janitor scanning rules, parent-name caching.

---

## Notes & limitations

- **UI is currently Hebrew** (developer is a Hebrew speaker). Strings are
  centralised enough to localise — PRs welcome.
- **GPU temperature/VRAM**: NVIDIA only (via `nvidia-smi`). AMD/Intel Arc
  support is not implemented.
- **Linux/macOS**: most features work, but the dock is Windows-tuned. Tray
  layouting and taskbar geometry use Win32 APIs where available.
- **`Start-Monitor.bat` / `.vbs`** assume Python is in standard
  `%LocalAppData%\Programs\Python\` locations. Adjust if installed elsewhere.

---

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, sell it, mix it into your own
dashboard. Just keep the copyright notice.

---

## Contributing

PRs are welcome — small focused changes especially. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the quick guide.
