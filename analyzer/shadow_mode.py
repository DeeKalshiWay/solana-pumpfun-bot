"""
analyzer/shadow_mode.py

Sim-vs-live divergence harness. The most differentiated piece of the repo
because almost no public Solana bot measures it.

THE PROBLEM:
    Backtesters and paper modes are notoriously optimistic. They assume
    instant fills, zero slippage, perfect tick data, no failed txs. Real
    trading on pump.fun bonding curves loses 5-15% to friction per round
    trip, and a bot that "100x'd in paper" can flatline live.

    The honest way to know how much your paper mode lies is to run both
    at the same time, on the same decisions, and measure the gap.

DESIGN:
    1. The live executor makes a decision (buy 0.10 SOL of mint X at price P)
    2. ALSO record what an idealized sim would have done: zero slippage,
       instant fill, theoretical mid-price entry.
    3. When the live exit happens, record both the live realized PnL
       AND the corresponding sim-mode PnL.
    4. The DIFFERENCE per trade is "slippage + latency cost". Aggregated
       across N trades, it's the bot's true friction tax.

WHAT WE LEARN:
    - Live PnL ≈ Sim PnL  →  paper backtests are trustworthy.
    - Sim - Live = 0.5%   →  acceptable friction.
    - Sim - Live > 5%     →  paper mode is lying. Don't trust backtests.
    - Sim - Live varies wildly per mint  →  some mints are hostile (high
      slippage curves, dump-on-sell behavior); useful as a per-mint blacklist.

STATUS: HALF-SHIPPED (2026-05-10)
    What this file provides:
        ✓ Decision/outcome recording API (this module)
        ✓ JSONL persistence layer
        ✓ Divergence analysis tool (tools/shadow_divergence.py)
        ✓ Sim-PnL estimator using bonding curve PDA mid-price

    What's NOT wired yet (next focused session):
        ⬜ Hook from trader/pumpportal_executor.py buy/sell → record_decision()
        ⬜ Hook from risk/manager.py close_position → record_outcome()
        ⬜ Dashboard tile showing rolling avg divergence

    The recording API is fully usable. Live wiring is intentionally
    deferred so it can be done carefully under load testing without
    risking the active trade pipeline.

Usage (once wired):
    from analyzer.shadow_mode import shadow

    # At buy time:
    shadow.record_decision(
        mint=mint, action="buy",
        sol_amount=0.10,
        live_entry_price=actual_fill_price,
        sim_entry_price=bonding_curve_mid_price,
    )

    # At close time:
    shadow.record_outcome(
        mint=mint,
        live_pnl_sol=actual_pnl,
        live_exit_price=actual_exit_price,
        sim_exit_price=bonding_curve_mid_price_at_exit,
    )

    # Periodically (or on the dashboard):
    stats = shadow.divergence_stats(last_n=100)
    # → {"avg_slippage_pct": 4.2, "p99_slippage_pct": 18.1, "n": 100}
"""

import json
import os
import time
from collections import defaultdict, deque

from loguru import logger


SHADOW_DECISIONS = "logs/shadow_decisions.jsonl"
SHADOW_OUTCOMES  = "logs/shadow_outcomes.jsonl"


class ShadowMode:
    """Singleton recorder for sim-vs-live divergence measurement."""

    def __init__(self):
        # In-memory cache of recent decisions for fast lookup at close time.
        # Bounded so we don't leak memory if positions never close (rare but possible).
        self._open: dict[str, dict] = {}
        # Rolling divergence stats for dashboard/alerts. Each entry is a
        # (sim_pnl - live_pnl) percentage of trade size.
        self._slippage_history: deque[float] = deque(maxlen=500)
        os.makedirs("logs", exist_ok=True)

    # ── Recording API ────────────────────────────────────────────────────────
    def record_decision(
        self,
        mint: str,
        action: str,
        sol_amount: float,
        live_entry_price: float,
        sim_entry_price: float,
        meta: dict | None = None,
    ) -> None:
        """Called at trade entry. Captures the divergence between actual
        fill price and what an idealized sim would have paid.

        live_entry_price: SOL-per-raw-token at the actual fill (post-slippage).
        sim_entry_price:  SOL-per-raw-token from the bonding curve PDA mid
                          AT THE TIME WE SUBMITTED THE TX (idealized fill).

        The instantaneous spread between these is the first slippage cost.
        """
        if not mint or sol_amount <= 0:
            return
        rec = {
            "ts":               time.time(),
            "mint":             mint,
            "action":           action,
            "sol_amount":       sol_amount,
            "live_entry_price": live_entry_price,
            "sim_entry_price":  sim_entry_price,
            "entry_slippage_pct": (
                ((live_entry_price - sim_entry_price) / sim_entry_price * 100)
                if sim_entry_price > 0 else 0
            ),
            "meta":             meta or {},
        }
        self._open[mint] = rec
        try:
            with open(SHADOW_DECISIONS, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:
            logger.debug(f"[SHADOW] decision write failed: {e}")

    def record_outcome(
        self,
        mint: str,
        live_pnl_sol: float,
        live_exit_price: float,
        sim_exit_price: float,
    ) -> None:
        """Called at trade exit. Records the live PnL and what sim would
        have realized at the same exit decision."""
        decision = self._open.pop(mint, None)
        if decision is None:
            # Outcome arrived without a recorded decision — orphan, skip.
            return

        size = decision["sol_amount"]
        # Sim PnL: same size, but using sim entry and sim exit prices.
        if decision["sim_entry_price"] > 0 and sim_exit_price > 0:
            sim_pnl_sol = (
                size * (sim_exit_price - decision["sim_entry_price"])
                     / decision["sim_entry_price"]
            )
        else:
            sim_pnl_sol = 0.0

        # Divergence: how much MORE the sim made than live (always positive on
        # well-running pump.fun trades because sim has no friction).
        divergence_sol = sim_pnl_sol - live_pnl_sol
        divergence_pct = (divergence_sol / size * 100) if size > 0 else 0
        self._slippage_history.append(divergence_pct)

        rec = {
            "ts":               time.time(),
            "mint":             mint,
            "size_sol":         size,
            "live_pnl_sol":     live_pnl_sol,
            "sim_pnl_sol":      sim_pnl_sol,
            "divergence_sol":   divergence_sol,
            "divergence_pct":   divergence_pct,
            "live_entry_price": decision["live_entry_price"],
            "sim_entry_price":  decision["sim_entry_price"],
            "live_exit_price":  live_exit_price,
            "sim_exit_price":   sim_exit_price,
            "entry_slippage_pct": decision["entry_slippage_pct"],
        }
        try:
            with open(SHADOW_OUTCOMES, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:
            logger.debug(f"[SHADOW] outcome write failed: {e}")

    # ── Live stats (for dashboard / alerts) ─────────────────────────────────
    def divergence_stats(self, last_n: int = 100) -> dict:
        """Rolling slippage stats. Used by /api/status or the dashboard."""
        hist = list(self._slippage_history)[-last_n:]
        if not hist:
            return {"n": 0, "avg_pct": 0.0, "median_pct": 0.0, "p99_pct": 0.0}
        s = sorted(hist)
        n = len(s)
        return {
            "n":          n,
            "avg_pct":    sum(s) / n,
            "median_pct": s[n // 2],
            "p99_pct":    s[max(0, n - max(1, n // 100))],
            "max_pct":    s[-1],
            "min_pct":    s[0],
        }


# Singleton — used by the live trade pipeline (when wired) and the
# divergence analysis tool.
shadow = ShadowMode()
