"""
trader/paper_executor.py
Simulates trade execution without sending real transactions.
Uses pump.fun bonding curve math to calculate realistic token amounts.
"""

import time
from loguru import logger

SIMULATED_SLIPPAGE = 0.015  # 1.5% simulated slippage + fees


class PaperExecutor:
    def __init__(self, wallet):
        self.wallet = wallet
        self._prices: dict = {}  # mint -> SOL per raw token unit

    async def start(self):
        pass

    async def stop(self):
        pass

    def update_price(self, mint: str, price_per_raw: float):
        """Called by price monitor to keep current prices for paper sells."""
        self._prices[mint] = price_per_raw

    async def buy(self, token_mint: str, sol_amount: float, token: dict = None) -> dict:
        token = token or {}

        # Use bonding curve reserves for accurate price if available.
        # pump.fun API returns vTokensInBondingCurve in token units (not raw),
        # so multiply by 1e6 (6 decimals) to get SOL per raw unit.
        v_sol = token.get("v_sol_in_bonding", 0)
        v_tokens = token.get("v_tokens_in_bonding", 0)

        if v_sol > 0 and v_tokens > 0:
            price_per_raw = v_sol / (v_tokens * 1_000_000)
        else:
            # Fallback: market cap / total raw supply (1B tokens * 1e6 decimals)
            mc_sol = token.get("market_cap_sol") or 30.0
            price_per_raw = mc_sol / 1_000_000_000_000_000

        tokens_received = int((sol_amount / price_per_raw) * (1 - SIMULATED_SLIPPAGE))

        self._prices[token_mint] = price_per_raw
        self.wallet.deduct(sol_amount)

        symbol = token.get("symbol", token_mint[:8])
        logger.success(
            f"[PAPER BUY] {symbol} | {sol_amount} SOL -> {tokens_received:,} tokens "
            f"| price={price_per_raw:.2e} SOL/raw"
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

    async def sell(self, token_mint: str, token_amount_raw, reason: str = "exit") -> dict:
        price = self._prices.get(token_mint, 0)

        if isinstance(token_amount_raw, str) and "%" in token_amount_raw:
            # Percentage sells aren't used in paper mode — treat as 100%
            token_amount_raw = 0

        if price > 0 and isinstance(token_amount_raw, int) and token_amount_raw > 0:
            sol_received = token_amount_raw * price * (1 - SIMULATED_SLIPPAGE)
        else:
            sol_received = 0.0

        self.wallet.credit(sol_received)

        logger.success(
            f"[PAPER SELL] {token_mint[:8]} | reason={reason} "
            f"| {token_amount_raw:,} tokens -> {sol_received:.4f} SOL"
        )

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
