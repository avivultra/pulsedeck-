import argparse
import os
import time

import psutil


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def disk_root_path() -> str:
    if os.name == "nt":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def main() -> None:
    parser = argparse.ArgumentParser(description="Live CPU, RAM, and disk usage in the terminal.")
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        metavar="SEC",
        help="Seconds between screen updates (default: 1.0).",
    )
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive.")

    # First cpu_percent() call is meaningless without a prior sample; prime once.
    psutil.cpu_percent(interval=0.1)
    disk_path = disk_root_path()

    try:
        while True:
            cpu_usage = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory()
            ram_usage = ram.percent
            disk = psutil.disk_usage(disk_path)
            disk_pct = 100.0 * disk.used / disk.total

            clear_screen()
            print("System Monitor")
            print("-" * 34)
            print(f"CPU Usage : {cpu_usage:6.2f}%")
            print(f"RAM Usage : {ram_usage:6.2f}%")
            print(f"Disk ({disk_path}) : {disk_pct:6.2f}% used")
            print("-" * 34)
            print("Press Ctrl+C to stop.")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nMonitoring stopped.")


if __name__ == "__main__":
    main()
