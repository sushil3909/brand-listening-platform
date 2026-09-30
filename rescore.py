"""Re-run sentiment, tags and risk scoring over every stored post.

Use after: downloading the better sentiment model, or editing keywords in config.yaml.
Manual sentiment overrides are kept. Usage: python rescore.py
"""
from __future__ import annotations

import json
import sys

from app import db
from app.config import load_config
from app.scoring import product_tags, risk_score
from app.sentiment import analyze, get_backend


def main() -> int:
    cfg = load_config()
    backend = get_backend()
    print(f"Re-scoring with backend: {backend.name}")
    db.init_db()

    with db.connect() as conn:
        rows = conn.execute(
            """SELECT p.id, p.text, p.is_own, p.sentiment_override,
                      p.like_count + p.reply_count + p.repost_count + p.quote_count AS engagement,
                      a.followers_count
               FROM posts p JOIN authors a ON a.id = p.author_id"""
        ).fetchall()
        # relevance: does the text name the brand itself, or only carry the auto @tag of a reply?
        from app.collector import relevance_of
        conn.executemany(
            "UPDATE posts SET relevance=? WHERE id=?",
            [("explicit" if r["is_own"] else relevance_of(r["text"], cfg), r["id"]) for r in rows],
        )
    print(f"{len(rows)} posts")

    counts = {"positive": 0, "neutral": 0, "negative": 0}
    levels = {"high": 0, "medium": 0, "low": 0}
    batch = []
    for i, r in enumerate(rows, 1):
        label, score = analyze(r["text"])
        effective = r["sentiment_override"] or label
        tags = product_tags(r["text"], cfg)
        rscore, rlevel, hits = risk_score(
            r["text"], effective, score, r["engagement"], r["followers_count"], bool(r["is_own"]), cfg
        )
        counts[effective] = counts.get(effective, 0) + 1
        levels[rlevel] += 1
        batch.append((label, score, rscore, rlevel,
                      json.dumps(sorted(set(tags + [f"risk:{k}" for k in hits]))), r["id"]))
        if i % 100 == 0 or i == len(rows):
            with db.connect() as conn:
                conn.executemany(
                    "UPDATE posts SET sentiment_label=?, sentiment_score=?, risk_score=?, risk_level=?, tags=? WHERE id=?",
                    batch,
                )
            batch = []
            print(f"  {i}/{len(rows)}")

    print(f"Done. Sentiment: {counts}. Risk: {levels}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
