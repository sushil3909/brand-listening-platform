"""Register the dedicated X collection account (cookies from .env) with twscrape.

Usage:  python setup_account.py
Re-run any time the cookies change (e.g. after logging out/in again).
"""
from __future__ import annotations

import asyncio
import sys

from twscrape import API

from app.config import ACCOUNTS_DB_PATH, env, load_config


async def main() -> int:
    username = env("X_ACCOUNT_USERNAME")
    auth_token = env("X_AUTH_TOKEN")
    ct0 = env("X_CT0")

    missing = [k for k, v in {"X_ACCOUNT_USERNAME": username, "X_AUTH_TOKEN": auth_token, "X_CT0": ct0}.items() if not v]
    if missing:
        print(f"ERROR: missing in .env: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in the values first.")
        return 1

    cfg = load_config()
    own = {a.lower() for a in cfg["brand"].get("own_accounts", [])} | {cfg["brand"]["handle"].lower()}
    if username.lstrip("@").lower() in own:
        print("ERROR: refusing to use the brand account for collection. Use a dedicated account.")
        return 1

    if len(auth_token) < 30 or len(ct0) < 30:
        print("ERROR: auth_token / ct0 look too short. Copy the full cookie VALUES (not names).")
        return 1

    api = API(str(ACCOUNTS_DB_PATH))
    await api.pool.add_account_cookies(username.lstrip("@"), f"auth_token={auth_token}; ct0={ct0}")

    print("Account registered. Testing with a tiny search...")
    api = API(str(ACCOUNTS_DB_PATH), raise_when_no_account=True, wait_timeout=30)
    got = 0
    try:
        async for tweet in api.search(f"@{cfg['brand']['handle']}", limit=3):
            got += 1
            print(f"  OK  {tweet.date:%Y-%m-%d %H:%M}  @{tweet.user.username}: {tweet.rawContent[:80]!r}")
    except Exception as e:
        print(f"ERROR: search test failed: {e.__class__.__name__}: {e}")
        print("Most likely the cookies are wrong/expired, or the account is locked. Log in again in the browser and re-copy them.")
        return 1

    if got == 0:
        print("WARNING: search returned 0 posts. Cookies accepted, but check the account is not restricted.")
    else:
        print(f"SUCCESS: account works ({got} posts fetched).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
