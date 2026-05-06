"""
analyzer/signal_scorer.py
Upgraded with open-source alpha strategies:

  1. Creator tracking (Dexter) — score bonus for top creators
  2. Bonding curve progress scoring — 30% filter proven to cut rugs by 95%
  3. Transaction velocity scoring — fast accumulation = better graduation odds
  4. Community/momentum hybrid scoring
  5. Hard reject of tokens at risk of rug (whale init, dumping, bad creator)
"""

import asyncio
import time
from typing import Tuple
from loguru import logger
import datetime
from config import (
    MIN_BUY_SCORE, MAX_BONDING_CURVE_PCT, BUY_COOLDOWN_SECONDS,
    ATH_RATIO_REJECT_BELOW, DEAD_HOURS_UTC,
    SYMBOL_BLACKLIST_EXACT, NAME_BLACKLIST_SUBSTRINGS,
)
from detector.dex_monitor import DexMonitor
from detector.social_monitor import get_social_stats
from detector.creator_tracker import creator_tracker
from detector.pumpfun_tracker import get_pumpfun_state
from detector.holder_filter import get_top10_concentration, concentration_too_high
from detector.wallet_intel import wallet_intel
from detector.influencer_monitor import influencer_monitor
from analyzer.counterfactual import counterfactual
import aiohttp


class SignalScorer:
    def __init__(self, raw_queue: asyncio.Queue, trade_queue: asyncio.Queue,
                 executor=None):
        self.raw_queue   = raw_queue
        self.trade_queue = trade_queue
        self.executor    = executor
        self.dex         = DexMonitor()
        self.running     = False
        self.scored_count = 0
        self._rpc_session: aiohttp.ClientSession = None

    async def start(self):
        await self.dex.start()
        self._rpc_session = aiohttp.ClientSession()
        self.running = True
        logger.info("Signal scorer started (v3 — holder filter + circuit breakers)")
        await self._run_loop()

    async def stop(self):
        self.running = False
        await self.dex.stop()
        if self._rpc_session:
            await self._rpc_session.close()

    async def _run_loop(self):
        while self.running:
            try:
                token = await asyncio.wait_for(self.raw_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            asyncio.create_task(self._score_token(token))

    async def _score_token(self, token: dict):
        mint   = token.get("mint", "")
        symbol = token.get("symbol", "???")

        try:
            # Optional validation cooldown (15s = from Chainstack research)
            if BUY_COOLDOWN_SECONDS > 0:
                await asyncio.sleep(BUY_COOLDOWN_SECONDS)

            # Tier 4: if the bundle observer is still watching this mint
            # (4s window after creation), wait for its decision.
            waited = 0
            while waited < 5 and mint and wallet_intel.bundle_pending(mint):
                await asyncio.sleep(0.5)
                waited += 0.5

            # Record creator launch for tracking
            creator = token.get("creator", "")
            if creator:
                creator_tracker.record_launch(creator)

            # Enrich with DEX data if token is not brand-new
            age_min = token.get("age_minutes", 99)
            if age_min > 0.5:
                token = await self.dex.enrich_token(token)

            social = get_social_stats(mint)
            token.update(social)

            pf_state = get_pumpfun_state(mint)
            if pf_state:
                token["pf_reply_count"]   = pf_state.get("reply_count", 0)
                token["pf_ath_ratio"]     = pf_state.get("ath_ratio", 0.0)
                token["pf_is_live"]       = pf_state.get("is_live", False)
                token["pf_trade_stale_s"] = pf_state.get("trade_staleness_s", 999)
                token["pf_has_twitter"]   = bool(pf_state.get("twitter"))
                # Tier 4: comments velocity = replies / minute since tracking began.
                # Cheap derivative — no extra HTTP calls.
                age_s = max(pf_state.get("tracker_age_s", 0), 1)
                token["pf_comment_velocity"] = round(
                    pf_state.get("reply_count", 0) / (age_s / 60), 2
                )

            # Tier 4: influencer mention lookup (free, in-memory)
            if influencer_monitor.is_mentioned(symbol):
                token["influencer_mention"] = influencer_monitor.get_mention(symbol)

            score, breakdown = self._compute_score(token)
            token["score"]           = score
            token["score_breakdown"] = breakdown
            token["scored_at"]       = time.time()

            self.scored_count += 1

            curve_pct = token.get("bonding_curve_pct", 0)
            logger.info(
                f"[SCORE] {symbol} | {mint[:8]}... | Score: {score}/100 | "
                f"Curve: {curve_pct:.1f}% | "
                f"MC: {token.get('market_cap_sol', 0):.1f}S | "
                f"Creator: {token.get('initial_buy_sol', 0):.2f}S"
            )

            # Hard filters first
            if not self._passes_hard_filters(token):
                logger.debug(f"[FILTERED] {symbol}: {token.get('reject_reason')}")
                # Counterfactual: log this rejection so we can later check what
                # the token actually did. If our filters are filtering winners,
                # this surfaces it.
                counterfactual.record_rejection(token, token.get('reject_reason', 'hard_filter'))
                return

            # Score threshold
            if score < MIN_BUY_SCORE:
                token["reject_reason"] = f"score_{score}"
                # Bin by score band, not exact score, so aggregation is meaningful
                band = (score // 10) * 10
                counterfactual.record_rejection(token, f"score_band_{band}")
                return

            # Tier 2: holder concentration RPC check — done last because it's the
            # most expensive filter. If top 10 hold > threshold, it's a rug risk.
            if self._rpc_session is not None:
                pct = await get_top10_concentration(self._rpc_session, mint)
                token["top10_holder_pct"] = round(pct, 1)
                if concentration_too_high(pct):
                    token["reject_reason"] = f"top10_{pct:.0f}pct"
                    logger.debug(f"[FILTERED] {symbol}: top 10 hold {pct:.1f}% (>limit)")
                    counterfactual.record_rejection(token, "top10_concentration")
                    return

            logger.success(f"[BUY SIGNAL] {symbol} scored {score}/100 — queuing")
            await self.trade_queue.put(token)

        except Exception as e:
            logger.error(f"Score error for {mint[:8]}: {e}")

    # ── Hard Filters ──────────────────────────────────────────────────────────
    def _passes_hard_filters(self, token: dict) -> bool:
        # Diurnal filter — skip dead hours (UTC). Cheapest reject, do first.
        if DEAD_HOURS_UTC is not None:
            hour_utc = datetime.datetime.utcnow().hour
            start, end = DEAD_HOURS_UTC
            in_window = (start <= hour_utc < end) if start < end else (hour_utc >= start or hour_utc < end)
            if in_window:
                token["reject_reason"] = f"dead_hours_{hour_utc:02d}utc"
                return False

        # Symbol/name blacklist — pattern-rugs
        symbol = (token.get("symbol", "") or "").strip().upper()
        name   = (token.get("name", "") or "").strip().lower()
        if symbol in SYMBOL_BLACKLIST_EXACT:
            token["reject_reason"] = f"sym_blacklist_{symbol}"
            return False
        if any(p in name for p in NAME_BLACKLIST_SUBSTRINGS):
            token["reject_reason"] = "name_blacklist"
            return False

        # Creator hard-blacklist (3+ trades, net negative, WR<25%)
        creator = token.get("creator", "")
        if creator and creator_tracker.is_blacklisted(creator):
            token["reject_reason"] = "creator_blacklisted"
            return False

        # Tier 4: known sniper-bot creator (50+ pump.fun mints bought)
        if creator and wallet_intel.is_bot_wallet(creator):
            token["reject_reason"] = f"bot_creator_{wallet_intel.wallet_buys(creator)}"
            return False

        # Tier 4: bundled launch (2+ non-creator wallets bought in first 4s)
        mint = token.get("mint", "")
        if mint and wallet_intel.is_bundled_launch(mint):
            token["reject_reason"] = "bundled_launch"
            return False

        # ATH-ratio reject — token already dumped from peak; we'd be top-buying.
        # Only fires when tracker has data (pf_ath_ratio > 0).
        ath_ratio = token.get("pf_ath_ratio", 0)
        if ATH_RATIO_REJECT_BELOW > 0 and 0 < ath_ratio < ATH_RATIO_REJECT_BELOW:
            token["reject_reason"] = f"ath_dump_{ath_ratio:.2f}"
            return False

        # Bonding curve too close to migration — thin upside
        curve_pct = token.get("bonding_curve_pct", 0)
        if curve_pct > MAX_BONDING_CURVE_PCT:
            token["reject_reason"] = f"curve_{curve_pct:.0f}pct"
            return False

        # Rug indicators from Birdeye (only if data present)
        if token.get("rug_risk_flag"):
            token["reject_reason"] = "rug_risk"
            return False

        # Creator buying >5 SOL = sniper/rug setup
        initial_buy = token.get("initial_buy_sol", 0)
        if initial_buy > 5.0:
            token["reject_reason"] = f"whale_init_{initial_buy:.1f}S"
            return False

        # Symbol/name sanity check
        if not token.get("symbol") or token.get("symbol") == "???":
            token["reject_reason"] = "no_symbol"
            return False

        # Already dumping hard
        price_change_5m = token.get("price_change_5m", 0)
        if price_change_5m < -30:
            token["reject_reason"] = f"dumping_{price_change_5m:.0f}pct"
            return False

        return True

    # ── Score Computation ─────────────────────────────────────────────────────
    def _compute_score(self, token: dict) -> Tuple[int, dict]:
        breakdown = {}

        # ── Factor 1: Creator signal (0-25) ──────────────────────────────────
        # Combines on-chain initial buy + creator tracker leaderboard bonus
        initial_buy  = token.get("initial_buy_sol", 0)
        creator      = token.get("creator", "")
        creator_score = 0

        # Sweet spot 0.5–2 SOL shows commitment without rug flag
        if   0.5 <= initial_buy <= 2.0:  creator_score += 15
        elif 0.2 <= initial_buy < 0.5:   creator_score += 10
        elif 2.0 < initial_buy <= 4.0:   creator_score += 8
        elif initial_buy < 0.2:          creator_score += 3

        # Image & metadata quality
        if token.get("image_uri"):        creator_score += 3

        name   = token.get("name", "")
        symbol = token.get("symbol", "")
        if 2 <= len(symbol) <= 10 and name and name != "Unknown":
            creator_score += 4

        # Creator tracking bonus (Dexter strategy: top-50 creators get bonus)
        if creator:
            bonus = creator_tracker.get_score_bonus(creator)
            creator_score += bonus
            if bonus > 0:
                logger.debug(f"[CREATOR BONUS] {symbol} creator={creator[:8]} +{bonus}")
            elif bonus < 0:
                logger.debug(f"[CREATOR PENALTY] {symbol} creator={creator[:8]} {bonus}")

        breakdown["creator"] = max(0, min(creator_score, 25))

        # ── Factor 2: Bonding curve progress (0-25) ───────────────────────────
        # Research: tokens at 30%+ bonding curve have 95% lower rug rate
        # Velocity: fewer transactions to reach same curve level = organic interest
        curve_pct       = token.get("bonding_curve_pct", 0)
        v_sol           = token.get("v_sol_in_bonding", 0)
        curve_score     = 0

        if   curve_pct >= 50:   curve_score += 20   # well-established, real demand
        elif curve_pct >= 30:   curve_score += 15   # proven past dump zone
        elif curve_pct >= 10:   curve_score += 8    # early but has some traction
        elif curve_pct >= 3:    curve_score += 4
        elif curve_pct > 0:     curve_score += 2
        else:
            # Brand new — score based on initial SOL in bonding curve
            if   v_sol >= 5:    curve_score += 10
            elif v_sol >= 2:    curve_score += 7
            elif v_sol >= 0.5:  curve_score += 4
            else:               curve_score += 2    # very fresh, unknown

        # Transaction velocity bonus (research: fast accumulation = better graduation)
        # Proxy: initial_buy / v_sol ratio — higher = fewer wallets bought so far
        if v_sol > 0 and initial_buy > 0:
            concentration = initial_buy / v_sol
            if   concentration >= 0.5:  curve_score += 5   # creator is biggest buyer (fresh)
            elif concentration >= 0.2:  curve_score += 3

        breakdown["bonding_curve"] = min(curve_score, 25)

        # ── Factor 3: Community / buy pressure (0-25) ─────────────────────────
        holders    = token.get("holder_count", 0)
        buys_5m    = token.get("buys_5m", 0)
        sells_5m   = token.get("sells_5m", 0)
        replies    = token.get("reply_count", 0)
        social_1m  = token.get("social_mentions_1m", 0)

        community_score = 0

        if   holders >= 100:  community_score += 10
        elif holders >= 50:   community_score += 7
        elif holders >= 20:   community_score += 4
        elif holders >= 10:   community_score += 2

        if   buys_5m >= 30:   community_score += 5
        elif buys_5m >= 15:   community_score += 3
        elif buys_5m >= 5:    community_score += 1

        total_tx = buys_5m + sells_5m
        if total_tx >= 5:
            buy_ratio = buys_5m / total_tx
            if   buy_ratio >= 0.75:   community_score += 5
            elif buy_ratio >= 0.60:   community_score += 3

        if total_tx >= 50 or replies >= 30:  community_score += 5
        elif total_tx >= 20 or replies >= 10: community_score += 3

        # Social mention velocity
        if social_1m >= 5:    community_score += 5
        elif social_1m >= 2:  community_score += 3

        # Pump.fun reply count + livestream signal (additive bonus from tracker)
        pf_replies = token.get("pf_reply_count", 0)
        if   pf_replies >= 30:  community_score += 6
        elif pf_replies >= 15:  community_score += 4
        elif pf_replies >= 5:   community_score += 2
        if token.get("pf_is_live"):   community_score += 3
        if token.get("pf_has_twitter"): community_score += 1

        # Tier 4: comments velocity (Δreplies/minute) — best real-time hype gauge
        pf_comment_velocity = token.get("pf_comment_velocity", 0)
        if   pf_comment_velocity >= 10: community_score += 8   # 10+ replies/min = viral
        elif pf_comment_velocity >= 5:  community_score += 5
        elif pf_comment_velocity >= 2:  community_score += 2

        # Tier 4: Twitter influencer mention (set externally if a known
        # crypto-Twitter influencer mentioned this ticker recently)
        if token.get("influencer_mention"):
            community_score += 10
            logger.debug(f"[INFLUENCER] {symbol} influencer mention bonus +10")

        # Fresh launch floor — give minimum so new tokens aren't zero-scored
        if token.get("age_minutes", 99) < 1 and community_score == 0:
            community_score = 5

        breakdown["community"] = min(community_score, 25)

        # ── Factor 4: Price momentum (0-25) ──────────────────────────────────
        price_5m    = token.get("price_change_5m", 0)
        price_1h    = token.get("price_change_1h", 0)
        price_score = 0

        # Sweet spot: 5-100% in 5m = organic pump, not rug
        if   5 <= price_5m <= 100:  price_score += 15
        elif price_5m > 100:        price_score += 8    # possibly parabolic, caution
        elif price_5m > 0:          price_score += 5

        if   price_1h > 50:         price_score += 10
        elif price_1h > 20:         price_score += 5
        elif price_1h > 0:          price_score += 2

        # Market cap in prime pump range (research: 25-60 SOL = best entry)
        mc_sol = token.get("market_cap_sol", 0)
        if   25 <= mc_sol <= 60:    price_score += 5   # prime range
        elif 10 <= mc_sol < 25:     price_score += 3

        # Fresh token neutral floor
        if token.get("age_minutes", 99) < 1 and price_score == 0:
            price_score = 8

        breakdown["price_momentum"] = min(price_score, 25)

        total = sum(breakdown.values())
        return min(total, 100), breakdown
