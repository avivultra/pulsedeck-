# PulseDeck — notes for Claude

## Who it is for
PulseDeck is first and foremost the owner's own tool: a Windows 10/11 machine,
Hebrew UI, heavy Claude Code use. A feature earns its place by helping that
daily use; if it helps him, it will likely help other developers too. Don't
trade his workflow (Hebrew-first, the fan button, the dock size, the strip's
data) for a hypothetical audience. Change the strip's *look* freely; change
its *content* only when asked.

## Rules that do not bend
- **The monitor must not burden the machine.** Nothing slow on the Tk thread;
  sample as rarely as the feature allows; physics/animation sleeps when
  nothing moves; skip work while a window is minimised. Measure before and
  after (CPU ms per tick, RSS) and say the numbers.
- **Never close anything automatically.** Every close is the user's decision,
  confirmed in the UI; system processes and PulseDeck itself are protected.
- **Privacy:** history stays in `history/` on the machine. "Ask Claude" sends
  program names and numbers only, never command lines, paths or user names.
- Bilingual strings via `tr("עברית", "English")`; layout via `i18n.SIDE()` etc.

## Working on it
- Tests: `python -m pytest -q` (Tk tests need a display; on Linux use Xvfb).
- Main window: `vault_window.py` (graph model `vault_graph.py`, bubbles
  `vault_sprites.py`). Strip: `dock_strip.py`. Shared data layer and spike
  parsing: `live_chart.py`. Per-program history: `process_history.py`.
