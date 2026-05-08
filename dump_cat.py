"""
One-off: sell 100% of an orphaned pump.fun position.

Used after the 2026-05-07 token-resolver bug left a CAT buy unregistered
in the bot's risk manager. Mirrors how main.py wires the live executor.
"""

import asyncio
import sys

from loguru import logger

from trader.executor import TradeExecutor
from trader.wallet import SolanaWallet

MINT = "3x5SiC8P47EzGAD9N7hzeUBc5JbWBW4GG7EuEQTgpump"


async def main():
    sys.stdout.reconfigure(encoding="utf-8")
    wallet = SolanaWallet()
    await wallet.start()
    executor = TradeExecutor(wallet.keypair)
    await executor.start()

    sol_before = await wallet.get_sol_balance()
    bal_before = await wallet.get_token_balance(MINT)
    logger.info(f"Pre-sell: {sol_before:.6f} SOL liquid | {bal_before:,.0f} {MINT[:8]}")

    result = await executor.sell(MINT, "100%", reason="manual_dump")
    logger.info(f"Sell result: success={result.get('success')} sig={(result.get('signature') or '')[:24]}")
    if not result.get("success"):
        logger.error(f"Dump failed: {result}")

    # Wait for indexer + show post-balance
    await asyncio.sleep(4)
    sol_after = await wallet.get_sol_balance()
    bal_after = await wallet.get_token_balance(MINT)
    delta_sol = sol_after - sol_before
    logger.success(
        f"Post-sell: {sol_after:.6f} SOL liquid (Δ {delta_sol:+.6f}) | "
        f"{bal_after:,.0f} {MINT[:8]} remaining"
    )

    await wallet.stop()


if __name__ == "__main__":
    asyncio.run(main())
