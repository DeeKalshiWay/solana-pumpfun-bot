"""
analyzer/counterfactual.py

Tracks what happens to tokens we REJECTED.

For every rejection (hard filter or score-too-low), we record the reject reason
and the token's market cap at reject time. 10 minutes later, we poll DexScreener
for the current market cap and write an outcome row.

After a few thousand rejections, this answers the most important question
about the bot's edge:

    "Which filters are pruning rugs, and which are killing winners?"

If `whale_init` rejections show an average +50% MC move 10 min later, the
filter is killing winners and should be relaxed. If `score_too_low` rejections
are net -40%, the score threshold is doing its job.

Output: logs/counterfactual.jsonl (append-only, one row per resolved rejection)
"""

import asyncio
import json
import os
import time
from collections import defaultdict

import aiohttp
from loguru import logger

from analyzer.rug_memory import RUG_PNL_THRESHOLD, rug_memory
from detector.wallet_intel import wallet_intel

REJECT_QUEUE_FILE = "logs/counterfactual_pending.jsonl"  # not strictly needed but good for restart safety
OUTCOME_FILE      = "logs/counterfactual.jsonl"
RESOLVE_DELAY_SEC = 600    # poll outcome 10 min after rejection
POLL_INTERVAL_SEC = 30     # how often the background loop checks for due entries


class CounterfactualLogger:
    """
    Tracks rejected-token outcomes. Single instance, shared across the
    signal_scorer (which feeds it) and a background task (which resolves
    pending entries).
    """

    def __init__(self):
        self.running = False
        # Pending: list of dicts with mint/reason/ts/mc_at_reject
        # In-memory only — if the bot dies mid-window we lose those, that's OK
        self._pending: list = []
        self._session: aiohttp.ClientSession | None = None
        os.makedirs("logs", exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────────
    def record_rejection(self, token: dict, reason: str):
        """Called by signal_scorer for every rejected token."""
        mint = token.get("mint", "")
        if not mint:
            return
        self._pending.append({
            "mint":             mint,
            "symbol":           token.get("symbol", "???"),
            "reason":           reason,
            "score":            int(token.get("score", 0)),
            # Raw (pre-rug-penalty) score so a rug feed-through into
            # rug_memory uses the SAME bucket key as scorer lookups.
            "raw_score":        int(token.get("raw_score", token.get("score", 0))),
            "ts":               time.time(),
            "mc_at_reject_sol": float(token.get("market_cap_sol", 0)),
            "initial_buy_sol":  float(token.get("initial_buy_sol", 0)),
            "curve_pct":        float(token.get("bonding_curve_pct", 0)),
            "creator":          token.get("creator", ""),
        })

    # ── Background resolver ──────────────────────────────────────────────────
    async def run(self):
        self.running = True
        self._session = aiohttp.ClientSession()
        logger.info(
            f"Counterfactual logger started — polls outcomes "
            f"{RESOLVE_DELAY_SEC // 60} min after each rejection"
        )
        try:
            while self.running:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                await self._resolve_due()
        finally:
            if self._session:
                await self._session.close()

    def stop(self):
        self.running = False

    async def _resolve_due(self):
        now = time.time()
        due = [r for r in self._pending if (now - r["ts"]) >= RESOLVE_DELAY_SEC]
        if not due:
            return
        # Remove due entries from pending up front so we don't double-resolve
        self._pending = [r for r in self._pending if (now - r["ts"]) < RESOLVE_DELAY_SEC]
        for entry in due:
            try:
                outcome = await self._build_outcome(entry)
                self._append(outcome)

                # Smart-money attribution: walk the mint's early-buyer set
                # (persisted by wallet_intel at finalize_bundle) and credit
                # each wallet with this mc_delta_pct outcome. Reclassifies on
                # boundary crossings so a wallet can flip smart/noise mid-run.
                try:
                    wallet_intel.attribute_outcome(
                        outcome["mint"], outcome.get("mc_delta_pct", 0)
                    )
                except Exception as e:
                    logger.debug(f"[CF] wallet_intel.attribute_outcome err: {e}")

                # Passive rug-memory feed: if this rejected token went on to
                # rug after rejection, record its feature signature into the
                # rug_memory pattern store. Lets the bot keep LEARNING from
                # the market even when wallet is unfunded / not trading.
                #
                # Threshold: same as live trades — pnl_pct <= -50%. We don't
                # have a SOL loss to gate on (we never bought), so this is
                # purely market-cap-derived.
                if outcome.get("mc_delta_pct", 0) <= RUG_PNL_THRESHOLD:
                    rug_memory.record_rug(
                        token_features = {
                            "initial_buy_sol":   entry.get("initial_buy_sol", 0),
                            "bonding_curve_pct": entry.get("curve_pct", 0),
                            "score":             entry.get("raw_score", entry.get("score", 0)),
                        },
                        pnl_pct      = outcome["mc_delta_pct"],
                        hold_minutes = (outcome["resolved_ts"] - entry["ts"]) / 60,
                        mint         = entry["mint"],
                        symbol       = entry.get("symbol", "???"),
                    )
            except Exception as e:
                logger.debug(f"[CF] resolve error for {entry['mint'][:8]}: {e}")
        # Cap memory if pending grows unreasonably
        if len(self._pending) > 5000:
            self._pending = self._pending[-5000:]

    async def _build_outcome(self, entry: dict) -> dict:
        current_mc = await self._fetch_mc_sol(entry["mint"])
        mc_at      = entry["mc_at_reject_sol"]
        delta_pct  = ((current_mc - mc_at) / mc_at * 100) if mc_at > 0 else 0
        return {
            "mint":             entry["mint"],
            "symbol":           entry["symbol"],
            "reason":           entry["reason"],
            "score":            entry["score"],
            "reject_ts":        entry["ts"],
            "resolved_ts":      time.time(),
            "mc_at_reject_sol": mc_at,
            "mc_now_sol":       round(current_mc, 4),
            "mc_delta_pct":     round(delta_pct, 2),
            "creator":          entry["creator"],
            "initial_buy_sol":  entry["initial_buy_sol"],
            "curve_pct":        entry["curve_pct"],
        }

    async def _fetch_mc_sol(self, mint: str) -> float:
        """DexScreener fallback — works for graduated and pump.fun tokens once tracked."""
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                if r.status != 200:
                    return 0
                data = await r.json()
                pairs = data.get("pairs") or []
                if not pairs:
                    return 0
                # Highest-liquidity pair on solana
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                if not sol_pairs:
                    return 0
                best = max(sol_pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0))
                # marketCap is in USD; convert to SOL using ~150 USD/SOL approximation
                # (rough but fine for relative comparisons)
                mc_usd = float(best.get("marketCap", 0) or 0)
                return mc_usd / 150.0 if mc_usd > 0 else 0
        except Exception:
            return 0

    def _append(self, row: dict):
        try:
            with open(OUTCOME_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            # Auto-rotate: if file > 5 MB, keep only the most recent 50% of lines.
            # Prevents unbounded growth overnight.
            if os.path.getsize(OUTCOME_FILE) > 5 * 1024 * 1024:
                self._rotate_outcomes()
        except Exception as e:
            logger.debug(f"[CF] append error: {e}")

    def _rotate_outcomes(self):
        """Trim outcomes file to half size to bound disk usage."""
        try:
            with open(OUTCOME_FILE, encoding="utf-8") as f:
                lines = f.readlines()
            keep = lines[len(lines) // 2:]
            tmp = OUTCOME_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(tmp, OUTCOME_FILE)
            logger.info(f"[CF] rotated outcomes: kept {len(keep)} of {len(lines)} rows")
        except Exception as e:
            logger.warning(f"[CF] rotation failed: {e}")

    # ── Aggregation ──────────────────────────────────────────────────────────
    def aggregate(self) -> dict:
        """
        Group all resolved outcomes by reject_reason and compute average,
        median, hit-rate (% that went up >50%), and total count.
        """
        if not os.path.exists(OUTCOME_FILE):
            return {"reasons": [], "total": 0}

        rows = []
        try:
            with open(OUTCOME_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            return {"reasons": [], "total": 0}

        groups: dict = defaultdict(list)
        for r in rows:
            groups[r.get("reason", "unknown")].append(r.get("mc_delta_pct", 0))

        out = []
        for reason, deltas in groups.items():
            if not deltas:
                continue
            srt = sorted(deltas)
            mid = len(srt) // 2
            median = srt[mid] if len(srt) % 2 else (srt[mid-1] + srt[mid]) / 2
            avg = sum(deltas) / len(deltas)
            hits = sum(1 for d in deltas if d >= 50)   # +50% counts as a winner
            big_losers = sum(1 for d in deltas if d <= -30)
            out.append({
                "reason":     reason,
                "count":      len(deltas),
                "avg_pct":    round(avg, 2),
                "median_pct": round(median, 2),
                "hit_rate":   round(hits / len(deltas) * 100, 1),     # % >+50%
                "rug_rate":   round(big_losers / len(deltas) * 100, 1),  # % <-30%
            })
        # Sort by count descending
        out.sort(key=lambda x: x["count"], reverse=True)
        return {"reasons": out, "total": len(rows)}


# Singleton — imported by signal_scorer and main
counterfactual = CounterfactualLogger()
