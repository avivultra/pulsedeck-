"""Auto Extreme-Cooling controller for Lenovo Legion (Nerve Sense).

Watches the GPU temperature (the sensor the dock already shows, via nvidia-smi)
and toggles Lenovo "Extreme Cooling" ON/OFF automatically using its keyboard
shortcut (Ctrl+Shift+1) with hysteresis to avoid rapid flapping.

  ON  when temp >= --on   (default 50 C)
  OFF when temp <  --off  (default 43 C)

IMPORTANT — how it works and its one limitation:
  Lenovo exposes only a TOGGLE hotkey, not separate on/off commands, and there
  is no public way to READ the current Extreme-Cooling state. So this script
  TRACKS the state itself (edge-triggered: it only sends the hotkey when the
  temperature crosses a threshold). It assumes cooling starts OFF — which is
  true right after boot/sleep/restart (the app auto-closes Extreme Cooling on
  those events). If you toggle it manually, the script may be one flip out of
  sync until the next threshold crossing.

Failure modes are benign: worst case the fan runs louder than needed, or falls
back to normal cooling. It never disables cooling entirely.

Usage:
  python fan_auto.py --test          # send ONE toggle so you can watch Nerve Sense flip
  python fan_auto.py                 # run with defaults (ON>=50, OFF<43)
  python fan_auto.py --on 55 --off 45 --interval 5
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import subprocess
import sys
import time
from pathlib import Path

# --- Win32 key injection (no external deps) ---
_user32 = ctypes.windll.user32
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_1 = 0x31          # main-row '1'
KEYEVENTF_KEYUP = 0x0002

log = logging.getLogger("fan_auto")


def send_extreme_cooling_toggle() -> None:
    """Send Ctrl+Shift+1 — the Nerve Sense Extreme Cooling toggle hotkey."""
    _user32.keybd_event(VK_CONTROL, 0, 0, 0)
    _user32.keybd_event(VK_SHIFT, 0, 0, 0)
    _user32.keybd_event(VK_1, 0, 0, 0)
    time.sleep(0.05)
    _user32.keybd_event(VK_1, 0, KEYEVENTF_KEYUP, 0)
    _user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
    _user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def read_gpu_temp() -> int | None:
    """GPU temperature in C via nvidia-smi, or None if unavailable."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, creationflags=flags,
        )
        if out.returncode != 0:
            return None
        line = (out.stdout or "").strip().splitlines()
        return int(line[0].strip()) if line else None
    except Exception:
        log.exception("nvidia-smi read failed")
        return None


def _setup_logging() -> None:
    from logging.handlers import RotatingFileHandler
    log_dir = Path(__file__).resolve().parent / "history"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "fan_auto.log", maxBytes=256_000,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler(sys.stdout))


def main() -> None:
    p = argparse.ArgumentParser(description="Auto Extreme-Cooling by GPU temp")
    p.add_argument("--on", type=float, default=50.0,
                   help="Turn cooling ON at/above this temp (C). Default 50.")
    p.add_argument("--off", type=float, default=43.0,
                   help="Turn cooling OFF below this temp (C). Default 43.")
    p.add_argument("--interval", type=float, default=5.0,
                   help="Seconds between temperature checks. Default 5.")
    p.add_argument("--test", action="store_true",
                   help="Send ONE toggle and exit (watch Nerve Sense flip).")
    args = p.parse_args()

    _setup_logging()

    if args.test:
        log.info("TEST: sending one Ctrl+Shift+1 toggle now — watch Nerve Sense.")
        send_extreme_cooling_toggle()
        log.info("TEST: sent. Did the Extreme Cooling switch flip?")
        return

    if args.off >= args.on:
        p.error("--off must be lower than --on (hysteresis gap).")

    # Assume OFF at start — matches the app's state right after boot/sleep/restart.
    cooling_on = False
    log.info("Auto Extreme-Cooling started. ON>=%.0f C, OFF<%.0f C, every %.0fs. "
             "Assuming cooling currently OFF.", args.on, args.off, args.interval)

    try:
        while True:
            t = read_gpu_temp()
            if t is None:
                log.info("GPU temp unavailable; skipping this cycle.")
            else:
                if not cooling_on and t >= args.on:
                    send_extreme_cooling_toggle()
                    cooling_on = True
                    log.info("GPU %d C >= %.0f → Extreme Cooling ON", t, args.on)
                elif cooling_on and t < args.off:
                    send_extreme_cooling_toggle()
                    cooling_on = False
                    log.info("GPU %d C < %.0f → Extreme Cooling OFF", t, args.off)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Stopped by user. (Extreme Cooling left in its current state.)")


if __name__ == "__main__":
    main()
