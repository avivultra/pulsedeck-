# Contributing to PulseDeck

Thanks for thinking about contributing. The project is small enough that
contribution is straightforward.

## Quick setup

```bash
git clone https://github.com/<your-user>/pulsedeck.git
cd pulsedeck
python -m venv .venv
source .venv/bin/activate          # Linux/macOS
# OR
.venv\Scripts\activate             # Windows

pip install -r requirements.txt
```

## Running tests

```bash
pytest test_monitor.py
```

All 38+ tests should pass before opening a PR. New behaviour should come with
new tests.

## Code style

- **Python 3.10+** — uses `match`, `X | Y` union types, etc.
- **Type hints** on every public function. Internal helpers are flexible.
- **No silent exception handlers.** Every `except` should either re-raise,
  return a useful fallback, or call `log.exception(...)` with context.
- **No `from foo import *`.**
- **Hebrew strings** stay in UI files (`dock_strip.py`, `alerts.py`,
  `live_chart.py`, `janitor.py`); module-level logic and logs are in English.
- **Threading**: daemon threads only. Tk is touched from the main thread —
  cross-thread updates go through `root.after(0, ...)`.

## What we like

- Bug fixes with a regression test.
- Performance improvements with before/after numbers (measure with
  `Get-Process pythonw | Select WorkingSet64, TotalProcessorTime`).
- New metric collectors (e.g. AMD GPU temp, Intel Arc VRAM) as small
  modules sitting next to `temperature_readings.py`.
- Localisation — moving Hebrew UI strings into a `locale/he.json` and
  adding `locale/en.json` would be a great PR.

## What we'd rather not have

- New runtime dependencies — psutil, pystray, Pillow, matplotlib is the
  current set; please justify additions.
- Big redesigns of the dock or alert window without discussion first.
- Auto-killing processes without explicit user confirmation. Ever.

## How to submit

1. Fork the repo, create a branch from `main`.
2. Make your change, add tests if applicable, ensure `pytest` passes.
3. Open a PR with a clear title and one-paragraph description of what
   changed and why. Screenshots help for UI changes.
