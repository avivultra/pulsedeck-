import os
import time

import psutil


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def main() -> None:
    try:
        while True:
            cpu_usage = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory()
            ram_usage = ram.percent

            clear_screen()
            print("System Monitor")
            print("-" * 30)
            print(f"CPU Usage : {cpu_usage:6.2f}%")
            print(f"RAM Usage : {ram_usage:6.2f}%")
            print("-" * 30)
            print("Press Ctrl+C to stop.")

            time.sleep(1)
    except KeyboardInterrupt:
        print("\nMonitoring stopped.")


if __name__ == "__main__":
    main()
