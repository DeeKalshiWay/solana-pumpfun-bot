"""
trader/paper_executor.py

REALISTIC paper simulator. Models the friction that real money trading
faces and that the original 1.5%-flat sim was hiding.

Friction modeled:
  1. SIZE-DEPENDENT SLIPPAGE
     A 0.005 SOL trade barely moves the bonding curve (3-6%).
     A 1.78 SOL trade IS the price impact (15-30% slippage).
     Slippage scales with (your_size / available_curve_liquidity).

  2. MEV / SANDWICH TAX
     Liquid mints get sandwiched by other bots. Models a baseline tax
     on top of slippage — bigger when you're a bigger fish.

  3. TRANSACTION FAILURE RATE
     Public RPC ~10-15% fail rate; Helius ~3-5%. Failed buys mean lost
     gas with no fill. Modeled as a chance the trade just fails.

  4. NETWORK FEE
     Fixed Solana priority fee per round-trip — eats small trades
     disproportionately.

  5. EXIT DEGRADATION FOR LATE TPs
     The +800% TP exits in particular face degraded fills because the
     token is usually rolling over by then; selling INTO weakness
     compounds with sandwiching.

The new dashboard number will be 30-60% lower than the old optimistic sim.
That's the point — bring the paper number closer to what a live run would
actually produce.
"""

import math
import os
import random
import time

from loguru import logger

from config import (
    EXIT_LATENCY_ENABLED,
    EXIT_LATENCY_P50_S,
    EXIT_LATENCY_P99_S,
    PRIORITY_FEE_SOL,
    SELL_PRIORITY_FEE_SOL,
    STAMPEDE_MULT_STALL,
)

# Force-sell reasons that suffer from herd-exit stampede on Solana. When every
# other bot watching the same WS feed fires the same exit at the same moment,
# the realized fill happens at a stale price *and* with magnified size impact.
# Keep this list in sync with the reasons risk/manager._force_sell emits.
STAMPEDE_REASONS = frozenset({"momentum_stall", "no_movement", "time_exit"})


def _sample_exit_latency_s() -> float:
    """
    Lognormal latency sample with the configured p50/p99. Derived params:
      mu    = ln(p50)
      sigma = (ln(p99) - mu) / 2.326    # 2.326 = one-sided 99th-pct z-score
    Clamped to [0.05, 30] to avoid pathological extremes.
    """
    if not EXIT_LATENCY_ENABLED:
        return 0.0
    p50 = max(EXIT_LATENCY_P50_S, 0.05)
    p99 = max(EXIT_LATENCY_P99_S, p50 * 1.01)
    mu = math.log(p50)
    sigma = max((math.log(p99) - mu) / 2.326, 1e-4)
    return max(0.05, min(random.lognormvariate(mu, sigma), 30.0))


def _price_at_lookback(price_history, lookback_s: float, fallback: float) -> float:
    """
    Walk back through (ts, pnl_pct, price) tuples and return the price recorded
    closest to (now - lookback_s). If history is empty or pre-widening (only
    2 elements per tuple), return fallback. Robust to missing/old entries.
    """
    if not price_history:
        return fallback
    target_ts = time.time() - lookback_s
    best_price = None
    best_dt = float("inf")
    for entry in price_history:
        if len(entry) < 3:
            continue   # pre-widening tuple, no price stored
        ts, _pnl, price = entry[0], entry[1], entry[2]
        if price is None or price <= 0:
            continue
        dt = abs(ts - target_ts)
        if dt < best_dt:
            best_dt = dt
            best_price = price
    if best_price is None or best_price <= 0:
        return fallback
    return best_price

# ────────────────────────────────────────────────────────────────────────────
# Tunable realism knobs. Defaults reflect public RPC + small wallet against
# the modern (2026) pump.fun environment with active competing bots.
# Set REALISTIC_PAPER_SIM=0 in env to disable all friction (legacy behavior).
# ────────────────────────────────────────────────────────────────────────────
REALISTIC_PAPER_SIM = os.getenv("REALISTIC_PAPER_SIM", "1") == "1"

# Base slippage at tiny size (0.005 SOL on a 30 SOL curve)
BASE_SLIPPAGE_PCT       = 0.025   # 2.5% baseline (vs old 1.5%)

# Slippage that scales with size relative to bonding-curve liquidity.
# Roughly: a buy of N% of the curve costs ~0.6N% additional slippage.
SIZE_SLIPPAGE_COEFF     = 0.6

# MEV/sandwich tax — bigger trades attract more attention from competing bots.
# Modeled as additional drag scaled with trade size.
MEV_BASE_PCT            = 0.005   # 0.5% baseline
MEV_SIZE_COEFF          = 0.4     # additional, scales with curve fraction

# Probability that a transaction fails entirely (lost gas, no fill).
# Helius drops this from ~12% to ~4%.
TX_FAIL_RATE            = 0.05    # 5% — assumes Helius RPC

# Solana network fee per tx (priority fee + signature) in SOL.
# Legacy flat constant — kept for non-realistic mode + the tx-fail gas burn
# refund logic. Realistic mode uses the asymmetric live model below.
NETWORK_FEE_SOL         = 0.0008  # ~$0.12 per round-trip

# ── Live-calibrated priority fees ─────────────────────────────────────────
# Mirrors trader/pumpportal_executor.py exactly: buys scale at 5% of trade
# size with a 0.0005 SOL floor and PRIORITY_FEE_SOL cap; sells scale at 5%
# of position value with a SELL_PRIORITY_FEE_SOL cap (5x the buy cap).
# Asymmetric fees were the dominant unmodeled friction in paper — small
# trades that round-tripped fast paid 1x the cap on entry and 5x on exit.
SMALL_SELL_FEE_FALLBACK = 0.001  # used when position_value <= 0.1 SOL,
                                  # matches pumpportal_executor.py:58


def _buy_priority_fee(sol_amount: float) -> float:
    """Live-model buy priority fee: scales with trade size, capped + floored."""
    if not REALISTIC_PAPER_SIM:
        return NETWORK_FEE_SOL
    return min(PRIORITY_FEE_SOL, max(sol_amount * 0.05, 0.0005))


def _sell_priority_fee(position_value_sol: float) -> float:
    """Live-model sell priority fee: 5x the buy cap, scales with position value."""
    if not REALISTIC_PAPER_SIM:
        return NETWORK_FEE_SOL
    if position_value_sol > 0.1:
        return min(SELL_PRIORITY_FEE_SOL, position_value_sol * 0.05)
    return min(SELL_PRIORITY_FEE_SOL, SMALL_SELL_FEE_FALLBACK)

# Late-TP penalty: by the time +800% TP fires the token is often rolling
# over and exits get worse fills than entries.
LATE_TP_EXIT_PENALTY    = 0.05    # additional 5% on exits at +800%+

# Default bonding-curve "size" if we can't compute one (in SOL terms)
DEFAULT_CURVE_SOL       = 30.0


def _slippage_for_buy(sol_amount: float, curve_sol: float) -> float:
    """
    Returns total slippage fraction for a buy.
    Slippage = base + (your_fraction_of_curve × coefficient).
    """
    if not REALISTIC_PAPER_SIM:
        return 0.015   # legacy

    # Fraction of the curve we're consuming
    your_frac = min(sol_amount / max(curve_sol, 5.0), 1.0)
    size_drag = your_frac * SIZE_SLIPPAGE_COEFF
    return BASE_SLIPPAGE_PCT + size_drag


def _mev_tax(sol_amount: float, curve_sol: float) -> float:
    """Sandwich-attack drag, scales with trade size."""
    if not REALISTIC_PAPER_SIM:
        return 0.0
    your_frac = min(sol_amount / max(curve_sol, 5.0), 1.0)
    return MEV_BASE_PCT + (your_frac * MEV_SIZE_COEFF)


def _will_tx_fail() -> bool:
    """Returns True with TX_FAIL_RATE probability."""
    if not REALISTIC_PAPER_SIM:
        return False
    return random.random() < TX_FAIL_RATE


class PaperExecutor:
    def __init__(self, wallet):
        self.wallet = wallet
        self._prices: dict = {}              # mint -> SOL per raw token unit
        self._curve_sol: dict = {}           # mint -> bonding curve size at entry
        # Pre-deducted entry slippage stored for the matching exit
        self._entry_slippage_taken: dict = {}

    async def start(self):
        if REALISTIC_PAPER_SIM:
            logger.warning(
                "[PAPER] REALISTIC sim ON (live-calibrated): "
                f"base slip {BASE_SLIPPAGE_PCT*100:.1f}%, "
                f"size coeff {SIZE_SLIPPAGE_COEFF}, "
                f"tx fail {TX_FAIL_RATE*100:.0f}%, "
                f"buy fee ≤{PRIORITY_FEE_SOL} SOL · sell fee ≤{SELL_PRIORITY_FEE_SOL} SOL"
            )
        else:
            logger.info("[PAPER] Legacy 1.5%-flat slippage sim")

    async def stop(self):
        pass

    def update_price(self, mint: str, price_per_raw: float):
        """Called by price monitor to keep current prices for paper sells."""
        self._prices[mint] = price_per_raw

    # ── BUY ───────────────────────────────────────────────────────────────────
    async def buy(self, token_mint: str, sol_amount: float, token: dict = None) -> dict:
        token = token or {}
        symbol = token.get("symbol", token_mint[:8])

        # Priority fee scaled to trade size, matching live pumpportal_executor.
        buy_fee = _buy_priority_fee(sol_amount)
        # Network fee gets eaten regardless of fill outcome
        self.wallet.deduct(buy_fee)

        # Tx failure: lost gas, no position
        if _will_tx_fail():
            logger.warning(
                f"[PAPER BUY FAILED] {symbol} | tx failed (RPC drop) | "
                f"-{buy_fee:.4f} SOL gas burned"
            )
            return {
                "success":         False,
                "error":           "tx_failed",
                "venue":           "paper",
                "type":            "buy",
                "mint":            token_mint,
                "timestamp":       time.time(),
            }

        # Bonding-curve price math
        v_sol = float(token.get("v_sol_in_bonding", 0))
        v_tokens = float(token.get("v_tokens_in_bonding", 0))
        if v_sol > 0 and v_tokens > 0:
            price_per_raw = v_sol / (v_tokens * 1_000_000)
            curve_sol = max(v_sol, 5.0)
        else:
            mc_sol = token.get("market_cap_sol") or DEFAULT_CURVE_SOL
            price_per_raw = mc_sol / 1_000_000_000_000_000
            curve_sol = max(float(mc_sol), 5.0)

        # Real-world friction
        slip = _slippage_for_buy(sol_amount, curve_sol)
        mev  = _mev_tax(sol_amount, curve_sol)
        total_drag = slip + mev

        # Effective entry price is INFLATED by slippage + MEV
        effective_price = price_per_raw * (1 + total_drag)
        tokens_received = int(sol_amount / effective_price)

        if tokens_received <= 0:
            self.wallet.credit(buy_fee)          # refund the gas if no fill
            logger.warning(f"[PAPER BUY 0FILL] {symbol} | curve too small")
            return {
                "success": False, "error": "zero_fill", "venue": "paper",
                "type": "buy", "mint": token_mint, "timestamp": time.time(),
            }

        self._prices[token_mint]      = price_per_raw
        self._curve_sol[token_mint]   = curve_sol
        self._entry_slippage_taken[token_mint] = total_drag
        self.wallet.deduct(sol_amount)

        logger.success(
            f"[PAPER BUY] {symbol} | {sol_amount:.4f} SOL -> {tokens_received:,} tokens "
            f"| slip={slip*100:.1f}% mev={mev*100:.1f}% fee={buy_fee:.4f} (curve={curve_sol:.0f} SOL)"
        )

        return {
            "success":         True,
            "signature":       f"PAPER_{token_mint[:8]}_{int(time.time())}",
            "type":            "buy",
            "mint":            token_mint,
            "sol_spent":       sol_amount,
            "tokens_expected": tokens_received,
            "venue":           "paper",
            "timestamp":       time.time(),
        }

    async def prebuild_sell_tx(self, token_mint: str) -> bytes | None:
        """Paper mode: no real tx to build. Compat shim so risk_manager's
        prebuild fast-path doesn't AttributeError when paper mode is on."""
        return None

    # ── SELL ──────────────────────────────────────────────────────────────────
    async def sell(self, token_mint: str, token_amount_raw, reason: str = "exit",
                   prebuilt_tx: bytes | None = None,
                   price_history: list | None = None) -> dict:
        # prebuilt_tx is ignored in paper mode (no real tx). Param exists so
        # the kwarg from risk_manager._force_sell doesn't crash.
        # price_history is consumed only on stall-class exits (see below).

        # Sell-side priority fee, scaled to the position's notional value.
        # In live this caps at 5x the buy cap because exit urgency on a rug
        # pays for itself — paper now models the same asymmetry.
        price_now_for_fee = self._prices.get(token_mint, 0)
        if isinstance(token_amount_raw, str) and "%" in token_amount_raw:
            pos_value = 0.0   # full %-exit; will resolve below, fee uses 0 (small-fallback)
        else:
            pos_value = float(token_amount_raw) * price_now_for_fee
        sell_fee = _sell_priority_fee(pos_value)
        self.wallet.deduct(sell_fee)

        # Tx failure on sell — keeps position, loses gas, retries next tick
        if _will_tx_fail():
            logger.warning(
                f"[PAPER SELL FAILED] {token_mint[:8]} | reason={reason} | "
                f"tx failed | -{sell_fee:.4f} SOL gas burned"
            )
            return {
                "success":      False,
                "error":        "tx_failed",
                "venue":        "paper",
                "type":         "sell",
                "mint":         token_mint,
                "reason":       reason,
                "timestamp":    time.time(),
            }

        price_now = self._prices.get(token_mint, 0)
        curve_sol = self._curve_sol.get(token_mint, DEFAULT_CURVE_SOL)

        if isinstance(token_amount_raw, str) and "%" in token_amount_raw:
            token_amount_raw = 0

        if price_now <= 0 or not (isinstance(token_amount_raw, int) and token_amount_raw > 0):
            logger.warning(f"[PAPER SELL 0FILL] {token_mint[:8]} | no price/amount")
            return {
                "success":      True, "venue": "paper", "type": "sell",
                "mint":         token_mint, "reason": reason,
                "sol_received": 0.0, "timestamp": time.time(),
            }

        # ── Latency-honest exit pricing ──────────────────────────────────────
        # On stall-class force-sells, our paper fill no longer happens at the
        # latest tick. By the time the tx confirms (200ms-3s), every other bot
        # watching the same WS has also fired this exit; the price decays.
        # Sample a price from `latency_s` ago, clamped to ≤ price_now: a stall
        # often fires at a local peak, and using a strictly-higher past price
        # would make the patch *help* the realized side, which is wrong-signed.
        # The lookback can only WORSEN the fill vs the optimistic baseline.
        # Outside the stampede reasons (TP ladder, trailing stop), the latest
        # tick is a fine approximation — those exits aren't herd-correlated.
        is_stampede = reason in STAMPEDE_REASONS and EXIT_LATENCY_ENABLED
        latency_s = _sample_exit_latency_s() if is_stampede else 0.0
        if is_stampede:
            hist_price = _price_at_lookback(price_history, latency_s, fallback=price_now)
            exec_price = min(hist_price, price_now)
            stampede_mult = STAMPEDE_MULT_STALL
        else:
            exec_price = price_now
            stampede_mult = 1.0

        gross_sol            = token_amount_raw * exec_price
        gross_sol_optimistic = token_amount_raw * price_now

        # Apply size-dependent slippage on the way out too. Clamp total drag
        # to <=0.95 — without this, very large exits (or stampede-multiplied
        # exits on tight curves) yield NEGATIVE sol_received, which is a math
        # underflow rather than a realistic outcome. A real fill would just be
        # very bad, not literally pay-to-sell.
        slip_base = _slippage_for_buy(gross_sol, curve_sol)
        slip      = slip_base * stampede_mult
        mev       = _mev_tax(gross_sol, curve_sol)
        late_tp_penalty = LATE_TP_EXIT_PENALTY if "800" in reason else 0.0
        total_drag = min(slip + mev + late_tp_penalty, 0.95)

        # On exits, slippage REDUCES sol received (vs increasing cost on entries)
        sol_received = gross_sol * (1 - total_drag)
        self.wallet.credit(sol_received)

        # Counterfactual: what we'd have credited under the OLD model
        # (latest tick price, no stampede multiplier). Same MEV / late-TP
        # penalty so the diff isolates the latency+stampede effect. Same clamp.
        opt_total_drag      = min(slip_base + mev + late_tp_penalty, 0.95)
        sol_received_optim  = gross_sol_optimistic * (1 - opt_total_drag)

        if is_stampede:
            logger.success(
                f"[PAPER SELL/STAMPEDE] {token_mint[:8]} | reason={reason} | "
                f"{token_amount_raw:,} tokens -> {sol_received:.4f} SOL "
                f"(was {sol_received_optim:.4f} pre-latency) | "
                f"slip {slip_base*100:.1f}%×{stampede_mult:.1f}={slip*100:.1f}% "
                f"mev={mev*100:.1f}% lat={latency_s:.2f}s"
            )
        else:
            logger.success(
                f"[PAPER SELL] {token_mint[:8]} | reason={reason} | "
                f"{token_amount_raw:,} tokens -> {sol_received:.4f} SOL "
                f"(slip={slip*100:.1f}% mev={mev*100:.1f}% fee={sell_fee:.4f}"
                f"{' +late' if late_tp_penalty else ''})"
            )

        # Cleanup entry tracking
        self._entry_slippage_taken.pop(token_mint, None)

        return {
            "success":               True,
            "signature":             f"PAPER_SELL_{token_mint[:8]}_{int(time.time())}",
            "type":                  "sell",
            "mint":                  token_mint,
            "reason":                reason,
            "sol_received":          sol_received,
            "sol_received_optimistic": sol_received_optim if is_stampede else None,
            "exit_latency_s":        latency_s if is_stampede else None,
            "venue":                 "paper",
            "timestamp":             time.time(),
        }
