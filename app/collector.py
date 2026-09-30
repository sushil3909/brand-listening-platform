"""Collects posts from X via twscrape (logged-in GraphQL) and stores them in SQLite.

Design:
  * One search per configured query, newest first (X "Latest" tab).
  * Incremental: each run re-scans the last `refresh_window_days` so engagement
    counts on recent posts get refreshed for free; the first run goes back
    `first_run_lookback_days`.
  * Replies whose parent post we do not have get the parent fetched once and
    stored as "context only" (is_context=1) so the thread can be shown without
    the parent polluting the statistics.
  * Every post keeps its raw JSON so we can re-process later.
  * Engagement snapshots are appended only when counts actually change.
"""
from __future__ import annotations

import asyncio
import json
import re
import traceback
from datetime import datetime, timedelta, timezone

from twscrape import API
from twscrape.models import Tweet

from . import db
from .config import ACCOUNTS_DB_PATH, COLLECTOR_FLAG, load_config
from .scoring import product_tags, risk_score
from .sentiment import analyze as analyze_sentiment


class NoActiveAccount(RuntimeError):
    pass


_LEADING_MENTIONS = re.compile(r"^(?:\s*@\w+)+\s*")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def relevance_of(text: str, cfg: dict) -> str:
    """'explicit' if the text names the brand itself; 'reaction' if the only brand
    reference is the @tag X adds automatically at the start of a reply."""
    body = _LEADING_MENTIONS.sub("", text or "").lower()
    for kw in cfg["brand"].get("keywords", []):
        if kw and kw.lower() in body:
            return "explicit"
    return "reaction"


async def active_accounts() -> list[str]:
    api = API(str(ACCOUNTS_DB_PATH))
    accs = await api.pool.get_all()
    return [a.username for a in accs if a.active]


def _since_date(conn, query_name: str, cfg: dict) -> str:
    col = cfg["collection"]
    row = conn.execute(
        "SELECT MAX(created_at) AS m FROM posts WHERE source_query = ?", (query_name,)
    ).fetchone()
    now = datetime.now(timezone.utc)
    if row and row["m"]:
        last = datetime.fromisoformat(row["m"])
        since = last - timedelta(days=int(col["refresh_window_days"]))
    else:
        since = now - timedelta(days=int(col["first_run_lookback_days"]))
    return since.strftime("%Y-%m-%d")


def _upsert_author(conn, u, now: str) -> None:
    conn.execute(
        """
        INSERT INTO authors (id, username, display_name, description, location,
            followers_count, following_count, statuses_count, verified, blue, protected,
            profile_image_url, account_created_at, first_seen, last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            username=excluded.username, display_name=excluded.display_name,
            description=excluded.description, location=excluded.location,
            followers_count=excluded.followers_count, following_count=excluded.following_count,
            statuses_count=excluded.statuses_count, verified=excluded.verified, blue=excluded.blue,
            protected=excluded.protected, profile_image_url=excluded.profile_image_url,
            last_seen=excluded.last_seen
        """,
        (
            u.id_str, u.username, u.displayname, u.rawDescription, u.location,
            u.followersCount, u.friendsCount, u.statusesCount,
            int(bool(u.verified)), int(bool(u.blue)), int(bool(u.protected)),
            u.profileImageUrl, _iso(u.created) if u.created else None, now, now,
        ),
    )


def _process_tweet(conn, t: Tweet, query_name: str, cfg: dict, now: str, stats: dict,
                   is_context: bool = False) -> None:
    own = {a.lower() for a in cfg["brand"].get("own_accounts", [])}
    is_own = t.user.username.lower() in own
    relevance = "explicit" if is_own else relevance_of(t.rawContent, cfg)
    if is_context and relevance == "explicit":
        is_context = False  # the parent talks about us itself: it is a real post, not just context

    _upsert_author(conn, t.user, now)

    existing = conn.execute("SELECT * FROM posts WHERE id = ?", (t.id_str,)).fetchone()
    engagement = t.likeCount + t.replyCount + t.retweetCount + t.quoteCount
    has_media = int(bool(t.media and (t.media.photos or t.media.videos or t.media.animated)))

    if existing is None:
        label, score = analyze_sentiment(t.rawContent)
        tags = product_tags(t.rawContent, cfg)
        rscore, rlevel, risk_hits = risk_score(
            t.rawContent, label, score, engagement, t.user.followersCount, is_own, cfg
        )
        conn.execute(
            """
            INSERT INTO posts (id, author_id, conversation_id, created_at, lang, text, url,
                in_reply_to_post_id, in_reply_to_username, is_quote, quoted_post_id,
                is_repost, reposted_post_id, hashtags, mentions, links, has_media, is_own,
                source_query, like_count, reply_count, repost_count, quote_count,
                bookmark_count, view_count, sentiment_label, sentiment_score,
                risk_score, risk_level, tags, first_seen, last_seen, is_context, relevance)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                t.id_str, t.user.id_str, t.conversationIdStr, _iso(t.date), t.lang, t.rawContent, t.url,
                t.inReplyToTweetIdStr, t.inReplyToScreenName, int(t.isQuoteStatus),
                t.quotedTweet.id_str if t.quotedTweet else None,
                int(t.retweetedTweet is not None),
                t.retweetedTweet.id_str if t.retweetedTweet else None,
                json.dumps(t.hashtags), json.dumps([m.username for m in t.mentionedUsers]),
                json.dumps([l.url for l in t.links]), has_media, int(is_own),
                query_name, t.likeCount, t.replyCount, t.retweetCount, t.quoteCount,
                t.bookmarkedCount, t.viewCount, label, score,
                rscore, rlevel, json.dumps(sorted(set(tags + [f"risk:{k}" for k in risk_hits]))),
                now, now, int(is_context), relevance,
            ),
        )
        conn.execute(
            "INSERT INTO raw_posts (post_id, captured_at, payload) VALUES (?,?,?)",
            (t.id_str, now, t.json()),
        )
        _snapshot(conn, t, now)
        stats["new"] += 1
    else:
        changed = (
            existing["like_count"] != t.likeCount
            or existing["reply_count"] != t.replyCount
            or existing["repost_count"] != t.retweetCount
            or existing["quote_count"] != t.quoteCount
            or existing["bookmark_count"] != t.bookmarkedCount
            or (existing["view_count"] or 0) != (t.viewCount or 0)
        )
        # keep the existing sentiment (may have a manual override); re-score risk with new engagement
        label = existing["sentiment_override"] or existing["sentiment_label"]
        rscore, rlevel, _ = risk_score(
            t.rawContent, label, existing["sentiment_score"], engagement,
            t.user.followersCount, is_own, cfg,
        )
        # a post first seen as "context" that now matches a search becomes a real post
        new_context = int(is_context) if existing["is_context"] else 0
        conn.execute(
            """
            UPDATE posts SET like_count=?, reply_count=?, repost_count=?, quote_count=?,
                bookmark_count=?, view_count=?, risk_score=?, risk_level=?, last_seen=?,
                is_context=?, relevance=?
            WHERE id=?
            """,
            (
                t.likeCount, t.replyCount, t.retweetCount, t.quoteCount, t.bookmarkedCount,
                t.viewCount, rscore, rlevel, now, new_context, relevance, t.id_str,
            ),
        )
        if changed:
            _snapshot(conn, t, now)
            stats["updated"] += 1

    # A repost/quote of a post that mentions us: store the original too.
    for inner in (t.retweetedTweet, t.quotedTweet):
        if inner is not None and inner.id_str != t.id_str:
            _process_tweet(conn, inner, query_name, cfg, now, stats, is_context=is_context)


def _snapshot(conn, t: Tweet, now: str) -> None:
    conn.execute(
        """
        INSERT INTO engagement_snapshots (post_id, captured_at, like_count, reply_count,
            repost_count, quote_count, bookmark_count, view_count)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (t.id_str, now, t.likeCount, t.replyCount, t.retweetCount, t.quoteCount,
         t.bookmarkedCount, t.viewCount),
    )


async def run_query(api: API, q: dict, cfg: dict) -> dict:
    name, base_query = q["name"], q["query"]
    limit = int(cfg["collection"]["max_posts_per_query"])
    stats = {"found": 0, "new": 0, "updated": 0}

    with db.connect() as conn:
        since = _since_date(conn, name, cfg)
        started = db.utcnow()
        cur = conn.execute(
            "INSERT INTO collection_runs (started_at, query_name, query) VALUES (?,?,?)",
            (started, name, base_query),
        )
        run_id = cur.lastrowid

    print(f"[collector] {name}: {base_query} since:{since} (limit {limit} per page)")

    # X returns newest first. If a page fills up (long outage, busy day) we page
    # backwards with `until:` until we reach the since date, so no day is skipped.
    since_day = datetime.strptime(since, "%Y-%m-%d").date()
    until: str | None = None
    try:
        async with asyncio.timeout(1500):
            for page in range(1, 12):
                q = f"{base_query} since:{since}" + (f" until:{until}" if until else "")
                got, oldest = 0, None
                async for tweet in api.search(q, limit=limit):
                    got += 1
                    stats["found"] += 1
                    d = tweet.date.date()
                    oldest = d if oldest is None or d < oldest else oldest
                    with db.connect() as conn:
                        _process_tweet(conn, tweet, name, cfg, db.utcnow(), stats)
                if got < limit or oldest is None or oldest <= since_day:
                    break
                next_until = (oldest + timedelta(days=1)).isoformat()
                if next_until == until:
                    break
                until = next_until
                print(f"[collector] {name}: page {page} full ({got}), continuing back from {until}")
        status, error = "ok", None
    except Exception as e:  # keep going with other queries; record the failure
        status, error = "error", f"{e.__class__.__name__}: {e}\n{traceback.format_exc()[-1500:]}"
        print(f"[collector] {name}: FAILED {e.__class__.__name__}: {e}")

    with db.connect() as conn:
        conn.execute(
            """UPDATE collection_runs SET finished_at=?, posts_found=?, posts_new=?, status=?, error=?
               WHERE id=?""",
            (db.utcnow(), stats["found"], stats["new"], status, error, run_id),
        )
    print(f"[collector] {name}: found={stats['found']} new={stats['new']} updated={stats['updated']} status={status}")
    return {"query": name, "status": status, **stats}


async def crawl_own_threads(api: API, cfg: dict) -> dict:
    """Search misses ~40% of replies to our posts (X hides low-reputation accounts from
    search). So for our own recent posts, read the conversation directly and fetch
    quotes with a dedicated query. Posts are re-crawled while X's counts exceed ours."""
    col = cfg["collection"]
    days = int(col.get("thread_crawl_days", 7))
    per_run = int(col.get("thread_crawl_posts_per_run", 10))
    stats = {"posts": 0, "found": 0, "new": 0, "updated": 0}
    if per_run <= 0:
        return stats
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT p.id, p.reply_count, p.quote_count,
                      (SELECT COUNT(*) FROM posts r WHERE r.in_reply_to_post_id = p.id) AS have_replies,
                      (SELECT COUNT(*) FROM posts q WHERE q.quoted_post_id = p.id) AS have_quotes
               FROM posts p
               WHERE p.is_own = 1 AND p.created_at >= ? AND (p.reply_count > 0 OR p.quote_count > 0)
               ORDER BY (p.reply_count + p.quote_count) DESC""",
            (since,),
        ).fetchall()
    todo = [r for r in rows if r["reply_count"] > r["have_replies"] or r["quote_count"] > r["have_quotes"]]
    todo.sort(key=lambda r: (r["reply_count"] - r["have_replies"]) + (r["quote_count"] - r["have_quotes"]), reverse=True)
    todo = todo[:per_run]
    if not todo:
        return stats
    print(f"[collector] threads: reading conversations of {len(todo)} of our posts")
    for r in todo:
        stats["posts"] += 1
        try:
            if r["reply_count"] > r["have_replies"]:
                async for t in api.tweet_replies(int(r["id"]), limit=400):
                    stats["found"] += 1
                    with db.connect() as conn:
                        _process_tweet(conn, t, "thread", cfg, db.utcnow(), stats)
            if r["quote_count"] > r["have_quotes"]:
                async for t in api.search(f"quoted_tweet_id:{r['id']}", limit=400):
                    stats["found"] += 1
                    with db.connect() as conn:
                        _process_tweet(conn, t, "quotes", cfg, db.utcnow(), stats)
        except Exception as e:
            print(f"[collector] threads: post {r['id']} failed {e.__class__.__name__}: {e}")
    print(f"[collector] threads: posts={stats['posts']} found={stats['found']} new={stats['new']}")
    return stats


async def fetch_missing_parents(api: API, cfg: dict) -> dict:
    """For replies whose parent we don't have, fetch the parent once (context only)."""
    limit = int(cfg["collection"].get("fetch_parents_per_run", 25))
    stats = {"found": 0, "new": 0, "updated": 0, "failed": 0}
    if limit <= 0:
        return stats
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT p.in_reply_to_post_id AS pid
               FROM posts p
               WHERE p.in_reply_to_post_id IS NOT NULL AND p.is_context = 0
                 AND NOT EXISTS (SELECT 1 FROM posts x WHERE x.id = p.in_reply_to_post_id)
                 AND NOT EXISTS (SELECT 1 FROM missing_parents m WHERE m.post_id = p.in_reply_to_post_id)
               ORDER BY p.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    ids = [r["pid"] for r in rows]
    if not ids:
        return stats
    print(f"[collector] parents: fetching {len(ids)} missing parent posts")
    for pid in ids:
        now = db.utcnow()
        try:
            tweet = await api.tweet_details(int(pid))
        except Exception as e:
            tweet, err = None, f"{e.__class__.__name__}: {e}"
        else:
            err = None if tweet else "not available (deleted, private or blocked)"
        with db.connect() as conn:
            if tweet is None:
                conn.execute("INSERT OR REPLACE INTO missing_parents (post_id, tried_at, error) VALUES (?,?,?)",
                             (pid, now, err))
                stats["failed"] += 1
            else:
                stats["found"] += 1
                _process_tweet(conn, tweet, "parent", cfg, now, stats, is_context=True)
    print(f"[collector] parents: found={stats['found']} new={stats['new']} unavailable={stats['failed']}")
    return stats


async def run_all() -> list[dict]:
    db.init_db()
    cfg = load_config()
    accounts = await active_accounts()
    if not accounts:
        raise NoActiveAccount(
            "No active X account in data/accounts.db. Run: python setup_account.py"
        )
    print(f"[collector] active accounts: {', '.join(accounts)}")
    # The live collector has priority over the backfill: the flag file tells the
    # backfill to pause, and we wait up to 10 minutes for X's rate limit to reset
    # rather than giving up after 2.
    api = API(str(ACCOUNTS_DB_PATH), raise_when_no_account=True, wait_timeout=600)
    results = []
    try:
        COLLECTOR_FLAG.write_text(db.utcnow(), encoding="utf-8")
        for q in cfg["queries"]:
            results.append(await run_query(api, q, cfg))
        for step_name, step in (("threads", crawl_own_threads), ("parents", fetch_missing_parents)):
            try:
                await step(api, cfg)
            except Exception as e:
                print(f"[collector] {step_name}: FAILED {e.__class__.__name__}: {e}")
    finally:
        try:
            COLLECTOR_FLAG.unlink()
        except FileNotFoundError:
            pass
    return results


if __name__ == "__main__":
    asyncio.run(run_all())
