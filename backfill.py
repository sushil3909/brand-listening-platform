"""One-off historical backfill: walk back in weekly windows and store everything found.

Usage:
    python backfill.py               # back to 12 months ago (default)
    python backfill.py --months 6    # back to 6 months ago
    python backfill.py --since 2026-08-18 --until 2026-09-11   # fill one specific gap
    python backfill.py --status      # show progress, do nothing

The live collector always has priority: this script pauses whenever a live run is in progress.

Resumable: finished windows are recorded in the database and skipped on re-run.
Gentle on the account: one window at a time, a pause between windows, and it waits
out X's rate limits instead of retrying.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from datetime import date, datetime, timedelta, timezone

from twscrape import API

from app import db
from app.collector import NoActiveAccount, _process_tweet, active_accounts
from app.config import ACCOUNTS_DB_PATH, COLLECTOR_FLAG, load_config

WINDOW_DAYS = 7
PAUSE_SECONDS = 20
PER_WINDOW_LIMIT = 3000
YIELD_EVERY = 20          # posts between checks for a live collector run
STALE_FLAG_SECONDS = 1200  # a flag older than this is a crash leftover, ignore it


class CollectorWantsAccount(Exception):
    pass


def collector_running() -> bool:
    try:
        ts = datetime.fromisoformat(COLLECTOR_FLAG.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return False
    return (datetime.now(timezone.utc) - ts).total_seconds() < STALE_FLAG_SECONDS


async def wait_for_collector() -> None:
    waited = False
    while collector_running():
        if not waited:
            print("   (live collector is running, waiting for it to finish)", flush=True)
            waited = True
        await asyncio.sleep(15)


def _windows(start: date, end: date) -> list[tuple[str, str]]:
    """Weekly (since, until) pairs from newest to oldest. X 'until' is exclusive."""
    out = []
    hi = end
    while hi > start:
        lo = max(start, hi - timedelta(days=WINDOW_DAYS))
        out.append((lo.isoformat(), hi.isoformat()))
        hi = lo
    return out


def _coverage_start(conn) -> date:
    """Oldest date the regular collector already covered (its first-run lookback)."""
    row = conn.execute("SELECT MIN(started_at) FROM collection_runs WHERE query_name != 'parent'").fetchone()
    cfg = load_config()["collection"]
    if row and row[0]:
        first = datetime.fromisoformat(row[0]).date()
        return first - timedelta(days=int(cfg["first_run_lookback_days"]))
    return date.today() - timedelta(days=int(cfg["first_run_lookback_days"]))


def show_status() -> None:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT query_name, status, COUNT(*) n, SUM(posts_found) f, SUM(posts_new) nw, MIN(since) oldest
               FROM backfill_windows GROUP BY query_name, status ORDER BY query_name, status"""
        ).fetchall()
        if not rows:
            print("No backfill has been run yet.")
            return
        for r in rows:
            print(f"  {r['query_name']:16} {r['status']:8} windows={r['n']:<4} found={r['f'] or 0:<6} new={r['nw'] or 0:<6} oldest={r['oldest']}")
        oldest = conn.execute("SELECT MIN(created_at) FROM posts WHERE is_context = 0").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM posts WHERE is_context = 0").fetchone()[0]
        print(f"\n  database: {total} posts, oldest {oldest}")


async def run(months: int, since: str | None = None, until: str | None = None) -> int:
    db.init_db()
    cfg = load_config()
    if not await active_accounts():
        raise NoActiveAccount("No active X account. Run setup_account.py first.")

    if since or until:
        # explicit range, e.g. to fill a known gap
        start = date.fromisoformat(since) if since else date.today() - timedelta(days=int(round(months * 30.44)))
        end = date.fromisoformat(until) if until else date.today()
    else:
        with db.connect() as conn:
            end = _coverage_start(conn)
        start = date.today() - timedelta(days=int(round(months * 30.44)))
    if start >= end:
        print(f"Nothing to do: range {start} .. {end} is empty.")
        return 0

    windows = _windows(start, end)
    plan = [(q["name"], q["query"], s, u) for q in cfg["queries"] for (s, u) in windows]
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO backfill_windows (query_name, since, until) VALUES (?,?,?)",
            [(n, s, u) for n, _, s, u in plan],
        )
        done = {(r["query_name"], r["since"], r["until"]) for r in conn.execute(
            "SELECT query_name, since, until FROM backfill_windows WHERE status = 'ok'")}
    todo = [p for p in plan if (p[0], p[2], p[3]) not in done]
    print(f"Backfill {start} -> {end} in {WINDOW_DAYS}-day windows: {len(plan)} windows total, {len(todo)} to do.")
    print("This can take a while. X rate limits are waited out automatically. Ctrl+C to stop; re-run to resume.\n")

    # wait_timeout=None: block until the account's rate limit resets rather than fail
    api = API(str(ACCOUNTS_DB_PATH), raise_when_no_account=False, wait_timeout=None)
    total_new = 0
    i = 0
    while todo:
        name, base_query, since, until = todo[0]
        i += 1
        q = f"{base_query} since:{since} until:{until}"
        stats = {"found": 0, "new": 0, "updated": 0}
        await wait_for_collector()
        started = datetime.now(timezone.utc)
        print(f"[{i}] {name} {since} .. {until} ({len(todo)} windows left)", end=" ", flush=True)
        try:
            async for tweet in api.search(q, limit=PER_WINDOW_LIMIT):
                stats["found"] += 1
                with db.connect() as conn:
                    _process_tweet(conn, tweet, name, cfg, db.utcnow(), stats)
                if stats["found"] % YIELD_EVERY == 0 and collector_running():
                    raise CollectorWantsAccount()
            status, err = "ok", None
        except CollectorWantsAccount:
            # posts already saved are kept; the window is redone later so nothing is missed
            print(f"-> paused after {stats['found']} posts, live collector needs the account")
            await asyncio.sleep(5)
            continue
        except KeyboardInterrupt:
            print("\nStopped. Re-run backfill.py to resume.")
            return 130
        except Exception as e:
            status, err = "error", f"{e.__class__.__name__}: {e}\n{traceback.format_exc()[-800:]}"
        todo.pop(0)
        with db.connect() as conn:
            conn.execute(
                """UPDATE backfill_windows SET status=?, posts_found=?, posts_new=?, finished_at=?, error=?
                   WHERE query_name=? AND since=? AND until=?""",
                (status, stats["found"], stats["new"], db.utcnow(), err, name, since, until),
            )
            conn.execute(
                """INSERT INTO collection_runs (started_at, finished_at, query_name, query, posts_found, posts_new, status, error)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (started.replace(microsecond=0).isoformat(), db.utcnow(), f"backfill:{name}", q,
                 stats["found"], stats["new"], status, err),
            )
        total_new += stats["new"]
        secs = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"-> found={stats['found']} new={stats['new']} ({secs:.0f}s) {status if status != 'ok' else ''}")
        if status == "error":
            print("   " + (err or "").splitlines()[0])
        if todo:
            await asyncio.sleep(PAUSE_SECONDS)

    print(f"\nDone. {total_new} new posts added.")
    show_status()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=12, help="how far back to go (default 12)")
    ap.add_argument("--since", help="explicit start date YYYY-MM-DD (fill a specific gap)")
    ap.add_argument("--until", help="explicit end date YYYY-MM-DD, exclusive")
    ap.add_argument("--status", action="store_true", help="show progress only")
    a = ap.parse_args()
    if a.status:
        db.init_db()
        show_status()
        return 0
    try:
        return asyncio.run(run(a.months, a.since, a.until))
    except NoActiveAccount as e:
        print(f"ERROR: {e}")
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Re-run backfill.py to resume.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
