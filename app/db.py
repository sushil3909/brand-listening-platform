"""SQLite storage. One file, zero setup. All timestamps are UTC ISO-8601 strings."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
    id                TEXT PRIMARY KEY,
    username          TEXT NOT NULL,
    display_name      TEXT,
    description       TEXT,
    location          TEXT,
    followers_count   INTEGER DEFAULT 0,
    following_count   INTEGER DEFAULT 0,
    statuses_count    INTEGER DEFAULT 0,
    verified          INTEGER DEFAULT 0,
    blue              INTEGER DEFAULT 0,
    protected          INTEGER DEFAULT 0,
    profile_image_url TEXT,
    account_created_at TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authors_username ON authors(username);

CREATE TABLE IF NOT EXISTS posts (
    id                   TEXT PRIMARY KEY,
    author_id            TEXT NOT NULL,
    conversation_id      TEXT,
    created_at           TEXT NOT NULL,
    lang                 TEXT,
    text                 TEXT NOT NULL,
    url                  TEXT NOT NULL,
    in_reply_to_post_id  TEXT,
    in_reply_to_username TEXT,
    is_quote             INTEGER DEFAULT 0,
    quoted_post_id       TEXT,
    is_repost            INTEGER DEFAULT 0,
    reposted_post_id     TEXT,
    hashtags             TEXT DEFAULT '[]',
    mentions             TEXT DEFAULT '[]',
    links                TEXT DEFAULT '[]',
    has_media            INTEGER DEFAULT 0,
    is_own               INTEGER DEFAULT 0,
    source_query         TEXT,
    like_count           INTEGER DEFAULT 0,
    reply_count          INTEGER DEFAULT 0,
    repost_count         INTEGER DEFAULT 0,
    quote_count          INTEGER DEFAULT 0,
    bookmark_count       INTEGER DEFAULT 0,
    view_count           INTEGER,
    sentiment_label      TEXT,
    sentiment_score      REAL,
    sentiment_override   TEXT,
    risk_score           INTEGER DEFAULT 0,
    risk_level           TEXT DEFAULT 'low',
    tags                 TEXT DEFAULT '[]',
    summary              TEXT,
    handled              INTEGER DEFAULT 0,
    notes                TEXT,
    first_seen           TEXT NOT NULL,
    last_seen            TEXT NOT NULL,
    FOREIGN KEY (author_id) REFERENCES authors(id)
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_id);
CREATE INDEX IF NOT EXISTS idx_posts_risk ON posts(risk_level);
CREATE INDEX IF NOT EXISTS idx_posts_sentiment ON posts(sentiment_label);

CREATE TABLE IF NOT EXISTS engagement_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id        TEXT NOT NULL,
    captured_at    TEXT NOT NULL,
    like_count     INTEGER,
    reply_count    INTEGER,
    repost_count   INTEGER,
    quote_count    INTEGER,
    bookmark_count INTEGER,
    view_count     INTEGER,
    FOREIGN KEY (post_id) REFERENCES posts(id)
);
CREATE INDEX IF NOT EXISTS idx_snap_post ON engagement_snapshots(post_id, captured_at);

CREATE TABLE IF NOT EXISTS raw_posts (
    post_id     TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS missing_parents (
    post_id   TEXT PRIMARY KEY,
    tried_at  TEXT NOT NULL,
    error     TEXT
);

CREATE TABLE IF NOT EXISTS external_mentions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,            -- name of the external listening tool
    network      TEXT,                     -- twitter / instagram / reddit ...
    post_id      TEXT,                     -- X post id when we could parse one
    url          TEXT,
    created_at   TEXT,
    author       TEXT,
    text         TEXT,
    ext_sentiment TEXT,                    -- the other tool's own label, for comparison
    status       TEXT DEFAULT 'pending',   -- matched | fetched | unavailable | not_x
    imported_at  TEXT NOT NULL,
    batch        TEXT,
    competitor   TEXT,                     -- which brand the row was matched to
    likes        INTEGER,
    comments     INTEGER,
    shares       INTEGER,
    impressions  INTEGER,                  -- the tool's "potential impressions" = estimated reach, not real views
    hashtags     TEXT,
    language     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ext_unique ON external_mentions(source, COALESCE(post_id, url));
CREATE INDEX IF NOT EXISTS idx_ext_status ON external_mentions(source, status);

CREATE TABLE IF NOT EXISTS backfill_windows (
    query_name   TEXT NOT NULL,
    since        TEXT NOT NULL,
    until        TEXT NOT NULL,
    status       TEXT DEFAULT 'pending',
    posts_found  INTEGER DEFAULT 0,
    posts_new    INTEGER DEFAULT 0,
    finished_at  TEXT,
    error        TEXT,
    PRIMARY KEY (query_name, since, until)
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    query_name   TEXT NOT NULL,
    query        TEXT NOT NULL,
    posts_found  INTEGER DEFAULT 0,
    posts_new    INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'running',
    error        TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


MIGRATIONS = [
    # (column, definition) added to posts if missing
    ("is_context", "INTEGER DEFAULT 0"),   # 1 = fetched only as the parent of a reply; excluded from stats
    ("relevance", "TEXT DEFAULT 'explicit'"),  # explicit = names the brand itself; reaction = only the auto @tag
]


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)").fetchall()}
        for col, ddl in MIGRATIONS:
            if col not in cols:
                conn.execute(f"ALTER TABLE posts ADD COLUMN {col} {ddl}")
        ecols = {r["name"] for r in conn.execute("PRAGMA table_info(external_mentions)").fetchall()}
        for col, ddl in [("competitor", "TEXT"), ("likes", "INTEGER"), ("comments", "INTEGER"),
                         ("shares", "INTEGER"), ("impressions", "INTEGER"),
                         ("hashtags", "TEXT"), ("language", "TEXT")]:
            if ecols and col not in ecols:
                conn.execute(f"ALTER TABLE external_mentions ADD COLUMN {col} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_reply ON posts(in_reply_to_post_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_context ON posts(is_context)")


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = dict(r)
        for key in ("hashtags", "mentions", "links", "tags"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except json.JSONDecodeError:
                    d[key] = []
        out.append(d)
    return out
