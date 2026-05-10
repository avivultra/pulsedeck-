"""Configuration loader/saver for the performance monitor.

Stores user preferences in `config.json` next to monitor.py. CLI flags
override values from the file; --save-config writes the merged result back.
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_FILENAME = "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "ui": {"dock": False, "tray": False, "history": False, "console": True},
    "disk_path": None,
    "history_dir": "history",
    "spike": {"cpu_threshold": 12, "ram_threshold": 6, "enabled": True},
    "rotation": {"enabled": True, "weeks_to_keep": 12},
    "log_level": "WARNING",
    "alerts": {
        "enabled": True,
        "cooldown_seconds": 300,
        "top_n": 5,
        "confirm_kill": True,
    },
    "dock": {
        "x": None,           # None = auto-place; otherwise pixel X
        "y": None,           # None = auto-place; otherwise pixel Y
        "font_scale": 1.0,   # 0.7 .. 1.6 — multiplier on font size
        "pinned": True,      # always-on-top: re-asserted every tick when pinned
    },
    "janitor": {
        "enabled": True,
        "scan_interval_minutes": 5,
        "conhost_threshold_per_parent": 20,
        "suspicious_parents": [
            "claude.exe", "electron.exe", "node.exe",
            "python.exe", "pythonw.exe", "code.exe",
        ],
    },
}


def config_path(base_dir: Path | None = None) -> Path:
    return (base_dir or PROJECT_DIR) / CONFIG_FILENAME


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Merge `overrides` into a copy of `base` recursively (dicts only)."""
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(base_dir: Path | None = None) -> dict[str, Any]:
    """Load config.json, creating it with defaults if missing.

    Missing keys in an existing file are filled in from DEFAULT_CONFIG so
    partial files remain valid as new options are added.
    """
    path = config_path(base_dir)
    if not path.exists():
        log.info("config.json not found; creating defaults at %s", path)
        try:
            save_config(DEFAULT_CONFIG, base_dir)
        except OSError:
            log.exception("Could not create default config; using in-memory defaults")
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        with path.open("r", encoding="utf-8") as f:
            user_config = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to read %s (%s); using defaults", path, exc)
        return copy.deepcopy(DEFAULT_CONFIG)

    if not isinstance(user_config, dict):
        log.warning("%s is not a JSON object; using defaults", path)
        return copy.deepcopy(DEFAULT_CONFIG)

    return _deep_merge(DEFAULT_CONFIG, user_config)


def save_config(cfg: dict[str, Any], base_dir: Path | None = None) -> None:
    """Atomically write `cfg` to config.json (write to .tmp then replace)."""
    path = config_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        tmp.replace(path)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                log.exception("Could not clean up temp config file %s", tmp)
        raise
