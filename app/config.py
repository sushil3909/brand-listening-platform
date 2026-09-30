"""Central configuration: .env (secrets) + config.yaml (queries, keywords)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Data and model cache can live on another drive: set DATA_DIR / HF_HOME in .env.
DATA_DIR = Path(os.getenv("DATA_DIR") or (ROOT / "data")).expanduser()
LOG_DIR = DATA_DIR / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

if os.getenv("HF_HOME"):  # where the ~1.1 GB sentiment model is stored
    Path(os.environ["HF_HOME"]).mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "listening.db"
COLLECTOR_FLAG = DATA_DIR / "collector.running"   # exists while a live collection run is in progress
ACCOUNTS_DB_PATH = DATA_DIR / "accounts.db"
CONFIG_PATH = ROOT / "config.yaml"


def env(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return val.strip()


def load_config() -> dict:
    """Read config.yaml fresh each call so edits apply without restarts."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("brand", {})
    cfg["brand"].setdefault("handle", "")
    cfg["brand"].setdefault("own_accounts", [])
    cfg["brand"].setdefault("keywords", ["acme trading", "acmetrading", f"@{cfg['brand']['handle']}"])
    cfg.setdefault("queries", [])
    cfg.setdefault("collection", {})
    col = cfg["collection"]
    col.setdefault("max_posts_per_query", 300)
    col.setdefault("first_run_lookback_days", 30)
    col.setdefault("refresh_window_days", 2)
    col.setdefault("interval_minutes", 15)
    col.setdefault("fetch_parents_per_run", 25)
    col.setdefault("thread_crawl_days", 7)
    col.setdefault("thread_crawl_posts_per_run", 10)
    cfg.setdefault("product_tags", {})
    cfg.setdefault("risk_keywords", {"high": [], "medium": []})
    cfg.setdefault("risk_thresholds", {"high": 60, "medium": 35})
    return cfg
