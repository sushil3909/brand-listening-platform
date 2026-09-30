"""Sentiment analysis, fully local and free.

Backends:
  - transformers: cardiffnlp/twitter-xlm-roberta-base-sentiment (multilingual, best quality, needs torch)
  - vader: VADER lexicon (English only, tiny, always available)
  - auto: transformers if importable, else vader

Returns (label, score) where label in {positive, neutral, negative} and score in [-1, 1].
"""
from __future__ import annotations

import re
from functools import lru_cache

from .config import env

_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@\w+")
_WS_RE = re.compile(r"\s+")


def clean_text(text: str) -> str:
    text = _URL_RE.sub("", text)
    text = _MENTION_RE.sub("", text)
    text = text.replace("#", "")
    return _WS_RE.sub(" ", text).strip()


class VaderBackend:
    name = "vader"

    def __init__(self) -> None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        self._an = SentimentIntensityAnalyzer()

    def analyze(self, text: str) -> tuple[str, float]:
        c = self._an.polarity_scores(clean_text(text))["compound"]
        if c >= 0.05:
            return "positive", round(c, 4)
        if c <= -0.05:
            return "negative", round(c, 4)
        return "neutral", round(c, 4)


class TransformersBackend:
    name = "transformers"
    MODEL = "cardiffnlp/twitter-xlm-roberta-base-sentiment"
    FALLBACK_TOKENIZER = "FacebookAI/xlm-roberta-base"

    def __init__(self, allow_download: bool = False) -> None:
        import os

        # Never download ~1 GB in the middle of a collection run: only use the model
        # if it is already cached (run download_model.py once to fetch it).
        # HF_HUB_OFFLINE must be set before transformers/huggingface_hub are imported.
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        if not allow_download:
            os.environ["HF_HUB_OFFLINE"] = "1"
        else:
            os.environ.pop("HF_HUB_OFFLINE", None)

        from transformers import AutoTokenizer, pipeline
        from transformers.utils import logging as hf_logging

        # transformers 5 prints a spurious "incorrect regex pattern" warning for this
        # tokenizer; token ids were verified identical to the base XLM-R tokenizer.
        hf_logging.set_verbosity_error()

        # The cardiffnlp repo ships only a raw sentencepiece file; newer transformers
        # needs protobuf to convert it. If that fails, use the identical vocabulary
        # from the base XLM-R model (same tokenizer, same ids).
        try:
            tokenizer = AutoTokenizer.from_pretrained(self.MODEL)
        except Exception:
            tokenizer = AutoTokenizer.from_pretrained(self.FALLBACK_TOKENIZER)

        self._pipe = pipeline(
            "sentiment-analysis",
            model=self.MODEL,
            tokenizer=tokenizer,
            truncation=True,
            max_length=256,
            top_k=None,
        )

    def analyze(self, text: str) -> tuple[str, float]:
        text = clean_text(text) or "."
        scores = {d["label"].lower(): d["score"] for d in self._pipe(text)[0]}
        pos = scores.get("positive", 0.0)
        neg = scores.get("negative", 0.0)
        neu = scores.get("neutral", 0.0)
        label = max(scores, key=scores.get)
        # signed confidence: +pos, -neg, neutral pulls toward 0
        score = round(pos - neg, 4)
        if label == "neutral" and abs(score) < 0.5:
            score = round(score * (1 - neu), 4)
        return label, score


_POS_EMOJI = set("🔥💯🚀👍😍🤩💪👏🙌❤🥰😊😎💰🤑🎉✅💚💙⭐🌟👌🫡🙏😁😀😃😄🤝📈💎🏆🥇✨💥🫶😘🥳🙂😉")
# 😭 and 💀 are left out on purpose: in trader/X slang they usually mean "laughing", not sadness.
_NEG_EMOJI = set("😡🤬😠👎💔😢😞😤🤮🤢💩🚩⚠❌🙄😒📉🤡😩😫😑😐🥲")
_ALPHA_RE = re.compile(r"[^\W\d_]")


def emoji_signal(text: str) -> int:
    """Net count of positive minus negative emoji."""
    return sum(1 for ch in text if ch in _POS_EMOJI) - sum(1 for ch in text if ch in _NEG_EMOJI)


def _label_from_score(score: float, pos_th: float = 0.2, neg_th: float = -0.2) -> str:
    if score >= pos_th:
        return "positive"
    if score <= neg_th:
        return "negative"
    return "neutral"


def analyze_with_rules(text: str, backend) -> tuple[str, float]:
    """Model sentiment plus a small emoji rule.

    Many replies are just "🔥🔥🔥" or "💯": the language model cannot read them and
    tends to call them negative. With no real words, emoji decide. With words, emoji
    nudge the score a little (±0.2 per net emoji, max ±0.4).
    """
    cleaned = clean_text(text)
    words = len(_ALPHA_RE.findall(cleaned))
    net = emoji_signal(text)

    if words < 4:  # effectively no words
        if net > 0:
            return "positive", round(min(0.9, 0.5 + 0.1 * net), 4)
        if net < 0:
            return "negative", round(max(-0.9, -0.5 + 0.1 * net), 4)
        return "neutral", 0.0

    label, score = backend.analyze(text)
    if net != 0:
        score = round(max(-1.0, min(1.0, score + max(-0.4, min(0.4, 0.2 * net)))), 4)
        label = _label_from_score(score)
    return label, score


@lru_cache(maxsize=1)
def get_backend():
    choice = (env("SENTIMENT_BACKEND", "auto") or "auto").lower()
    if choice in ("auto", "transformers"):
        try:
            backend = TransformersBackend()
            print(f"[sentiment] backend: {backend.name} ({TransformersBackend.MODEL})")
            return backend
        except Exception as e:  # torch/transformers missing, or model not downloaded yet
            if choice == "transformers":
                raise
            print(f"[sentiment] transformer model not available ({e.__class__.__name__}); using VADER. "
                  "Run download_model.py once to enable the better multilingual model.")
    backend = VaderBackend()
    print(f"[sentiment] backend: {backend.name}")
    return backend


def analyze(text: str) -> tuple[str, float]:
    return analyze_with_rules(text, get_backend())
