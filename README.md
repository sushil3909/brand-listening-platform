# Brand Listening Platform for X

A self-hosted tool that collects what people say about a brand on X, scores every post for
sentiment and business risk, and shows the result on a password-protected dashboard.
Collector, database, scoring and dashboard all run on one machine, with no paid services.

> This is a sanitized version of an internal tool I built for a proprietary trading firm.
> The brand name (`Acme Trading`), handles and keywords are placeholders, and no collected
> data or credentials are included.

## How it works

```
X (logged-in search)  ->  app/collector.py (twscrape)  ->  data/listening.db (SQLite)
                                                                 |
                                   app/sentiment.py + app/scoring.py (local)
                                                                 |
                                                     app/web.py  ->  http://127.0.0.1:8000
```

| Part | File | What it does |
|---|---|---|
| Collector | `app/collector.py` | One search per configured query, incremental, with engagement snapshots over time |
| Sentiment | `app/sentiment.py` | Multilingual transformer model, VADER fallback, emoji rules |
| Risk score | `app/scoring.py` | 0–100 score from keywords, sentiment, engagement and author reach |
| Storage | `app/db.py` | SQLite schema, indexes and column migrations |
| Dashboard | `app/web.py`, `app/templates/index.html` | FastAPI JSON API and a single-page dashboard |
| Backfill | `backfill.py` | Walks back through history in weekly windows, resumable |

## Design notes

**Sentiment.** The primary backend is `cardiffnlp/twitter-xlm-roberta-base-sentiment`, which
handles many languages. If the model is not downloaded, the collector falls back to the VADER
lexicon so it never stops. A small rules layer sits on top: replies that are only emoji
("🔥🔥🔥") carry no words for the model to read, so emoji decide the label there and nudge
the score elsewhere.

**Risk score.** Sentiment contributes up to 40 points, keyword hits add weighted points, and
engagement and author reach add up to 30 more on a log scale. A post is only marked "high"
when it also has a real signal: a high-risk keyword or strongly negative sentiment. Keywords
match whole words, so "sue" never fires inside "issue". The keyword lists live in
`config.yaml` and are tuned to the domain; for a trading firm, "breach" is left out on
purpose because it means failing a challenge rule.

**Collection.** Each run re-scans the last few days so engagement counts stay fresh, fetches
the parent of replies as context without counting it in the statistics, and reads the
conversations under the brand's own recent posts directly, since search misses many replies.
Every post keeps its raw JSON so it can be re-scored later (`rescore.py`).

**Dashboard.** Overview tiles with period-over-period change, volume and sentiment charts,
topic and author tables, and a threaded conversation view. Two access levels via HTTP basic
auth: an admin password (can override sentiment and mark posts handled) and an optional
read-only viewer password.

## Setup

1. `python -m venv .venv` and activate it
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in the cookies of a **dedicated** X account
4. Edit `config.yaml`: brand handle, own accounts, keywords, search queries
5. `python setup_account.py` registers the account and runs a 3-post test search
6. Optional: `python download_model.py` fetches the multilingual model (about 1.1 GB, once)

## Running

| Command | Purpose |
|---|---|
| `python run_collect.py` | One collection pass now |
| `python run_scheduler.py` | Collect every N minutes (interval in `config.yaml`) |
| `python run_web.py` | Dashboard at http://127.0.0.1:8000 |
| `python backfill.py --months 12` | One-off historical backfill; `--status` shows progress |
| `python rescore.py` | Re-run sentiment, tags and risk after changing the model or keywords |

## Notes

- Never use the brand's own account, or any account you cannot afford to lose, for collection.
- X changes its internals regularly. If collection starts failing, refresh the cookies and
  re-run `setup_account.py`, then upgrade `twscrape`.
- Check X's terms of service before using this against a live account.

## Stack

Python, FastAPI, Uvicorn, Jinja2, SQLite, twscrape, Hugging Face Transformers, PyTorch,
VADER, Chart.js
