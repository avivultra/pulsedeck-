"""
Rebuild history/metrics_chart.png from a CSV log (no live monitoring).

  python plot_history.py
  python plot_history.py --csv path/to/metrics.csv --png out.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

from metric_history import DEFAULT_CHART_PATH, DEFAULT_CSV_PATH, render_history_chart


def main() -> None:
    p = argparse.ArgumentParser(description="Render metrics CSV to a PNG chart.")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH, help="Input CSV path.")
    p.add_argument("--png", type=Path, default=DEFAULT_CHART_PATH, help="Output PNG path.")
    args = p.parse_args()
    ok = render_history_chart(args.csv, args.png)
    if ok:
        print(f"Chart ready: {args.png}")
    else:
        print("No PNG produced (CSV missing, empty, or fewer than 2 rows).")


if __name__ == "__main__":
    main()
