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

### Ghost Sweeper
- Background sweep (every 20 min by default) for processes, dev servers and CLI
  agents that **finished their work and were never cleaned up**
- Three independent signals, each explained on the card:
  - **Orphan** — the terminal/agent that launched it exited and left it running
  - **Idle server** — listening on a port with zero connections for the whole window
  - **Dormant** — no measurable CPU work for a long stretch
- Every finding carries the detail needed to decide without opening Task
  Manager: the **parent it belonged to** (named even after the parent died),
  **how long it has been orphaned**, whether it **finished or got stuck**,
  lifetime CPU, memory growth, thread count, listening ports, active
  connections, working directory and full command line
- A verdict per process — *finished / stuck / leaking / still working* — with a
  one-sentence explanation. A process with live network connections is demoted
  to "still working" even if it is orphaned
- Only ever reports processes **you own**; Windows services and other accounts
  are excluded by design
- Dock badge `👻 N`, per-row "hide once" / "always ignore", and a close button
  that goes through the same confirmation + protected-process guard as alerts
- **Secret masking** — a ghost's command line is the most useful field on its
  card and also where credentials live (`--basic-auth=user:pw`,
  `--password=`, `postgres://user:pw@host`). Passwords, tokens and API keys are
  masked on screen by default so a screenshot cannot leak one. A checkbox in
  the panel turns it off and on, and the choice persists. The toggle governs
  the screen; anything written to `history/sweeper.log` is masked
  unconditionally
- **🧹 Clean all** — one button, in addition to the per-card close buttons,
  that closes everything **provably idle and nothing active**:
  - ghosts judged *finished* (never *stuck*, *leaking* or *still working*)
  - idle memory hogs: your own processes holding ≥ 100 MB that have not used
    ≥ 1 % of a core for 5 minutes, with no window on screen (their own or a
    parent's), no network activity, outside `C:\Windows`, and not security
    software, sync clients, password managers or dev runtimes. Same-name
    children (browser helpers) are judged together with their parent
  - every item is listed with its RAM and can be unticked; after you confirm,
    each one is **re-sampled for 3 seconds** and anything that woke up is
    skipped and reported, not closed
  - open Claude Code sessions are never offered as hogs — a session waiting
    for your next message looks idle but is in use

### History
- **CSV log** every second to `history/regular/metrics.csv`
- **Weekly rotation** — rows older than 7 days move to `metrics-YYYY-WW.csv`
  archive files; older than 12 weeks are pruned
- **Spike log per day** in `history/spikes/spikes-YYYY-MM-DD.md` (Markdown,
  human-readable, with timestamp + reason + top processes)
- **Sweep log** in `history/sweeper.log` (rotated) — one summary line per
  sweep plus `NEW` / `GONE` / `USER` events, so weeks later you can answer
  "which project keeps leaking processes" and "do ghosts pile up or get
  cleaned up". Command lines are **always masked here**, regardless of the
  panel's display toggle — a log file outlives the session
- **Application log** in `history/monitor.log` (rotated, configurable level)
- **Freeze log** in `history/freeze.log` — if the dock stops responding for
  30 s, the stack of every thread is written here (via `faulthandler`, which
  works even when the whole interpreter is stuck). Empty apart from one
  start-up line per session means no freezes

---

## Quick start

```bash
git clone https://github.com/avivultra/pulsedeck-.git pulsedeck
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

### Windows — one-click install

Double-click **`Install.bat`**. It finds Python 3.10+ (the `py` launcher, the
usual install folders, or whatever is on PATH including the Microsoft Store
Python — each candidate is actually run, so the Store's "install Python" stub
is skipped), installs the packages into that Python, and creates a
**PulseDeck** desktop shortcut that starts that same Python, so the monitor
never runs under an interpreter that lacks its packages.

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
| `--sweeper` / `--no-sweeper` | true | Ghost Sweeper (abandoned processes) |
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
- `sweeper.scan_interval_minutes` / `idle_minutes` — how often to sweep, and how
  long a process must be quiet before it counts as abandoned
- `sweeper.redact_secrets` — mask passwords/tokens in displayed command lines
- `sweeper.ignored_names` / `ignored_instances` — the panel's "ignore" buttons
  write here
- `rotation.weeks_to_keep` — archive retention

---

## Architecture

```
monitor.py            ← entry point; sets up logging, config, dispatchers
├── config.py         ← config.json load/save
├── dependencies.py   ← startup checks for psutil / pystray / matplotlib / PIL
├── metric_history.py ← CSV append, weekly rotation, archive pruning
├── temperature_readings.py  ← cached entry point, routes to probe chains
├── gpu_probes.py            ← NVIDIA / AMD / Intel Arc / Linux sysfs chain
├── cpu_probes.py            ← psutil / Windows WMI / Linux thermal / macOS
│
├── dock_strip.py     ← Tk dock UI (drag/resize/pin/font-scale)
├── tray_runner.py    ← pystray system-tray icon
├── live_chart.py     ← Tk + matplotlib live chart window
│
├── process_monitor.py ← background sampler; per-PID activity & RSS
├── spike_reporter.py  ← spike detection + per-day markdown log
├── alerts.py          ← AlertEvent, dispatcher (cooldown/snooze/mute),
│                        toast, full alert window, safe try_terminate
├── janitor.py         ← conhost zombie scanner + cleanup panel
├── ghost_sweeper.py   ← abandoned-process detection (lineage + activity)
└── ghost_panel.py     ← the sweeper's report window
```

Threading model: one daemon thread per long-lived service (process sampler,
janitor scanner), all results read by the Tk main thread via `root.after()`.
No multiprocessing, no asyncio — keeps the dependency surface small.

---

## Performance

**Nothing slow runs on the UI thread.** That is the single rule the dock's
responsiveness depends on, and it was learned the hard way:

- **Sensor reads are asynchronous.** `read_primary_temp_celsius()` and the GPU
  readers return a cached value instantly; the actual probes — which spawn
  `powershell` (up to a 4 s timeout) and `nvidia-smi` (~500 ms) — run on a
  dedicated refresher thread. Previously these ran inline in the dock's
  once-per-second `tick()`, freezing the whole UI every 8–10 seconds.
- **Probes that keep failing back off.** After 3 consecutive failures the retry
  interval grows geometrically up to 15 minutes. On a laptop whose WMI does not
  expose `MSAcpi_ThermalZoneTemperature`, this turns an endless
  PowerShell-spawn-every-8-seconds into a handful of attempts and then silence.
- **The same treatment for swap.** `psutil.swap_memory()` raises on Windows
  machines with the PDH performance counters disabled — and the *failing* call
  costs a median 4 ms with spikes near 900 ms. It is now negatively cached.
- **CSV appends are memoised.** Writing one row used to re-`mkdir` the parent,
  `stat` twice and re-read the file header every second. The header check is
  now cached and invalidated only on truncation or rotation.
- **One spike entry per load episode.** While CPU stayed above 88 % the spike
  reporter wrote an entry — and walked the whole process table on the UI
  thread — every second (2,002 entries in one day). Under 100 % CPU the dock
  starved itself until Windows closed it as "not responding". Entries are now
  at most one a minute, and the top-process list comes from the background
  sampler's snapshot instead of a fresh scan.
- **No PowerShell once WMI says "no sensor".** Three empty answers and the WMI
  temperature probe stops for the session; timeouts no longer escape as
  errors. `nvidia-smi` runs every 30 s instead of 10 s.

Measured on the development machine, the per-tick data collection went from
recurring 400–900 ms stalls to a **median of 4.6 ms, p95 8 ms**.

Adaptive sampling is the other half:
- **Process sampler**: 2 s tick during the 30 s after any alert (so process data
  is fresh when the user opens a popup), 5 s tick when idle
- **Janitor scanner**: 5 min tick (configurable), with cached parent-name lookup
- **Live chart**: incremental CSV reading — only the tail since the last refresh
  is parsed (~86× faster than a full re-read of a 1.5 MB file)
- **Dock**: `place_window()` runs only when the dock actually moves; topmost
  re-assertion throttled to every 5 ticks (~5 s)
- **Ghost Sweeper**: one sweep every 20 minutes on its own thread. Expensive
  per-process lookups (`cmdline`, `cwd`, `username`) are done only for the
  handful of processes that actually get flagged — including `username` in the
  bulk `process_iter` alone took a sweep from 1.7 s to 5.2 s

Typical footprint: **~90 MB RAM, ~1 % CPU** (measured on an 8-thread laptop).

---

## Testing

```bash
pytest
```

149 unit tests across `test_monitor.py`, `test_ghost_sweeper.py`,
`test_i18n.py`, `test_hardware.py` and
`test_idle_cleanup.py` (clean-all never offers or closes anything active,
terminate outcomes, the freeze log, the WMI give-up).

`test_ghost_sweeper.py` covers the sweeper (parent resolution under PID reuse,
verdict classification, first-sighting estimates, cross-scan stability of
reported ages, the ignore list, secret redaction in both directions — masked
credentials and untouched ordinary arguments) plus the responsiveness work
(the sensor refresher's non-blocking contract and backoff, and the CSV
fast path) and the sweep log (one NEW per ghost, GONE on disappearance,
unconditional masking on disk, and isolation so the suite never touches the
user's real log).

`test_monitor.py` covers: config load/save, CSV rotation logic, archive pruning,
dependency validation, alert formatters, protected-process guards, spike
detection, cooldown gating, mute-list suppression, snooze, oscillation dedup,
janitor scanning rules, parent-name caching, and the GPU/CPU probe-chain
fallback behaviour (first success wins, exceptions don't poison the chain,
unavailable probes are skipped).

---

## Hardware & OS compatibility

PulseDeck is designed to **run on any machine** — Intel, AMD, ARM, Apple
Silicon, etc. Core monitoring works everywhere via `psutil`. Sensor-level
metrics (GPU, CPU temperature) are read with **graceful fallback**: if the
hardware doesn't expose a sensor, the value simply shows `—` and the app
keeps running.

### Metrics — what works on which machine

| Metric | Where it works | If unavailable |
|---|---|---|
| CPU %, core count | **Every machine** (Intel/AMD/ARM/Apple Silicon) | — |
| RAM, swap | **Every machine** | — |
| Disk usage (any drive) | **Every machine** | — |
| Network throughput | **Every machine** | — |
| Battery + AC status | Devices with a battery (laptops, tablets) | Hidden in UI |
| Uptime | **Every machine** | — |
| Top processes (CPU/RAM) | **Every machine** | — |
| **CPU temperature** | Multi-source probe chain — psutil first, then Windows WMI, then Linux `/sys/class/thermal`, with a macOS placeholder. Covers most modern machines. | Shows `—` |
| **GPU temperature** | **NVIDIA** (`nvidia-smi`) · **AMD** (`amd-smi` or `rocm-smi`, or Linux `/sys/class/drm/` sysfs fallback) · **Intel Arc / Flex** (`xpu-smi`) | Shows `—` |
| **GPU VRAM** | **NVIDIA** + **AMD** (with vendor CLI). Linux sysfs fallback is temperature-only. Integrated GPUs partial. | Hidden |
| Spike alerts, kill, history | **Every machine** | — |

### OS-specific features

| Feature | Windows | Linux | macOS |
|---|---|---|---|
| Core monitoring | ✅ | ✅ | ✅ |
| Dock (drag/pin/resize) | ✅ (above the taskbar on any edge; remembers a spot on a second monitor while it is connected) | ✅ (sensible defaults) | ✅ (sensible defaults) |
| System tray | ✅ | ✅ | ✅ |
| Live chart | ✅ | ✅ | ✅ |
| Spike toasts | ✅ | ✅ | ✅ |
| **`conhost.exe` Janitor** | ✅ | n/a (no conhost) | n/a |
| **Ghost Sweeper** | ✅ | ✅ | ✅ |
| **Desktop shortcut installer** | ✅ (`.lnk` via PowerShell) | ✅ (`.desktop` file) | Manual hint printed |
| `Start-Monitor-Hidden.vbs` | ✅ | n/a (use `python monitor.py`) | n/a |

**Windows 10 and 11 are the fully supported targets** — every feature is
built and tested there. Linux and macOS run the core monitor, dock, tray and
chart, but get far less testing; bug reports welcome.

### Fan control (Lenovo Legion — optional)

- A **🌀 fan button** on the far left of the dock toggles Lenovo "Extreme
  Cooling" via its Nerve Sense keyboard shortcut (`Ctrl+Shift+1`). No admin,
  no drivers — just a synthetic hotkey.
- `fan_auto.py` is a standalone auto-controller: turns Extreme Cooling ON when
  GPU temp ≥ 50 °C and OFF below 43 °C (hysteresis, configurable via
  `--on`/`--off`). Run `python fan_auto.py --test` to verify the hotkey first.
- **Shown only where it works.** The button appears when the machine is a
  Lenovo with Nerve Center / Nerve Sense installed; on every other machine it
  is hidden (the hotkey would otherwise land in whatever window has focus).
  Override in `config.json`: `"hardware": {"fan_button": true | false | "auto"}`.

### Other notes

- **English and Hebrew UI.** The language follows the Windows display
  language (Hebrew → Hebrew, anything else → English), with a right-to-left
  layout for Hebrew. Switch any time from the dock's right-click menu
  (🌐 שפה / Language) or set `"ui": {"language": "en" | "he" | "auto"}` in
  `config.json`; it applies on the next start. Every string is written as
  `tr("עברית", "English")` in place, so adding a language means extending
  `i18n.py` — PRs welcome.
- **`Start-Monitor.bat` / `.vbs`** try the usual Python locations and the `py`
  launcher. The desktop shortcut made by `Install.bat` does not depend on them.

---

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, sell it, mix it into your own
dashboard. Just keep the copyright notice.

---

## Contributing

PRs are welcome — small focused changes especially. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the quick guide.
