"""FastAPI dashboard + JSON API over the SQLite store."""
from __future__ import annotations

import base64
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import db
from .config import ACCOUNTS_DB_PATH, env, load_config

app = FastAPI(title="Brand Listening", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PASSWORD = env("DASHBOARD_PASSWORD") or ""          # full access (override sentiment, mark handled)
VIEWER_PASSWORD = env("VIEWER_PASSWORD") or ""      # read-only access for testers / colleagues


def _role_for(pwd: str) -> str | None:
    if PASSWORD and secrets.compare_digest(pwd, PASSWORD):
        return "admin"
    if VIEWER_PASSWORD and secrets.compare_digest(pwd, VIEWER_PASSWORD):
        return "viewer"
    return None


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Shared passwords (any username). If DASHBOARD_PASSWORD is empty, auth is off (admin)."""
    role = "admin" if not PASSWORD else None
    if role is None:
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                role = _role_for(pwd)
            except Exception:
                role = None
        if role is None:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="listening"'})
    request.state.role = role
    if request.method != "GET" and role != "admin":
        return JSONResponse({"detail": "read-only access"}, status_code=403)
    return await call_next(request)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


# --- SQL fragments -------------------------------------------------------------
SENT = "COALESCE(p.sentiment_override, p.sentiment_label)"
NEG_STRONG = f"({SENT} = 'negative' AND p.sentiment_score <= -0.6)"
# the model said negative, but weakly and with no risk keyword: a human should look
REVIEW = f"({SENT} = 'negative' AND p.sentiment_override IS NULL AND p.sentiment_score > -0.6 AND p.tags NOT LIKE '%risk:%')"
# a reply worth showing without clicking "show replies"
IMPORTANT = f"(p.risk_level != 'low' OR {NEG_STRONG} OR p.like_count >= 5 OR COALESCE(p.view_count,0) >= 1000 OR p.relevance = 'explicit')"
POST_COLS = f"""p.*, {SENT} AS sentiment, {REVIEW} AS needs_review, {IMPORTANT} AS important,
               a.username, a.display_name, a.followers_count, a.blue, a.verified, a.profile_image_url"""


@app.get("/api/me")
def me(request: Request):
    return {"role": request.state.role}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    cfg = load_config()
    return templates.TemplateResponse(request, "index.html", {"brand": cfg["brand"]["handle"]})


@app.get("/api/overview")
def overview(days: int = Query(7, ge=1, le=3650), include_own: bool = False):
    since = _since(days)
    prev_since = _since(days * 2)
    own_clause = "AND p.is_context = 0" + ("" if include_own else " AND p.is_own = 0")

    with db.connect() as conn:
        # how much of the requested range actually has data (drives the chart bucket size)
        oldest = conn.execute(
            f"SELECT MIN(p.created_at) FROM posts p WHERE p.created_at >= ? {own_clause}", (since,)
        ).fetchone()[0]
        span_days = days
        if oldest:
            span_days = max(1, (datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).days + 1)
        span_days = min(days, span_days)
        def totals(a: str, b: str | None) -> dict:
            where = f"p.created_at >= ? {own_clause}" + (" AND p.created_at < ?" if b else "")
            params = (a, b) if b else (a,)
            r = conn.execute(
                f"""SELECT COUNT(*) AS posts,
                       SUM(CASE WHEN p.in_reply_to_post_id IS NOT NULL THEN 1 ELSE 0 END) AS replies,
                       SUM(CASE WHEN {SENT}='negative' THEN 1 ELSE 0 END) AS negative,
                       SUM(CASE WHEN {SENT}='positive' THEN 1 ELSE 0 END) AS positive,
                       SUM(CASE WHEN {SENT}='neutral' THEN 1 ELSE 0 END) AS neutral,
                       SUM(CASE WHEN p.risk_level='high' THEN 1 ELSE 0 END) AS high_risk,
                       SUM(CASE WHEN p.risk_level='medium' THEN 1 ELSE 0 END) AS medium_risk,
                       SUM(CASE WHEN {REVIEW} THEN 1 ELSE 0 END) AS to_review,
                       SUM(p.like_count + p.reply_count + p.repost_count + p.quote_count) AS engagement,
                       SUM(COALESCE(p.view_count,0)) AS views,
                       COUNT(DISTINCT p.author_id) AS authors,
                       AVG(p.sentiment_score) AS avg_sentiment
                    FROM posts p WHERE {where}""",
                params,
            ).fetchone()
            return {k: (r[k] or 0) for k in r.keys()}

        current = totals(since, None)
        previous = totals(prev_since, since)

        # daily up to 60 days of data, weekly (Monday-start) up to ~1 year, monthly beyond
        if span_days <= 60:
            bucket = "substr(p.created_at,1,10)"
        elif span_days <= 400:
            bucket = "date(substr(p.created_at,1,10), '-' || ((strftime('%w', substr(p.created_at,1,10)) + 6) % 7) || ' days')"
        else:
            bucket = "substr(p.created_at,1,7) || '-01'"
        daily = db.rows_to_dicts(conn.execute(
            f"""SELECT {bucket} AS day, COUNT(*) AS posts,
                   SUM(CASE WHEN {SENT}='negative' THEN 1 ELSE 0 END) AS negative,
                   SUM(CASE WHEN {SENT}='positive' THEN 1 ELSE 0 END) AS positive,
                   SUM(CASE WHEN {SENT}='neutral' THEN 1 ELSE 0 END) AS neutral,
                   SUM(CASE WHEN p.risk_level='high' THEN 1 ELSE 0 END) AS high_risk,
                   AVG(p.sentiment_score) AS avg_sentiment,
                   SUM(COALESCE(p.view_count,0)) AS views,
                   SUM(p.like_count + p.reply_count + p.repost_count + p.quote_count) AS engagement
                FROM posts p WHERE p.created_at >= ? {own_clause}
                GROUP BY day ORDER BY day""",
            (since,),
        ).fetchall())

        hashtags = db.rows_to_dicts(conn.execute(
            f"""SELECT LOWER(j.value) AS tag, COUNT(*) AS n
                FROM posts p, json_each(p.hashtags) j
                WHERE p.created_at >= ? {own_clause}
                GROUP BY LOWER(j.value) ORDER BY n DESC LIMIT 10""",
            (since,),
        ).fetchall())

        tags = db.rows_to_dicts(conn.execute(
            f"""SELECT j.value AS tag, COUNT(*) AS n,
                   SUM(CASE WHEN {SENT}='negative' THEN 1 ELSE 0 END) AS negative
                FROM posts p, json_each(p.tags) j
                WHERE p.created_at >= ? {own_clause} AND j.value NOT LIKE 'risk:%'
                GROUP BY j.value ORDER BY n DESC LIMIT 10""",
            (since,),
        ).fetchall())

        authors = db.rows_to_dicts(conn.execute(
            f"""SELECT a.username, a.display_name, a.followers_count, a.blue, COUNT(*) AS posts,
                   SUM(p.like_count + p.reply_count + p.repost_count + p.quote_count) AS engagement,
                   SUM(COALESCE(p.view_count, 0)) AS views,
                   SUM(CASE WHEN {SENT}='negative' THEN 1 ELSE 0 END) AS negative,
                   MAX(p.risk_score) AS max_risk
                FROM posts p JOIN authors a ON a.id = p.author_id
                WHERE p.created_at >= ? {own_clause}
                GROUP BY a.id ORDER BY engagement DESC, posts DESC LIMIT 12""",
            (since,),
        ).fetchall())

        attention = db.rows_to_dicts(conn.execute(
            f"""SELECT {POST_COLS} FROM posts p JOIN authors a ON a.id = p.author_id
                WHERE p.created_at >= ? AND p.is_context = 0 AND p.is_own = 0 AND p.handled = 0
                  AND (p.risk_level = 'high' OR (p.risk_level = 'medium' AND {NEG_STRONG}))
                ORDER BY p.risk_score DESC, p.created_at DESC LIMIT 6""",
            (since,),
        ).fetchall())

    return {"days": days, "oldest": oldest, "span_days": span_days,
            "bucket": "day" if span_days <= 60 else "week" if span_days <= 400 else "month",
            "current": current, "previous": previous, "daily": daily,
            "hashtags": hashtags, "tags": tags, "authors": authors, "attention": attention}


SORTS = {
    "newest": "p.created_at DESC",
    "activity": "last_activity DESC",
    "engagement": "(p.like_count + p.reply_count + p.repost_count + p.quote_count) DESC, last_activity DESC",
    "views": "COALESCE(p.view_count,0) DESC, last_activity DESC",
    "risk": "MAX(p.risk_score, COALESCE(rep.max_risk,0)) DESC, last_activity DESC",
    "replies": "COALESCE(rep.n,0) DESC, last_activity DESC",
}

REPLY_AGG = f"""
    rep AS (
        SELECT p.in_reply_to_post_id AS pid, COUNT(*) AS n,
               SUM(CASE WHEN {SENT}='negative' THEN 1 ELSE 0 END) AS neg,
               SUM(CASE WHEN p.risk_level='high' THEN 1 ELSE 0 END) AS high,
               SUM(CASE WHEN {REVIEW} THEN 1 ELSE 0 END) AS review,
               SUM(CASE WHEN {IMPORTANT} THEN 1 ELSE 0 END) AS important,
               MAX(p.risk_score) AS max_risk, MAX(p.created_at) AS last
        FROM posts p
        WHERE p.in_reply_to_post_id IS NOT NULL AND p.is_context = 0
        GROUP BY p.in_reply_to_post_id
    )"""


@app.get("/api/feed")
def feed(
    days: int = Query(7, ge=1, le=3650),
    tab: Literal["all", "mentions", "keywords", "negative", "risk", "review", "own"] = "all",
    hide_own: bool = False,
    q: str = "",
    tag: str = "",
    sort: str = "newest",
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Threads: a root post (never a reply we hold the parent of) with a summary of its replies."""
    where = ["NOT EXISTS (SELECT 1 FROM posts x WHERE x.id = p.in_reply_to_post_id)",
             "(p.is_context = 0 OR COALESCE(rep.n,0) > 0)",
             "MAX(p.created_at, COALESCE(rep.last, '')) >= ?"]
    params: list = [_since(days)]
    if hide_own:
        where.append("p.is_own = 0")
    if tab == "mentions":
        where.append("(p.source_query = 'mentions' OR COALESCE(rep.n,0) > 0)")
    elif tab == "keywords":
        where.append("p.source_query = 'brand_keywords' AND p.is_own = 0")
    elif tab == "negative":  # our own posts' sentiment is irrelevant; only what others say counts
        where.append(f"(({SENT} = 'negative' AND p.is_context = 0 AND p.is_own = 0) OR COALESCE(rep.neg,0) > 0)")
    elif tab == "risk":
        where.append("(p.risk_level = 'high' OR COALESCE(rep.high,0) > 0)")
    elif tab == "review":
        where.append(f"(({REVIEW} AND p.is_context = 0 AND p.is_own = 0) OR COALESCE(rep.review,0) > 0)")
    elif tab == "own":
        where.append("p.is_own = 1")
    if q.strip():
        where.append("(p.text LIKE ? OR a.username LIKE ? OR EXISTS (SELECT 1 FROM posts r WHERE r.in_reply_to_post_id = p.id AND r.text LIKE ?))")
        params += [f"%{q.strip()}%", f"%{q.strip()}%", f"%{q.strip()}%"]
    if tag.strip():
        where.append("(EXISTS (SELECT 1 FROM json_each(p.tags) j WHERE j.value = ?) OR EXISTS (SELECT 1 FROM posts r, json_each(r.tags) j WHERE r.in_reply_to_post_id = p.id AND j.value = ?))")
        params += [tag.strip(), tag.strip()]

    order = SORTS.get(sort, SORTS["newest"])
    base = f"""
        WITH {REPLY_AGG}
        SELECT {POST_COLS},
               COALESCE(rep.n,0) AS replies_total, COALESCE(rep.neg,0) AS replies_negative,
               COALESCE(rep.high,0) AS replies_high, COALESCE(rep.review,0) AS replies_review,
               COALESCE(rep.important,0) AS replies_important,
               MAX(p.created_at, COALESCE(rep.last, '')) AS last_activity
        FROM posts p JOIN authors a ON a.id = p.author_id LEFT JOIN rep ON rep.pid = p.id
        WHERE {' AND '.join(where)}"""
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ({base})", params).fetchone()[0]
        roots = db.rows_to_dicts(conn.execute(f"{base} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall())
        for r in roots:
            r["replies"] = []
            if r["replies_total"]:
                # replies shown open: the important ones (or the ones matching the tab), max 5
                extra = ""
                if tab == "negative":
                    extra = f" OR {SENT} = 'negative'"
                elif tab == "risk":
                    extra = " OR p.risk_level = 'high'"
                elif tab == "review":
                    extra = f" OR {REVIEW}"
                r["replies"] = db.rows_to_dicts(conn.execute(
                    f"""SELECT {POST_COLS} FROM posts p JOIN authors a ON a.id = p.author_id
                        WHERE p.in_reply_to_post_id = ? AND p.is_context = 0 AND ({IMPORTANT}{extra})
                        ORDER BY p.risk_score DESC, p.like_count DESC, p.created_at DESC LIMIT 5""",
                    (r["id"],),
                ).fetchall())
    return {"total": total, "items": roots}


@app.get("/api/posts/{post_id}/replies")
def replies(post_id: str):
    with db.connect() as conn:
        rows = db.rows_to_dicts(conn.execute(
            f"""SELECT {POST_COLS} FROM posts p JOIN authors a ON a.id = p.author_id
                WHERE p.in_reply_to_post_id = ? AND p.is_context = 0
                ORDER BY p.created_at ASC""",
            (post_id,),
        ).fetchall())
    return {"items": rows}


@app.get("/api/posts")
def posts(
    days: int = Query(7, ge=1, le=3650),
    sentiment: Literal["all", "positive", "neutral", "negative"] = "all",
    risk: Literal["all", "high", "medium", "low"] = "all",
    kind: Literal["all", "mentions", "keywords", "replies", "reposts", "quotes", "original"] = "all",
    include_own: bool = False,
    handled: Literal["all", "yes", "no"] = "all",
    q: str = "",
    tag: str = "",
    sort: str = "newest",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Flat post list (used by exports and tests)."""
    where = ["p.created_at >= ?", "p.is_context = 0"]
    params: list = [_since(days)]
    if not include_own:
        where.append("p.is_own = 0")
    if sentiment != "all":
        where.append(f"{SENT} = ?"); params.append(sentiment)
    if risk != "all":
        where.append("p.risk_level = ?"); params.append(risk)
    if kind == "mentions":
        where.append("p.source_query = 'mentions'")
    elif kind == "keywords":
        where.append("p.source_query != 'mentions'")
    elif kind == "replies":
        where.append("p.in_reply_to_post_id IS NOT NULL")
    elif kind == "reposts":
        where.append("p.is_repost = 1")
    elif kind == "quotes":
        where.append("p.is_quote = 1")
    elif kind == "original":
        where.append("p.in_reply_to_post_id IS NULL AND p.is_repost = 0 AND p.is_quote = 0")
    if handled != "all":
        where.append("p.handled = ?"); params.append(1 if handled == "yes" else 0)
    if q.strip():
        where.append("(p.text LIKE ? OR a.username LIKE ?)")
        params += [f"%{q.strip()}%", f"%{q.strip()}%"]
    if tag.strip():
        where.append("EXISTS (SELECT 1 FROM json_each(p.tags) j WHERE j.value = ?)")
        params.append(tag.strip())
    flat_sorts = {
        "newest": "p.created_at DESC", "oldest": "p.created_at ASC",
        "engagement": "(p.like_count + p.reply_count + p.repost_count + p.quote_count) DESC, p.created_at DESC",
        "views": "COALESCE(p.view_count,0) DESC, p.created_at DESC",
        "risk": "p.risk_score DESC, p.created_at DESC",
        "negative": "p.sentiment_score ASC, p.created_at DESC",
        "positive": "p.sentiment_score DESC, p.created_at DESC",
    }
    order = flat_sorts.get(sort, flat_sorts["newest"])
    with db.connect() as conn:
        rows = db.rows_to_dicts(conn.execute(
            f"""SELECT {POST_COLS} FROM posts p JOIN authors a ON a.id = p.author_id
                WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?""",
            (*params, limit, offset)).fetchall())
        total = conn.execute(
            f"SELECT COUNT(*) FROM posts p JOIN authors a ON a.id = p.author_id WHERE {' AND '.join(where)}",
            params,
        ).fetchone()[0]
    return {"total": total, "items": rows}


@app.get("/api/posts/{post_id}/snapshots")
def snapshots(post_id: str):
    with db.connect() as conn:
        rows = db.rows_to_dicts(conn.execute(
            "SELECT * FROM engagement_snapshots WHERE post_id = ? ORDER BY captured_at", (post_id,)
        ).fetchall())
    return {"items": rows}


class OverrideBody(BaseModel):
    sentiment: Literal["positive", "neutral", "negative"] | None = None


@app.post("/api/posts/{post_id}/override")
def override(post_id: str, body: OverrideBody):
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone():
            raise HTTPException(404, "post not found")
        conn.execute("UPDATE posts SET sentiment_override = ? WHERE id = ?", (body.sentiment, post_id))
    return {"ok": True}


class HandledBody(BaseModel):
    handled: bool
    notes: str | None = None


@app.post("/api/posts/{post_id}/handled")
def set_handled(post_id: str, body: HandledBody):
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone():
            raise HTTPException(404, "post not found")
        if body.notes is None:
            conn.execute("UPDATE posts SET handled = ? WHERE id = ?", (int(body.handled), post_id))
        else:
            conn.execute("UPDATE posts SET handled = ?, notes = ? WHERE id = ?",
                         (int(body.handled), body.notes, post_id))
    return {"ok": True}


@app.get("/api/health")
def health():
    with db.connect() as conn:
        runs = db.rows_to_dicts(conn.execute(
            "SELECT * FROM collection_runs ORDER BY id DESC LIMIT 10"
        ).fetchall())
        counts = conn.execute(
            "SELECT COUNT(*) AS posts, (SELECT COUNT(*) FROM authors) AS authors, MAX(last_seen) AS last_seen FROM posts WHERE is_context = 0"
        ).fetchone()
    accounts = []
    if ACCOUNTS_DB_PATH.exists():
        try:
            c = sqlite3.connect(ACCOUNTS_DB_PATH)
            c.row_factory = sqlite3.Row
            for r in c.execute("SELECT username, active, last_used, stats, error_msg FROM accounts"):
                d = dict(r)
                try:
                    d["total_req"] = sum(int(v) for v in json.loads(d.pop("stats") or "{}").values())
                except (ValueError, TypeError):
                    d["total_req"] = None
                    d.pop("stats", None)
                accounts.append(d)
            c.close()
        except Exception as e:
            accounts.append({"username": "?", "active": 0, "error_msg": str(e)})
    last_ok = next((r for r in runs if r["status"] == "ok"), None)
    return {"posts": counts["posts"], "authors": counts["authors"], "last_seen": counts["last_seen"],
            "last_ok_run": last_ok["finished_at"] if last_ok else None,
            "runs": runs, "accounts": accounts,
            "interval_minutes": load_config()["collection"]["interval_minutes"]}
