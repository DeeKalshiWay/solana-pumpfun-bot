"""
detector/pumpfun_tracker.py

Tracks pump.fun coin metadata in real time via the public v3 endpoint:
    https://frontend-api-v3.pump.fun/coins/{mint}

CONSOLIDATED: this module no longer opens its own WS to PumpPortal. It
registers a callback on the shared PumpFunMonitor connection.

For each new token it sees from the shared stream, it polls that mint's
metadata every PUMPFUN_POLL_INTERVAL_S seconds for up to
PUMPFUN_TRACK_MINUTES minutes. From each poll it derives:

    - reply velocity      (delta replies since previous poll)
    - market-cap deltas   (current vs ATH)
    - trade staleness     (now - last_trade_timestamp)
    - livestream flag

Reply deltas are forwarded to detector.social_monitor.record_mention()
so the existing signal_scorer's `social_mentions_*` terms light up.
Richer per-mint state is exposed via get_pumpfun_state(mint).
"""
import asyncio
import time

import aiohttp
from loguru import logger

from config import (
    PUMPFUN_MAX_CONCURRENT_TRACKS,
    PUMPFUN_POLL_INTERVAL_S,
    PUMPFUN_TRACK_ENABLED,
    PUMPFUN_TRACK_MINUTES,
)
from detector.social_monitor import record_mention

V3_COIN_URL = "https://frontend-api-v3.pump.fun/coins/{mint}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://pump.fun",
    "Referer": "https://pump.fun/",
}

# Per-mint live state, readable by the scorer via get_pumpfun_state().
# Cleared when the tracker stops following a mint.
_state: dict = {}


def get_pumpfun_state(mint: str) -> dict:
    """Snapshot of the latest tracked metrics for a mint. {} if not tracked."""
    return _state.get(mint, {}).copy()


class PumpFunTracker:
    """
    Subscribes to PumpPortal newToken events, then polls pump.fun's v3 API
    for each mint to capture reply velocity and momentum metrics.
    """

    def __init__(self, monitor=None):
        """
        monitor: PumpFunMonitor instance to subscribe to. If provided, we
                 register a create-event callback. If None, run() will be a
                 no-op pass-through (back-compat for older startup code).
        """
        self.running = False
        self._session: aiohttp.ClientSession | None = None
        self._tracked: set[str] = set()
        self._sem = asyncio.Semaphore(PUMPFUN_MAX_CONCURRENT_TRACKS)
        self._monitor = monitor
        if monitor is not None:
            monitor.subscribe_create(self._on_create_event)

    def _on_create_event(self, data: dict):
        """Callback fired by PumpFunMonitor for every new token."""
        if not PUMPFUN_TRACK_ENABLED or not self.running:
            return
        mint = data.get("mint")
        if not mint or mint in self._tracked:
            return
        self._tracked.add(mint)
        asyncio.create_task(self._track_one(mint, data))

    async def run(self):
        """No more WS — just stays alive while the monitor feeds us callbacks."""
        if not PUMPFUN_TRACK_ENABLED:
            logger.info("PumpFun tracker disabled via config")
            return

        self.running = True
        self._session = aiohttp.ClientSession(headers=_HEADERS)
        logger.info("PumpFun tracker started (piggybacks on shared PumpPortal WS)")

        try:
            # Just stay alive — the actual work happens in _on_create_event.
            while self.running:
                await asyncio.sleep(60)
        finally:
            if self._session:
                await self._session.close()

    def stop(self):
        self.running = False

    async def _track_one(self, mint: str, create_data: dict):
        """
        Poll v3/coins/{mint} every PUMPFUN_POLL_INTERVAL_S seconds for up to
        PUMPFUN_TRACK_MINUTES minutes, recording reply deltas and updating state.
        """
        async with self._sem:
            url = V3_COIN_URL.format(mint=mint)
            sym = create_data.get("symbol", "?")
            started_at = time.time()
            deadline = started_at + PUMPFUN_TRACK_MINUTES * 60

            prev_replies = 0
            prev_mc = float(create_data.get("marketCapSol", 0)) * 150  # rough USD
            poll_count = 0
            consecutive_errors = 0

            try:
                while self.running and time.time() < deadline:
                    poll_count += 1
                    try:
                        snapshot = await self._fetch(url)
                        consecutive_errors = 0
                    except Exception:
                        consecutive_errors += 1
                        if consecutive_errors >= 5:
                            logger.debug(
                                f"[PUMPFUN-TRACK] {sym} {mint[:8]} giving up after 5 errors"
                            )
                            break
                        await asyncio.sleep(PUMPFUN_POLL_INTERVAL_S)
                        continue

                    if not snapshot:
                        await asyncio.sleep(PUMPFUN_POLL_INTERVAL_S)
                        continue

                    # Reply velocity → record_mention() so the scorer picks it up
                    replies = int(snapshot.get("reply_count", 0) or 0)
                    if replies > prev_replies:
                        delta = replies - prev_replies
                        # cap to avoid pathological bursts
                        for _ in range(min(delta, 25)):
                            record_mention(mint, "pumpfun_comments", hype_score=40)
                        prev_replies = replies

                    # Build state dict for the scorer
                    mc          = float(snapshot.get("usd_market_cap", 0) or 0)
                    ath         = float(snapshot.get("ath_market_cap", 0) or 0)
                    last_trade  = int(snapshot.get("last_trade_timestamp", 0) or 0) / 1000.0
                    age_seconds = time.time() - (
                        int(snapshot.get("created_timestamp", 0) or 0) / 1000.0
                    )

                    ath_ratio       = (mc / ath) if ath > 0 else 0.0
                    trade_staleness = max(0.0, time.time() - last_trade) if last_trade > 0 else 999.0
                    mc_growth_pct   = ((mc - prev_mc) / prev_mc * 100) if prev_mc > 0 else 0.0

                    _state[mint] = {
                        "reply_count":        replies,
                        "usd_market_cap":     mc,
                        "ath_market_cap":     ath,
                        "ath_ratio":          round(ath_ratio, 4),
                        "trade_staleness_s":  round(trade_staleness, 1),
                        "is_live":            bool(snapshot.get("is_currently_live", False)),
                        "is_banned":          bool(snapshot.get("is_banned", False)),
                        "complete":           bool(snapshot.get("complete", False)),
                        "twitter":            snapshot.get("twitter", "") or "",
                        "website":            snapshot.get("website", "") or "",
                        "mc_growth_pct":      round(mc_growth_pct, 2),
                        "tracker_age_s":      round(age_seconds, 1),
                        "tracker_polls":      poll_count,
                        "tracker_updated_at": time.time(),
                    }
                    prev_mc = mc

                    # Stop early if token migrated or is banned
                    if snapshot.get("complete") or snapshot.get("is_banned"):
                        logger.info(
                            f"[PUMPFUN-TRACK] {sym} {mint[:8]} "
                            f"{'migrated' if snapshot.get('complete') else 'banned'}, stop"
                        )
                        break

                    await asyncio.sleep(PUMPFUN_POLL_INTERVAL_S)
            finally:
                # Keep state for ~5 more minutes so scorer / exit logic can read it,
                # then drop to bound memory.
                async def _expire():
                    await asyncio.sleep(300)
                    _state.pop(mint, None)
                    self._tracked.discard(mint)
                asyncio.create_task(_expire())

    async def _fetch(self, url: str) -> dict | None:
        if not self._session:
            return None
        async with self._session.get(
            url, timeout=aiohttp.ClientTimeout(total=6)
        ) as resp:
            if resp.status != 200:
                return None
            try:
                return await resp.json()
            except aiohttp.ContentTypeError:
                return None
