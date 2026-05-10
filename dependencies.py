"""Pre-flight dependency checks for the performance monitor.

Each feature has its own required imports. Verifying upfront produces a clean
"install X" message instead of an opaque ImportError mid-run.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Requirement:
    feature: str          # CLI flag/feature name shown to the user
    module: str           # importlib name to probe
    pip_name: str         # pip install argument (often == module)
    note: str = ""        # optional extra hint


# Always required — if these are missing, monitor.py can't even import. Listed
# for completeness so users see a clear message even when a future refactor
# moves them behind lazy imports.
ALWAYS: tuple[Requirement, ...] = (
    Requirement("core", "psutil", "psutil"),
)

FEATURE_REQUIREMENTS: dict[str, tuple[Requirement, ...]] = {
    "dock": (
        Requirement("dock", "tkinter", "tk",
                    note="On Debian/Ubuntu install via apt: `sudo apt install python3-tk`."),
    ),
    "tray": (
        Requirement("tray", "pystray", "pystray"),
        Requirement("tray", "PIL", "Pillow"),
    ),
    "history": (
        Requirement("history", "matplotlib", "matplotlib",
                    note="Only needed for PNG chart rendering. CSV logging works without it."),
    ),
}


def _try_import(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


def check_features(enabled_features: set[str]) -> list[Requirement]:
    """Return the list of Requirements that are NOT satisfied for the given features.

    `enabled_features` is a subset of FEATURE_REQUIREMENTS keys (e.g. {"dock", "history"}).
    Always-required modules are checked unconditionally.
    """
    missing: list[Requirement] = []
    seen: set[tuple[str, str]] = set()

    def _check(req: Requirement) -> None:
        key = (req.feature, req.module)
        if key in seen:
            return
        seen.add(key)
        if not _try_import(req.module):
            missing.append(req)

    for req in ALWAYS:
        _check(req)
    for feat in enabled_features:
        for req in FEATURE_REQUIREMENTS.get(feat, ()):
            _check(req)
    return missing


def format_missing(missing: list[Requirement]) -> str:
    """Build a human-readable error message listing pip install commands."""
    if not missing:
        return ""
    by_feature: dict[str, list[Requirement]] = {}
    for req in missing:
        by_feature.setdefault(req.feature, []).append(req)

    lines = ["Missing dependencies:"]
    pip_names: list[str] = []
    for feature, reqs in by_feature.items():
        pkgs = ", ".join(r.pip_name for r in reqs)
        lines.append(f"  - {feature}: {pkgs}")
        pip_names.extend(r.pip_name for r in reqs if r.pip_name != "tk")
        for r in reqs:
            if r.note:
                lines.append(f"      {r.note}")
    if pip_names:
        lines.append("")
        lines.append(f"Install with:  pip install {' '.join(sorted(set(pip_names)))}")
    return "\n".join(lines)


def enabled_features_from_args(args) -> set[str]:
    """Map argparse Namespace → set of features to validate."""
    feats: set[str] = set()
    if getattr(args, "dock", False):
        feats.add("dock")
    if getattr(args, "tray", False):
        feats.add("tray")
    if getattr(args, "history", False):
        feats.add("history")
    return feats
