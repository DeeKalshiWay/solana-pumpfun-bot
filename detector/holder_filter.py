"""
detector/holder_filter.py
Solana RPC helper that returns top-10 holder concentration for a mint.

If top 10 holders own > HOLDER_CONCENTRATION_LIMIT_PCT of supply, the token is
flagged as rug-prone and signal_scorer rejects it.

Calls getTokenLargestAccounts + getTokenSupply on the configured RPC.
Cached for 60s per mint to avoid hammering RPC for re-scored tokens.
"""

import asyncio
import time
import aiohttp
from loguru import logger
from config import RPC_URL, HOLDER_CONCENTRATION_LIMIT_PCT


_cache: dict = {}      # mint -> (timestamp, top10_pct)
_CACHE_TTL = 60        # seconds


async def get_top10_concentration(session: aiohttp.ClientSession, mint: str) -> float:
    """
    Returns the percentage of supply held by top 10 token accounts.
    Returns 0 on RPC failure (treat as unknown — don't reject).
    """
    now = time.time()
    cached = _cache.get(mint)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    try:
        # Fire both calls in parallel
        async with session.post(
            RPC_URL,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenLargestAccounts",
                "params": [mint, {"commitment": "confirmed"}],
            },
            timeout=aiohttp.ClientTimeout(total=4),
        ) as r1:
            largest = await r1.json()

        async with session.post(
            RPC_URL,
            json={
                "jsonrpc": "2.0", "id": 2,
                "method": "getTokenSupply",
                "params": [mint],
            },
            timeout=aiohttp.ClientTimeout(total=4),
        ) as r2:
            supply = await r2.json()

        accounts = largest.get("result", {}).get("value", []) or []
        total    = supply.get("result", {}).get("value", {}).get("amount")
        if not accounts or not total:
            return 0
        total_int = int(total)
        if total_int == 0:
            return 0

        # Sum top 10 by raw amount
        top10_amount = 0
        for acc in accounts[:10]:
            amt = acc.get("amount") or "0"
            try:
                top10_amount += int(amt)
            except (TypeError, ValueError):
                pass

        pct = (top10_amount / total_int) * 100
        _cache[mint] = (now, pct)
        return pct

    except asyncio.TimeoutError:
        return 0
    except Exception as e:
        logger.debug(f"[HOLDER] {mint[:8]} concentration check error: {e}")
        return 0


def concentration_too_high(pct: float) -> bool:
    """Apply the configured threshold."""
    if HOLDER_CONCENTRATION_LIMIT_PCT <= 0:
        return False
    return pct > HOLDER_CONCENTRATION_LIMIT_PCT
