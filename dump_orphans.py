"""
One-off: sell 100% of multiple orphaned pump.fun positions.

Used after the 2026-05-07 emergency-stop bug closed positions in the
risk manager without successfully sending the sell tx. Mirrors how
main.py wires the live executor.
"""

import asyncio
import sys

from loguru import logger

from trader.executor import TradeExecutor
from trader.wallet import SolanaWallet

ORPHANS = [
    ("Antlion", "3hiaU86wP3U3gAUoHRSygicXsB3EPkgZRYf6Uew3pump"),  # +183% — sell first
    ("Arecibo", "FLUdU8d4r4FwT6EUCp7i2e1TF6Mb9GTen88tYkLppump"),  # -5%
]


async def main():
    sys.stdout.reconfigure(encoding="utf-8")
    wallet = SolanaWallet()
    await wallet.start()
    executor = TradeExecutor(wallet.keypair)
    await executor.start()

    sol_start = await wallet.get_sol_balance()
    logger.info(f"Pre-dump SOL: {sol_start:.6f}")

    for sym, mint in ORPHANS:
        bal = await wallet.get_token_balance(mint)
        logger.info(f"--- {sym} ({mint[:8]}…) | holding {bal:,.0f} ---")
        if bal <= 0:
            logger.info(f"  {sym}: nothing to sell, skipping")
            continue
        result = await executor.sell(mint, "100%", reason=f"manual_dump_{sym}")
        logger.info(f"  {sym} sell: success={result.get('success')} sig={(result.get('signature') or '')[:24]}")
        # Brief pause between sells so the second tx doesn't race the first's account-state update
        await asyncio.sleep(2)

    await asyncio.sleep(4)
    sol_end = await wallet.get_sol_balance()
    logger.success(
        f"Post-dump SOL: {sol_end:.6f} (Δ {sol_end - sol_start:+.6f})"
    )

    await wallet.stop()


if __name__ == "__main__":
    asyncio.run(main())
