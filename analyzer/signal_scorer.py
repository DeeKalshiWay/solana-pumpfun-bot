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
import datetime
import json
import time

import aiohttp
from loguru import logger

from analyzer.auto_tuner import auto_tuner
from analyzer.counterfactual import counterfactual
from analyzer.regime_filter import regime_filter
from analyzer.rug_memory import rug_memory
from analyzer.signal_fusion import compute_fusion
from config import (
    ATH_RATIO_REJECT_BELOW,
    BUY_COOLDOWN_SECONDS,
    DEAD_HOURS_UTC,
    FUSION_ENABLED,
    FUSION_MAX_BONUS,
    FUSION_MAX_PENALTY,
    MAX_BONDING_CURVE_PCT,
    MAX_INITIAL_BUY_SOL,
    NAME_BLACKLIST_SUBSTRINGS,
    RPC_URL,
    SMART_CALLER_MIN,
    SYMBOL_BLACKLIST_EXACT,
)

# Note: MIN_BUY_SCORE is no longer imported directly — the dynamic
# threshold comes from auto_tuner.effective_min_score().
from detector.creator_tracker import creator_tracker
from detector.dex_monitor import DexMonitor
from detector.holder_filter import concentration_too_high, get_top10_concentration
from detector.influencer_monitor import influencer_monitor
from detector.pumpfun_tracker import get_pumpfun_state
from detector.social_monitor import get_social_stats
from detector.wallet_intel import wallet_intel
from detector.whale_tracker import whale_tracker
from detector.x_feed import x_feed


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
            except TimeoutError:
                continue
            asyncio.create_task(self._score_token(token))

    async def _score_token(self, token: dict):
        mint   = token.get("mint", "")
        symbol = token.get("symbol", "???")

        try:
            # Optional validation cooldown (15s = from Chainstack research)
            if BUY_COOLDOWN_SECONDS > 0:
                await asyncio.sleep(BUY_COOLDOWN_SECONDS)

            # ── Parallel I/O: kick off DEX enrichment NOW so it runs alongside
            # the bundle wait (~5s blocking) instead of stacking serially after.
            # Saves up to ~1s per token on busy mints.
            age_min = token.get("age_minutes", 99)
            dex_task = (
                asyncio.create_task(self.dex.enrich_token(token))
                if age_min > 0.5 else None
            )

            # Tier 4: if the bundle observer is still watching this mint
            # (4s window after creation), wait for its decision.
            waited = 0
            while waited < 5 and mint and wallet_intel.bundle_pending(mint):
                await asyncio.sleep(0.5)
                waited += 0.5

            # Record creator launch for tracking (cheap, in-memory)
            creator = token.get("creator", "")
            if creator:
                creator_tracker.record_launch(creator)

            # Pull DEX result now — it's been running in parallel during the
            # bundle wait above, so this typically returns immediately.
            if dex_task is not None:
                try:
                    token = await dex_task
                except Exception as e:
                    logger.debug(f"[SCORE] dex enrich failed for {symbol}: {e}")

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

            # X-feed bridge — tails the standalone x-monitor JSONL output and
            # exposes a single boolean. Fed to BOTH the four-factor community
            # score and the fusion engine so X hype can compound when it
            # aligns with on-chain accumulation.
            try:
                if x_feed.has_hype_for(token):
                    token["x_hype_match"] = True
            except Exception as e:
                logger.debug(f"[X-FEED] lookup err for {symbol}: {e}")

            # Cache smart-buyer count on the token so the four-factor scorer
            # and fusion engine read the same value (and we only hit
            # wallet_intel once per token).
            if mint:
                token["smart_buyer_count"] = len(wallet_intel.smart_buyers_in_window(mint))
                # Whale wallets are different from smart wallets — classified
                # by SOL VOLUME not win-rate. A whale buying early says "real
                # money decided this is worth a position" which is independent
                # signal from the smart-money/win-rate classifier.
                token["whale_buyer_count"]  = len(whale_tracker.whale_buyers_in_window(mint))
                token["whale_buy_volume"]   = whale_tracker.whale_buy_volume(mint)

            score, breakdown = self._compute_score(token)
            # Stash the RAW (pre-rug-penalty, pre-fusion) score so:
            #   - rug_memory record + lookup use the SAME bucket key (otherwise
            #     records go in at post-penalty bins and lookups fire at
            #     pre-penalty bins → matches never happen, feature dead)
            #   - downstream logging can show both raw and effective scores
            #   - fusion changes don't shift the rug_memory bin boundaries
            token["raw_score"] = score

            # Signal fusion: bonus/penalty when independent signals co-fire.
            # Applied AFTER the four-factor sum so it shows up cleanly in
            # the breakdown for counterfactual attribution. Capped at
            # ±FUSION_MAX_BONUS/PENALTY so it can't dominate the score.
            if FUSION_ENABLED:
                fusion_delta, fusion_breakdown = compute_fusion(
                    token, FUSION_MAX_BONUS, FUSION_MAX_PENALTY,
                )
                if fusion_delta != 0:
                    breakdown["fusion"]      = fusion_delta
                    token["fusion_patterns"] = fusion_breakdown
                    score = max(0, min(100, score + fusion_delta))

            # Rug-pattern memory: if this candidate's feature signature has
            # rugged repeatedly in the past, dock the score. Penalty is 0
            # when there's no match (or fewer than MATCH_MIN_RUGS).
            rug_features = {
                "initial_buy_sol":   token.get("initial_buy_sol", 0),
                "bonding_curve_pct": token.get("bonding_curve_pct", 0),
                "score":             score,   # use RAW score for the bucket key
            }
            rug_penalty = rug_memory.score_penalty(rug_features)
            if rug_penalty > 0:
                rug_matches = rug_memory.matched_count(rug_features)
                breakdown["rug_pattern_match"] = -rug_penalty
                token["rug_pattern_matches"] = rug_matches
                score -= rug_penalty

            token["score"]           = score
            token["score_breakdown"] = breakdown
            token["scored_at"]       = time.time()

            self.scored_count += 1

            curve_pct = token.get("bonding_curve_pct", 0)
            rug_note  = f" | RUG-MATCH ×{token.get('rug_pattern_matches', 0)} (-{rug_penalty})" if rug_penalty else ""
            fusion_note = ""
            if breakdown.get("fusion"):
                pats = ",".join(token.get("fusion_patterns", {}).keys())
                fusion_note = f" | FUSION {breakdown['fusion']:+d} ({pats})"
            logger.info(
                f"[SCORE] {symbol} | {mint[:8]}... | Score: {score}/100 | "
                f"Curve: {curve_pct:.1f}% | "
                f"MC: {token.get('market_cap_sol', 0):.1f}S | "
                f"Creator: {token.get('initial_buy_sol', 0):.2f}S"
                f"{fusion_note}"
                f"{rug_note}"
            )

            # Hard filters first
            if not self._passes_hard_filters(token):
                logger.debug(f"[FILTERED] {symbol}: {token.get('reject_reason')}")
                # Counterfactual: log this rejection so we can later check what
                # the token actually did. If our filters are filtering winners,
                # this surfaces it.
                counterfactual.record_rejection(token, token.get('reject_reason', 'hard_filter'))
                return

            # Score threshold (dynamic — auto_tuner shifts it on rolling WR)
            min_score = auto_tuner.effective_min_score()
            if score < min_score:
                token["reject_reason"] = f"score_{score}"
                # Bin by score band, not exact score, so aggregation is meaningful
                band = (score // 10) * 10
                counterfactual.record_rejection(token, f"score_band_{band}")

                # Smart Caller: borderline rejections (just below threshold)
                # get queued for manual review by the control bot.
                if SMART_CALLER_MIN <= score < min_score:
                    try:
                        with open("logs/candidate_queue.jsonl", "a", encoding="utf-8") as f:
                            f.write(json.dumps({
                                "ts":         time.time(),
                                "mint":       mint,
                                "symbol":     symbol,
                                "score":      score,
                                "init_buy":   token.get("initial_buy_sol", 0),
                                "curve_pct":  token.get("bonding_curve_pct", 0),
                                "creator":    (token.get("creator", "") or "")[:12],
                            }) + "\n")
                    except Exception as e:
                        logger.debug(f"[SMART CALLER] write fail: {e}")
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

                # Freeze-authority sensor: if non-null, the creator (or whoever
                # holds it) can freeze your token account post-buy — locking
                # you out of selling. Pump.fun mints typically have null freeze
                # authority; anything else is a clear rug-vector.
                if not await self._freeze_authority_safe(mint):
                    token["reject_reason"] = "freeze_authority_set"
                    logger.debug(f"[FILTERED] {symbol}: freeze authority set")
                    counterfactual.record_rejection(token, "freeze_authority_set")
                    return

            logger.success(f"[BUY SIGNAL] {symbol} scored {score}/100 — queuing")
            await self.trade_queue.put(token)

        except Exception as e:
            logger.error(f"Score error for {mint[:8]}: {e}")

    async def _freeze_authority_safe(self, mint: str) -> bool:
        """Returns True if the token's freeze authority is null (safe).

        Pump.fun mints normally have a null freeze authority. If it's set,
        whoever holds it can freeze your associated token account after the
        buy lands — preventing you from selling. Classic honeypot rug vector.
        Fail-open on RPC errors so a flaky RPC doesn't shut down trading.
        """
        try:
            payload = {
                "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                "params": [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            }
            async with self._rpc_session.post(
                RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=4)
            ) as r:
                data = await r.json()
            val = (data.get("result") or {}).get("value")
            if not val:
                return True  # account not visible yet — let it through
            info = ((val.get("data") or {}).get("parsed") or {}).get("info") or {}
            return info.get("freezeAuthority") in (None, "")
        except Exception:
            return True  # fail-open on RPC errors

    # ── Hard Filters ──────────────────────────────────────────────────────────
    def _passes_hard_filters(self, token: dict) -> bool:
        # Diurnal filter — skip dead hours (UTC). Cheapest reject, do first.
        if DEAD_HOURS_UTC is not None:
            hour_utc = datetime.datetime.now(datetime.UTC).hour
            start, end = DEAD_HOURS_UTC
            in_window = (start <= hour_utc < end) if start < end else (hour_utc >= start or hour_utc < end)
            if in_window:
                token["reject_reason"] = f"dead_hours_{hour_utc:02d}utc"
                return False

        # Regime filter — data-driven version of the dead-hours window. Pauses
        # buys when the trailing 60-min new-mint rate has collapsed relative to
        # the 24h median. Bootstrap-safe: no-op until enough history accrues.
        if regime_filter.should_pause():
            token["reject_reason"] = "regime_dead"
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

        # Hard filter on creator initial buy. Counterfactual analysis showed
        # ~91% rug rate for tokens with init buys >= MAX_INITIAL_BUY_SOL.
        # Reject before scoring even runs — these are bag-dump setups.
        initial_buy = token.get("initial_buy_sol", 0)
        if initial_buy >= MAX_INITIAL_BUY_SOL:
            token["reject_reason"] = f"big_init_buy_{initial_buy:.2f}sol"
            return False

        # Creator hard-blacklist (3+ trades, net negative, WR<25%)
        creator = token.get("creator", "")
        if creator and creator_tracker.is_blacklisted(creator):
            token["reject_reason"] = "creator_blacklisted"
            return False

        # Tier 4: noise-bot creator (Plan A 2026-05-10). Was: reject if creator
        # had ≥25 buys. Now: reject only if creator is *classified* noise — a
        # smart wallet at ≥25 buys is the signal we want, not a filter target.
        # Wallets with ≥25 buys but no outcome record yet (unknown class) keep
        # the legacy reject so we don't regress on uncharacterized bot pools.
        mint = token.get("mint", "")
        if creator and wallet_intel.is_bot_wallet(creator):
            cclass = wallet_intel.wallet_class(creator)
            if cclass == "noise" or cclass == "unknown":
                token["reject_reason"] = (
                    f"noise_creator_{wallet_intel.wallet_buys(creator)}"
                    if cclass == "noise"
                    else f"bot_creator_{wallet_intel.wallet_buys(creator)}"
                )
                return False
            # cclass == "smart": fall through; the smart-money bonus in
            # _compute_score will see this creator as a positive signal.

        # Tier 4: bundled launch. Was: reject if 5+ early buyers (coordination
        # signal). Now: only reject if fewer than 2 of those early buyers are
        # smart wallets — a bundle of smart money is curated, not coordinated.
        if mint and wallet_intel.is_bundled_launch(mint):
            n_smart = len(wallet_intel.smart_buyers_in_window(mint))
            if n_smart < 2:
                token["reject_reason"] = "bundled_launch"
                return False

        # Tier 4: known noise wallet was an early buyer. Was: any bot wallet
        # tripped the reject. Now: only reject if a noise wallet bought AND
        # no smart wallet bought — a smart wallet in the window outranks the
        # noise wallet's negative signal.
        if mint and wallet_intel.has_bot_buyer(mint):
            n_smart = len(wallet_intel.smart_buyers_in_window(mint))
            n_noise = len(wallet_intel.noise_buyers_in_window(mint))
            # Unknown bot-class wallets (no outcome record yet) still count as
            # noise for backward compatibility — they're what legacy is_bot
            # was matching against.
            if n_smart == 0 or n_noise > n_smart:
                token["reject_reason"] = "bot_buyer_in_window"
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

        # Symbol/name sanity check — DISABLED 2026-05-10 per
        # analytics/holdout_validation.md: no_symbol rejections had a 0.0%
        # rug rate on held-out data (vs 36.3% base). The filter was rejecting
        # mints that were actually fine and contributing nothing to risk
        # avoidance. Kept the code as a no-op comment for future audit
        # trail; remove on next cleanup if no longer informative.
        # if not token.get("symbol") or token.get("symbol") == "???":
        #     token["reject_reason"] = "no_symbol"
        #     return False

        # Already dumping hard
        price_change_5m = token.get("price_change_5m", 0)
        if price_change_5m < -30:
            token["reject_reason"] = f"dumping_{price_change_5m:.0f}pct"
            return False

        # Early-spike sensor: vertical pump on a fresh token = sniper-bot pattern.
        # Tracker reports mc_growth_pct between 2s polls. >80% growth in a 2s
        # window for a token under 60s old = thin pump, almost always followed
        # by a dump as the bots cycle out. Rejects the "pump room" pattern.
        pf = get_pumpfun_state(token.get("mint", ""))
        if pf:
            age_s = pf.get("tracker_age_s", 0)
            growth = pf.get("mc_growth_pct", 0)
            if 0 < age_s < 60 and growth > 80:
                token["reject_reason"] = f"early_spike_{growth:.0f}pct_in_{age_s:.0f}s"
                return False

        return True

    # ── Score Computation ─────────────────────────────────────────────────────
    def _compute_score(self, token: dict) -> tuple[int, dict]:
        breakdown = {}

        # ── Factor 1: Creator signal (0-25) ──────────────────────────────────
        # Combines on-chain initial buy + creator tracker leaderboard bonus
        initial_buy  = token.get("initial_buy_sol", 0)
        creator      = token.get("creator", "")
        creator_score = 0

        # INVERTED 2026-05-08 from counterfactual analysis: tokens with creator
        # initial buys >= 1.5 SOL rugged at ~91% rate; tokens with init buys
        # < 0.30 SOL produced the bulk of +100% pumps and 100% of moonshots.
        # Big initial buy = creator bag dump risk. Reward small organic buys.
        if   initial_buy <  0.10:        creator_score += 15
        elif initial_buy <  0.30:        creator_score += 12
        elif initial_buy <  0.60:        creator_score += 8
        elif initial_buy <  1.00:        creator_score += 4
        elif initial_buy <  2.00:        creator_score += 2
        else:                             creator_score -= 5

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

        # Smart-money bonus (Plan A 2026-05-10). Known-winning wallets buying
        # in the first 10s are the highest-signal entry we have. Cap at +15
        # so it can't single-handedly clear the factor-1 ceiling.
        mint = token.get("mint", "")
        if mint:
            smart_count = len(wallet_intel.smart_buyers_in_window(mint))
            if smart_count >= 2:
                creator_score += 15
                logger.debug(f"[SMART-MONEY] {symbol} mint={mint[:8]} +15 ({smart_count} smart buyers)")
            elif smart_count == 1:
                creator_score += 8
                logger.debug(f"[SMART-MONEY] {symbol} mint={mint[:8]} +8 (1 smart buyer)")
            # Smart creator (passed the noise reject) also bumps the score
            if creator and wallet_intel.is_smart_wallet(creator):
                creator_score += 8
                logger.debug(f"[SMART-MONEY] {symbol} smart creator={creator[:8]} +8")

            # Whale-buyer bonus — separate signal from smart-money. A whale
            # is a volume classifier ("real money decided this is worth a
            # position"); a smart wallet is a win-rate classifier. They can
            # overlap, but the bonuses are independent on purpose: when
            # both fire we WANT the score to reflect the conjunction.
            whale_count = int(token.get("whale_buyer_count", 0))
            whale_vol   = float(token.get("whale_buy_volume", 0))
            if whale_count >= 2 or whale_vol >= 3.0:
                creator_score += 10
                logger.debug(
                    f"[WHALE] {symbol} mint={mint[:8]} +10 "
                    f"({whale_count} whale buyers, {whale_vol:.2f} SOL)"
                )
            elif whale_count == 1:
                creator_score += 5
                logger.debug(
                    f"[WHALE] {symbol} mint={mint[:8]} +5 "
                    f"(1 whale buyer, {whale_vol:.2f} SOL)"
                )

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
        elif token.get("x_hype_match"):
            # X-feed match without a curated influencer hit — smaller
            # bonus since the source is generic X hype, not a tracked
            # account. The fusion engine compounds this further when it
            # aligns with smart-money or comment velocity.
            community_score += 4
            logger.debug(f"[X-HYPE] {symbol} x-monitor hype match +4")

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
