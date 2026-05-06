"""
analyzer/signal_scorer.py
Combines pump.fun metadata, DEX market data, and community traction
into a 0-100 buy score. Tokens above MIN_BUY_SCORE get queued for execution.
"""

import asyncio
import time
from typing import Tuple
from loguru import logger
from config import MIN_BUY_SCORE, MIN_LIQUIDITY_SOL
from detector.dex_monitor import DexMonitor
from detector.social_monitor import get_social_stats


SOL_USD_APPROX = 150.0


class SignalScorer:
    def __init__(self, raw_queue: asyncio.Queue, trade_queue: asyncio.Queue):
        self.raw_queue  = raw_queue
        self.trade_queue = trade_queue
        self.dex = DexMonitor()
        self.running = False
        self.scored_count = 0

    async def start(self):
        await self.dex.start()
        self.running = True
        logger.info("Signal scorer started")
        await self._run_loop()

    async def stop(self):
        self.running = False
        await self.dex.stop()

    async def _run_loop(self):
        while self.running:
            try:
                token = await asyncio.wait_for(self.raw_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            asyncio.create_task(self._score_token(token))

    async def _score_token(self, token: dict):
        mint = token.get("mint", "")
        symbol = token.get("symbol", "???")

        try:
            token = await self.dex.enrich_token(token)

            social = get_social_stats(mint)
            token.update(social)
            sym_social = get_social_stats(f"SYM:{symbol.upper()}")
            token["social_mentions_5m"] = max(
                token.get("social_mentions_5m", 0),
                sym_social.get("social_mentions_5m", 0)
            )

            score, breakdown = self._compute_score(token)
            token["score"] = score
            token["score_breakdown"] = breakdown
            token["scored_at"] = time.time()

            self.scored_count += 1

            logger.info(
                f"[SCORE] {symbol} | {mint[:8]}... | Score: {score}/100 | "
                f"Liq: ${token.get('liquidity_usd', 0):,.0f} | "
                f"Vol5m: ${token.get('volume_5m_usd', 0):,.0f} | "
                f"Buys5m: {token.get('buys_5m', 0)}"
            )

            if self._passes_hard_filters(token):
                if score >= MIN_BUY_SCORE:
                    logger.success(f"[BUY SIGNAL] {symbol} scored {score}/100 — queuing for execution")
                    await self.trade_queue.put(token)
                else:
                    logger.debug(f"[SKIP] {symbol} scored {score}/100 — below threshold {MIN_BUY_SCORE}")
            else:
                logger.warning(f"[FILTERED] {symbol} failed hard filters")

        except Exception as e:
            logger.error(f"Score error for {mint[:8]}: {e}")

    def _passes_hard_filters(self, token: dict) -> bool:
        if token.get("rug_risk_flag"):
            logger.warning(f"  ✗ Rug risk flag")
            return False

        liq_usd = token.get("liquidity_usd", 0)
        min_liq_usd = MIN_LIQUIDITY_SOL * SOL_USD_APPROX
        if liq_usd > 0 and liq_usd < min_liq_usd:
            logger.warning(f"  ✗ Liquidity too low: ${liq_usd:,.0f}")
            return False

        price_change_5m = token.get("price_change_5m", 0)
        if price_change_5m < -30:
            logger.warning(f"  ✗ Already dumping: {price_change_5m:.1f}% in 5m")
            return False

        return True

    def _compute_score(self, token: dict) -> Tuple[int, dict]:
        breakdown = {}

        # ── Factor 1: Community traction (0-25) ────────────────────────────
        # On-chain proxy for social signals (no Twitter/TG APIs needed)
        holders = token.get("holder_count", 0)
        buys_5m = token.get("buys_5m", 0)
        sells_5m = token.get("sells_5m", 0)
        replies = token.get("reply_count", 0)

        community_score = 0

        # Holder count (broad ownership)
        if holders >= 100:    community_score += 10
        elif holders >= 50:   community_score += 7
        elif holders >= 20:   community_score += 4
        elif holders >= 10:   community_score += 2

        # Unique buyers in 5min
        if buys_5m >= 30:     community_score += 5
        elif buys_5m >= 15:   community_score += 3
        elif buys_5m >= 5:    community_score += 1

        # Buy/sell pressure
        total_tx = buys_5m + sells_5m
        if total_tx >= 5:
            buy_ratio = buys_5m / total_tx
            if buy_ratio >= 0.75:   community_score += 5
            elif buy_ratio >= 0.60: community_score += 3
            elif buy_ratio >= 0.50: community_score += 1

        # Tx frenzy or reply count
        if total_tx >= 50 or replies >= 30:   community_score += 5
        elif total_tx >= 20 or replies >= 10: community_score += 3
        elif total_tx >= 10 or replies >= 5:  community_score += 1

        breakdown["community"] = min(community_score, 25)

        # ── Factor 2: Volume momentum (0-25) ──────────────────────────────
        vol_5m = token.get("volume_5m_usd", 0)

        vol_score = 0
        if vol_5m >= 50000:   vol_score += 15
        elif vol_5m >= 10000: vol_score += 10
        elif vol_5m >= 2000:  vol_score += 5
        elif vol_5m >= 500:   vol_score += 2

        if total_tx > 0:
            buy_ratio = buys_5m / total_tx
            if buy_ratio >= 0.75: vol_score += 10
            elif buy_ratio >= 0.6: vol_score += 5

        breakdown["volume"] = min(vol_score, 25)

        # ── Factor 3: Price momentum (0-25) ───────────────────────────────
        price_5m = token.get("price_change_5m", 0)
        price_1h = token.get("price_change_1h", 0)

        price_score = 0
        if 5 <= price_5m <= 100:    price_score += 15
        elif price_5m > 100:        price_score += 8
        elif price_5m > 0:          price_score += 5

        if price_1h > 50:           price_score += 10
        elif price_1h > 20:         price_score += 5
        elif price_1h > 0:          price_score += 2

        breakdown["price_momentum"] = min(price_score, 25)

        # ── Factor 4: Token metadata quality (0-25) ───────────────────────
        meta_score = 0

        # Social links
        if token.get("twitter"):   meta_score += 5
        if token.get("telegram"):  meta_score += 5
        if token.get("website"):   meta_score += 3

        # DexScreener presence (real pool, real trading)
        if token.get("dex_pair_address"): meta_score += 5
        if token.get("liquidity_usd", 0) > 3000: meta_score += 3

        # Freshness bonus
        age = token.get("age_minutes", 99)
        if age <= 2:    meta_score += 5
        elif age <= 5:  meta_score += 3
        elif age <= 10: meta_score += 1

        breakdown["metadata"] = min(meta_score, 25)

        total = sum(breakdown.values())
        return min(total, 100), breakdown
