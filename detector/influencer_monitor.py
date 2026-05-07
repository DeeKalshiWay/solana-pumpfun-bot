"""
detector/influencer_monitor.py

Tier 4: Twitter influencer mention scanner.

Polls a watchlist of crypto-Twitter accounts every INFLUENCER_POLL_S seconds
for mentions of pump.fun tickers (`$TICKER` or mint addresses). Tagged
tokens get a +10 score bonus.

Cost / dependencies:
  - REQUIRES TWITTER_BEARER_TOKEN (free Basic tier works) — uses Twitter v2
    /users/by + /tweets endpoints. If no token is set, the monitor is a
    no-op so nothing breaks.
  - Watchlist lives in logs/influencers.json — edit any time, hot-reloaded.

Strategy: only flag $TICKER tweets from the LAST 5 MINUTES so we catch
real-time alpha calls, not week-old pumps. The first 30 sec after an
influencer tweets a fresh ticker is when retail FOMO peaks.
"""

import asyncio
import json
import os
import re
import time

import aiohttp
from loguru import logger

from config import TWITTER_BEARER_TOKEN

INFLUENCER_FILE     = "logs/influencers.json"
MENTION_TTL_SECONDS = 300              # 5 min freshness window
INFLUENCER_POLL_S   = 60               # check Twitter every 60s

# Default watchlist — user can edit logs/influencers.json to override
DEFAULT_HANDLES = [
    # Solana / pump.fun degen-Twitter accounts (handles only, no @)
    "ansemtrades",
    "MustStopMurad",
    "blknoiz06",
    "0xMert_",
    "shaft_finance",
]

TICKER_RE = re.compile(r'\$([A-Z]{2,10})\b')


class InfluencerMonitor:
    """
    Maintains a rolling cache of recently-mentioned tickers from a list of
    crypto-Twitter accounts. The signal scorer reads this cache when scoring.
    """

    def __init__(self):
        self.running = False
        self._mentions: dict[str, dict] = {}    # ticker_upper -> {ts, handle}
        self._handles: list = []
        self._user_id_cache: dict[str, str] = {}
        self._load_handles()

    def _load_handles(self):
        os.makedirs("logs", exist_ok=True)
        if os.path.exists(INFLUENCER_FILE):
            try:
                with open(INFLUENCER_FILE, encoding="utf-8") as f:
                    self._handles = json.load(f).get("handles", DEFAULT_HANDLES)
                logger.info(f"[INFLUENCER] Loaded {len(self._handles)} handles from disk")
                return
            except Exception as e:
                logger.warning(f"[INFLUENCER] Could not load handles: {e}")
        # Seed with defaults
        try:
            with open(INFLUENCER_FILE, "w", encoding="utf-8") as f:
                json.dump({"handles": DEFAULT_HANDLES}, f, indent=2)
        except Exception:
            pass
        self._handles = DEFAULT_HANDLES

    def is_mentioned(self, symbol: str) -> bool:
        """Check if a ticker symbol was mentioned by an influencer in the last 5 min."""
        if not symbol:
            return False
        rec = self._mentions.get(symbol.upper())
        if not rec:
            return False
        return (time.time() - rec["ts"]) < MENTION_TTL_SECONDS

    def get_mention(self, symbol: str) -> dict:
        return self._mentions.get(symbol.upper(), {})

    async def run(self):
        if not TWITTER_BEARER_TOKEN:
            logger.info("[INFLUENCER] No TWITTER_BEARER_TOKEN — influencer monitor disabled (set one in .env to enable)")
            return
        self.running = True
        logger.info(f"[INFLUENCER] Monitor started — watching {len(self._handles)} handles")

        async with aiohttp.ClientSession(headers={
            "Authorization": f"Bearer {TWITTER_BEARER_TOKEN}",
            "User-Agent":    "pump-bot/1.0",
        }) as session:
            while self.running:
                try:
                    await self._sweep(session)
                except Exception as e:
                    logger.debug(f"[INFLUENCER] sweep error: {e}")
                await asyncio.sleep(INFLUENCER_POLL_S)

    async def _sweep(self, session: aiohttp.ClientSession):
        """One pass: for each handle, fetch latest tweets and extract $TICKERs."""
        for handle in self._handles:
            try:
                user_id = await self._get_user_id(session, handle)
                if not user_id:
                    continue
                tweets = await self._get_recent_tweets(session, user_id)
                for tw in tweets:
                    text = tw.get("text", "")
                    for sym in TICKER_RE.findall(text):
                        sym_u = sym.upper()
                        # Cache mention with timestamp
                        if sym_u not in self._mentions:
                            logger.info(f"[INFLUENCER] @{handle} mentioned ${sym_u}")
                        self._mentions[sym_u] = {"ts": time.time(), "handle": handle}
            except Exception as e:
                logger.debug(f"[INFLUENCER] {handle} error: {e}")

        # Garbage-collect stale mentions
        cutoff = time.time() - MENTION_TTL_SECONDS
        for k in list(self._mentions.keys()):
            if self._mentions[k]["ts"] < cutoff:
                del self._mentions[k]

    async def _get_user_id(self, session, handle: str) -> str:
        if handle in self._user_id_cache:
            return self._user_id_cache[handle]
        url = f"https://api.twitter.com/2/users/by/username/{handle}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return ""
            data = await r.json()
            uid = data.get("data", {}).get("id", "")
            if uid:
                self._user_id_cache[handle] = uid
            return uid

    async def _get_recent_tweets(self, session, user_id: str) -> list:
        url = f"https://api.twitter.com/2/users/{user_id}/tweets"
        params = {"max_results": 5, "tweet.fields": "created_at"}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return []
            data = await r.json()
            return data.get("data", []) or []

    def stop(self):
        self.running = False


# Singleton
influencer_monitor = InfluencerMonitor()
