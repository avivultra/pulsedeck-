"""
Rebuild history/metrics_chart.png from a CSV log (no live monitoring).

  python plot_history.py
  python plot_history.py --csv path/to/metrics.csv --png out.png
  python plot_history.py --include-archive   # merge all weekly archives too
"""

from __future__ import annotations

import argparse
from pathlib import Path

from metric_history import (
    DEFAULT_CHART_PATH,
    DEFAULT_CSV_PATH,
    DEFAULT_HISTORY_DIR,
    render_combined_chart,
    render_history_chart,
)


def main() -> None:
    p = argparse.ArgumentParser(description="Render metrics CSV to a PNG chart.")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH, help="Input CSV path.")
    p.add_argument("--png", type=Path, default=DEFAULT_CHART_PATH, help="Output PNG path.")
    p.add_argument(
        "--include-archive",
        action="store_true",
        help="Combine main CSV with all metrics-YYYY-WW.csv archives in the history dir.",
    )
    p.add_argument(
        "--history-dir",
        type=Path,
        default=DEFAULT_HISTORY_DIR,
        help="History directory (used with --include-archive).",
    )
    args = p.parse_args()

    if args.include_archive:
        ok = render_combined_chart(args.history_dir, args.png, include_archive=True)
    else:
        ok = render_history_chart(args.csv, args.png)

    if ok:
        print(f"Chart ready: {args.png}")
    else:
        print("No PNG produced (CSV missing, empty, or fewer than 2 rows).")


if __name__ == "__main__":
    main()
