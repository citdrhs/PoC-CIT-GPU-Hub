"""Run the single-host scheduler."""
import argparse
import os
import time

from services.scheduler.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("GPUHUB_DB", ".gpuhub/hub.sqlite3"))
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    store = Store(args.db)
    try:
        while True:
            store.tick()
            if args.once:
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
