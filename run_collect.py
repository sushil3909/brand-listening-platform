"""Run one collection pass now. Usage: python run_collect.py"""
import asyncio
import sys

from app.collector import NoActiveAccount, run_all

if __name__ == "__main__":
    try:
        results = asyncio.run(run_all())
    except NoActiveAccount as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    total_new = sum(r["new"] for r in results)
    failed = [r["query"] for r in results if r["status"] != "ok"]
    print(f"Done. New posts: {total_new}. Failed queries: {failed or 'none'}")
    sys.exit(1 if failed else 0)
