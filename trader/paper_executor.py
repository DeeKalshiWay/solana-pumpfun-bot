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

import os
import random
import time

from loguru import logger

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
# Dragged from each round-trip whether you win or lose.
NETWORK_FEE_SOL         = 0.0008  # ~$0.12 per round-trip

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
                "[PAPER] REALISTIC sim ON: "
                f"base slip {BASE_SLIPPAGE_PCT*100:.1f}%, "
                f"size coeff {SIZE_SLIPPAGE_COEFF}, "
                f"tx fail {TX_FAIL_RATE*100:.0f}%, "
                f"fee {NETWORK_FEE_SOL} SOL/leg"
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

        # Network fee gets eaten regardless of fill outcome
        self.wallet.deduct(NETWORK_FEE_SOL)

        # Tx failure: lost gas, no position
        if _will_tx_fail():
            logger.warning(
                f"[PAPER BUY FAILED] {symbol} | tx failed (RPC drop) | "
                f"-{NETWORK_FEE_SOL} SOL gas burned"
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
            self.wallet.credit(NETWORK_FEE_SOL)  # refund the gas if no fill
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
            f"| slip={slip*100:.1f}% mev={mev*100:.1f}% (curve={curve_sol:.0f} SOL)"
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

    # ── SELL ──────────────────────────────────────────────────────────────────
    async def sell(self, token_mint: str, token_amount_raw, reason: str = "exit") -> dict:
        # Network fee on exit too
        self.wallet.deduct(NETWORK_FEE_SOL)

        # Tx failure on sell — keeps position, loses gas, retries next tick
        if _will_tx_fail():
            logger.warning(
                f"[PAPER SELL FAILED] {token_mint[:8]} | reason={reason} | "
                f"tx failed | -{NETWORK_FEE_SOL} SOL gas burned"
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

        price     = self._prices.get(token_mint, 0)
        curve_sol = self._curve_sol.get(token_mint, DEFAULT_CURVE_SOL)

        if isinstance(token_amount_raw, str) and "%" in token_amount_raw:
            token_amount_raw = 0

        if price <= 0 or not (isinstance(token_amount_raw, int) and token_amount_raw > 0):
            logger.warning(f"[PAPER SELL 0FILL] {token_mint[:8]} | no price/amount")
            return {
                "success":      True, "venue": "paper", "type": "sell",
                "mint":         token_mint, "reason": reason,
                "sol_received": 0.0, "timestamp": time.time(),
            }

        # Estimate the SOL value of what we're selling at the displayed price
        gross_sol = token_amount_raw * price

        # Apply size-dependent slippage on the way out too
        slip = _slippage_for_buy(gross_sol, curve_sol)
        mev  = _mev_tax(gross_sol, curve_sol)
        late_tp_penalty = LATE_TP_EXIT_PENALTY if "800" in reason else 0.0
        total_drag = slip + mev + late_tp_penalty

        # On exits, slippage REDUCES sol received (vs increasing cost on entries)
        sol_received = gross_sol * (1 - total_drag)
        self.wallet.credit(sol_received)

        logger.success(
            f"[PAPER SELL] {token_mint[:8]} | reason={reason} | "
            f"{token_amount_raw:,} tokens -> {sol_received:.4f} SOL "
            f"(slip={slip*100:.1f}% mev={mev*100:.1f}%"
            f"{' +late' if late_tp_penalty else ''})"
        )

        # Cleanup entry tracking
        self._entry_slippage_taken.pop(token_mint, None)

        return {
            "success":      True,
            "signature":    f"PAPER_SELL_{token_mint[:8]}_{int(time.time())}",
            "type":         "sell",
            "mint":         token_mint,
            "reason":       reason,
            "sol_received": sol_received,
            "venue":        "paper",
            "timestamp":    time.time(),
        }
