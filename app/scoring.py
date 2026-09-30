"""Risk scoring and product/topic tagging. Pure rules, no external calls.

Keyword matching is whole-word (so "sue" never fires on "issue", "ban" never on "banking").
"""
from __future__ import annotations

import math
import re
from functools import lru_cache


@lru_cache(maxsize=256)
def _pattern(keyword: str) -> re.Pattern:
    # Whole-word/phrase match, case-insensitive; internal spaces match any whitespace.
    parts = [re.escape(p) for p in keyword.strip().lower().split()]
    return re.compile(r"(?<![\w@#])" + r"\s+".join(parts) + r"(?![\w])", re.IGNORECASE)


def _hits(text: str, keywords: list[str]) -> list[str]:
    return [k for k in keywords if k and _pattern(k).search(text)]


def product_tags(text: str, cfg: dict) -> list[str]:
    return [tag for tag, kws in cfg.get("product_tags", {}).items() if _hits(text, kws)]


def risk_score(
    text: str,
    sentiment_label: str | None,
    sentiment_score: float | None,
    engagement: int,
    followers: int,
    is_own: bool,
    cfg: dict,
) -> tuple[int, str, list[str]]:
    """Return (score 0-100, level, matched risk keywords).

    Level "high" requires a real signal: an explicit high-risk keyword (scam, fraud,
    denied...) or strongly negative sentiment. Reach and engagement only amplify.
    """
    if is_own:
        return 0, "low", []

    rk = cfg.get("risk_keywords", {})
    high_hits = _hits(text, rk.get("high", []))
    med_hits = _hits(text, rk.get("medium", []))
    s = float(sentiment_score or 0.0)

    score = 0.0
    # 0-40 from sentiment
    if sentiment_label == "negative":
        score += 20 + 20 * min(1.0, abs(s))
    elif sentiment_label == "neutral":
        score += 5

    # keywords: high 30 (first) + 5 each extra; medium 12 + 3 each extra
    if high_hits:
        score += 30 + 5 * (len(high_hits) - 1)
    if med_hits:
        score += 12 + 3 * (len(med_hits) - 1)

    # engagement 0-20 (log scale, ~1000 interactions => 20)
    score += min(20.0, 20 * math.log10(1 + max(0, engagement)) / 3)

    # author reach 0-10 (log scale, ~100k followers => 10)
    score += min(10.0, 10 * math.log10(1 + max(0, followers)) / 5)

    score_i = int(round(min(100.0, score)))
    th = cfg.get("risk_thresholds", {})
    strong_negative = sentiment_label == "negative" and s <= float(th.get("strong_negative", -0.7))
    if score_i >= int(th.get("high", 60)) and (high_hits or strong_negative):
        level = "high"
    elif score_i >= int(th.get("medium", 35)):
        level = "medium"
    else:
        level = "low"
    return score_i, level, high_hits + med_hits
