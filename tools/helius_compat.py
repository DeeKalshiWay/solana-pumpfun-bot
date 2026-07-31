"""
tools/helius_compat.py

Drop-in replacement for the Helius Enhanced `/v0/addresses/{addr}/transactions`
REST endpoint, served from free public Solana RPCs.

Why this exists: the paid Helius key is out of credit. The polling follower
(`copy_follower.py`) called the Enhanced REST endpoint to get pre-parsed
swap data for a wallet. Free public RPCs only expose raw JSON-RPC, so we:

  1. `getSignaturesForAddress(addr, limit)` via rpc_pool       (signature list)
  2. For each signature: `getTransaction(sig)` via rpc_pool    (raw tx)
  3. `parse_raw_to_enhanced(raw)` from raw_tx_parser           (Enhanced shape)

Returns the same shape callers already consume — feePayer, source, type,
accountData, tokenTransfers — so copy_follower.py needs only a 1-line swap.

A tiny in-process LRU caches enriched transactions by signature so subsequent
polls of the same wallet don't re-fetch already-seen txs (free RPC budget is
precious).
"""

from __future__ import annotations

import time
from collections import OrderedDict

from tools.raw_tx_parser import parse_raw_to_enhanced
from tools.rpc_pool import get_signatures, get_transaction

# Bounded sig->Enhanced cache. Keeps memory in check across long runs.
_TX_CACHE: "OrderedDict[str, dict | None]" = OrderedDict()
_TX_CACHE_CAP = 4096


def _cache_get(sig: str):
    if sig in _TX_CACHE:
        # mark MRU
        _TX_CACHE.move_to_end(sig)
        return _TX_CACHE[sig]
    return None


def _cache_put(sig: str, val: dict | None):
    _TX_CACHE[sig] = val
    _TX_CACHE.move_to_end(sig)
    if len(_TX_CACHE) > _TX_CACHE_CAP:
        _TX_CACHE.popitem(last=False)


def get_address_transactions(addr: str, limit: int = 25) -> list[dict]:
    """Return up to `limit` recent transactions for `addr` in Enhanced shape.

    Mirrors Helius `/v0/addresses/{addr}/transactions?limit=N`. Skips any
    transaction that isn't a pump.fun-family swap (parse_raw_to_enhanced
    returns None for those) — matches the Enhanced endpoint's filter behavior
    closely enough for our follower.

    Sorted newest-first (same as Helius Enhanced).
    """
    sigs = get_signatures(addr, limit=limit)
    if not sigs:
        return []
    out: list[dict] = []
    for sig_obj in sigs:
        sig = sig_obj.get("signature")
        if not sig:
            continue
        # Skip failed txs — they wouldn't have meaningful balance changes.
        if sig_obj.get("err"):
            continue

        cached = _cache_get(sig)
        if cached is not None:
            out.append(cached)
            continue
        if sig in _TX_CACHE:
            # Cached None (non-pump-fun) — skip silently.
            continue

        raw = get_transaction(sig)
        if not raw:
            # Don't poison cache on transient RPC failure — let next poll retry
            continue
        enhanced = parse_raw_to_enhanced(raw)
        _cache_put(sig, enhanced)
        if enhanced is None:
            continue
        # Add the block time from the sig listing if parser didn't populate
        if not enhanced.get("timestamp"):
            enhanced["timestamp"] = sig_obj.get("blockTime")
        out.append(enhanced)
    return out


if __name__ == "__main__":
    # Smoke: pull a roster wallet's recent pump.fun swaps via free RPC.
    W = "EW1BMaF3AUnu9anjUmu8p3EY5F33ZhMESi7V2DJHNgNw"
    t0 = time.time()
    txs = get_address_transactions(W, limit=10)
    print(f"{W[:12]} -> {len(txs)} pump.fun swaps from {t0:.0f} ({time.time()-t0:.1f}s)")
    for t in txs[:5]:
        print(f"  {t.get('type'):>6} {t.get('source')}  "
              f"feePayer={(t.get('feePayer') or '')[:12]}  "
              f"transfers={len(t.get('tokenTransfers') or [])}")
