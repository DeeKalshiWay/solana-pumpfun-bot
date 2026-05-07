"""
detector/social_monitor.py
Monitors Twitter/X and Telegram for hype signals around new tokens.
Produces mention counts and sentiment scores that feed into signal_scorer.
"""

import asyncio
import re
import time
from collections import defaultdict

from loguru import logger

try:
    import tweepy
    TWEEPY_AVAILABLE = True
except ImportError:
    TWEEPY_AVAILABLE = False

try:
    from telethon import TelegramClient, events
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False

from config import (
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_CHANNELS,
    TELEGRAM_PHONE,
    TWITTER_BEARER_TOKEN,
    TWITTER_KEYWORDS,
)

# In-memory store: mint_address -> list of mention timestamps
# Used to compute mention velocity (mentions per minute)
_mention_store: dict = defaultdict(list)
_hype_keywords = [
    "100x", "1000x", "gem", "ape", "buy now", "just launched", "new launch",
    "pump", "moon", "shill", "alpha", "degen", "fire", "🔥", "🚀", "💎", "🟢",
    "early", "dont miss", "low cap", "undervalued",
]


def extract_mints_from_text(text: str) -> list:
    """
    Extract Solana mint addresses from text.
    Solana addresses are base58, 32-44 chars.
    """
    pattern = r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b'
    candidates = re.findall(pattern, text)
    # Filter out common non-mint strings
    excluded = {"pump", "solana", "raydium", "jupiter", "phantom"}
    return [c for c in candidates if c.lower() not in excluded and len(c) >= 40]


def score_hype_text(text: str) -> int:
    """
    Score a text snippet for hype signals.
    Returns 0-100 score.
    """
    text_lower = text.lower()
    score = 0
    for kw in _hype_keywords:
        if kw in text_lower:
            score += 8
    # Emoji density bonus
    emoji_count = sum(1 for c in text if ord(c) > 127)
    score += min(emoji_count * 2, 20)
    return min(score, 100)


def record_mention(mint: str, platform: str, hype_score: int):
    """Record a social mention for a token mint."""
    _mention_store[mint].append({
        "ts": time.time(),
        "platform": platform,
        "hype_score": hype_score,
    })


def get_social_stats(mint: str) -> dict:
    """
    Get social mention velocity for a token.
    Returns mentions in last 1min, 5min, 10min + avg hype score.
    """
    now = time.time()
    mentions = _mention_store.get(mint, [])

    # Clean old mentions (> 30 min)
    recent = [m for m in mentions if now - m["ts"] < 1800]
    _mention_store[mint] = recent

    last_1m  = [m for m in recent if now - m["ts"] < 60]
    last_5m  = [m for m in recent if now - m["ts"] < 300]
    last_10m = [m for m in recent if now - m["ts"] < 600]

    avg_hype = (sum(m["hype_score"] for m in recent) / len(recent)) if recent else 0

    return {
        "social_mentions_1m":  len(last_1m),
        "social_mentions_5m":  len(last_5m),
        "social_mentions_10m": len(last_10m),
        "social_hype_avg":     round(avg_hype, 1),
        "social_platforms":    list({m["platform"] for m in recent}),
    }


# ── Twitter/X Monitor ─────────────────────────────────────────────────────────
class TwitterMonitor:
    """
    Streams tweets matching pump.fun keywords.
    Extracts mint addresses from tweets and records mentions.
    """

    def __init__(self):
        self.running = False
        self.client = None

    def _setup(self):
        if not TWEEPY_AVAILABLE:
            logger.warning("tweepy not installed — Twitter monitoring disabled")
            return False
        if not TWITTER_BEARER_TOKEN:
            logger.warning("No TWITTER_BEARER_TOKEN — Twitter monitoring disabled")
            return False
        self.client = tweepy.Client(bearer_token=TWITTER_BEARER_TOKEN, wait_on_rate_limit=True)
        return True

    async def run(self):
        """
        Twitter v2 filtered stream for real-time tweet monitoring.
        Falls back to search polling if stream not available on free tier.
        """
        if not self._setup():
            return

        self.running = True

        # Try streaming first; fall back to polling
        await self._poll_search()

    async def _poll_search(self):
        """
        Poll Twitter search API every 15 seconds for recent mentions.
        Free/Basic tier compatible.
        """
        query = " OR ".join(f'"{kw}"' for kw in TWITTER_KEYWORDS[:5])
        query += " lang:en -is:retweet"

        last_id = None

        while self.running:
            try:
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: self.client.search_recent_tweets(
                        query=query,
                        max_results=100,
                        since_id=last_id,
                        tweet_fields=["created_at", "public_metrics", "author_id"],
                    )
                )

                if response and response.data:
                    for tweet in response.data:
                        await self._process_tweet(tweet)
                    last_id = response.data[0].id

            except Exception as e:
                logger.debug(f"Twitter poll error: {e}")

            # 15s between polls to respect rate limits
            await asyncio.sleep(15)

    async def _process_tweet(self, tweet):
        text = tweet.text
        hype = score_hype_text(text)

        # Look for mint addresses in tweet
        mints = extract_mints_from_text(text)
        for mint in mints:
            record_mention(mint, "twitter", hype)
            logger.debug(f"[TWITTER] Mint {mint[:8]} mentioned | hype={hype}")

        # Also look for coin symbols/names to correlate later
        # (stored by symbol, resolved to mint by scorer)
        symbols = re.findall(r'\$([A-Z]{2,10})', text)
        for sym in symbols:
            record_mention(f"SYM:{sym}", "twitter", hype)

    def stop(self):
        self.running = False


# ── Telegram Monitor ──────────────────────────────────────────────────────────
class TelegramMonitor:
    """
    Monitors Telegram channels for token calls and mint addresses.
    Uses Telethon (user account) for broad channel access.
    """

    def __init__(self):
        self.client = None
        self.running = False

    async def run(self):
        if not TELETHON_AVAILABLE:
            logger.warning("telethon not installed — Telegram monitoring disabled")
            return
        if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
            logger.warning("No Telegram credentials — Telegram monitoring disabled")
            return

        self.running = True

        try:
            self.client = TelegramClient(
                "pump_bot_session",
                TELEGRAM_API_ID,
                TELEGRAM_API_HASH
            )
            await self.client.start(phone=TELEGRAM_PHONE)
            logger.info(f"Telegram connected | Monitoring {len(TELEGRAM_CHANNELS)} channels")

            @self.client.on(events.NewMessage(chats=TELEGRAM_CHANNELS))
            async def handler(event):
                await self._process_message(event.raw_text, event.chat.username or "unknown")

            await self.client.run_until_disconnected()

        except Exception as e:
            logger.error(f"Telegram error: {e}")

    async def _process_message(self, text: str, channel: str):
        hype = score_hype_text(text)

        # Extract Solana mint addresses
        mints = extract_mints_from_text(text)
        for mint in mints:
            record_mention(mint, f"telegram:{channel}", hype)
            logger.info(f"[TELEGRAM] {channel} | Mint {mint[:8]} | hype={hype}")

        # Extract ticker symbols
        symbols = re.findall(r'\$([A-Z]{2,10})', text)
        for sym in symbols:
            record_mention(f"SYM:{sym}", f"telegram:{channel}", hype)

    async def stop(self):
        self.running = False
        if self.client:
            await self.client.disconnect()


# ── Social aggregator helper ──────────────────────────────────────────────────
class SocialMonitor:
    """Wraps Twitter + Telegram monitors, runs them concurrently."""

    def __init__(self):
        self.twitter = TwitterMonitor()
        self.telegram = TelegramMonitor()

    async def run(self):
        logger.info("Social monitor starting (Twitter + Telegram)...")
        await asyncio.gather(
            self.twitter.run(),
            self.telegram.run(),
            return_exceptions=True,
        )

    async def stop(self):
        self.twitter.stop()
        await self.telegram.stop()
