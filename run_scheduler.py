"""Keep collecting on a schedule (interval from config.yaml). Usage: python run_scheduler.py

Leave this window open, or run it as a Windows scheduled task at logon.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

from app.collector import NoActiveAccount, run_all
from app.config import load_config
from app.single_instance import ensure_single_instance


def main() -> None:
    ensure_single_instance("The collector", 8765)
    while True:
        interval = int(load_config()["collection"]["interval_minutes"])
        print(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} collection start ===")
        try:
            asyncio.run(run_all())
        except NoActiveAccount as e:
            print(f"ERROR: {e}")
        except Exception as e:  # never let the loop die
            print(f"ERROR: {e.__class__.__name__}: {e}")
        print(f"=== next run in {interval} min ===")
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
