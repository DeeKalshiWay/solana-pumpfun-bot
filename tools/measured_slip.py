"""
tools/measured_slip.py

Real-slip measurement for the streaming follower.

The legacy model: `our_entry = their_entry × (1 + ENTRY_LAG_PCT/100)` with
ENTRY_LAG_PCT = 5.0. Every trade got a flat modeled 5% adverse slip.

This module replaces the model with a *measurement*: after the wallet's buy
lands, we pull the next swap that hits the same mint from Helius Enhanced,
extract the effective price from that swap, and treat THAT as our entry.

Math: when somebody else buys on the bonding curve right after the proven
wallet, the price they pay is the price we'd pay if we were that buyer — i.e.
a fast HFT racing the same signal. The first post-wallet swap is the
empirical answer to "what would WE have filled at".

If no post-wallet swap is available yet (the wallet's buy is the most recent
trade), we fall back to the modeled slip. The effective fill price is the
worse (higher) of (modeled, measured) — conservative.

Usage:
    from tools.measured_slip import effective_entry_price
    fill_price, slip_pct, source = effective_entry_price(
        mint, their_price, their_ts, helius_key, modeled_lag_pct=5.0,
    )
"""

from __future__ import annotations

import json
import time
import urllib.request

WSOL = "So11111111111111111111111111111111111111112"

# Cache: mint -> (cached_ts, [list of recent swaps newest-first])
# Avoids hammering Helius on rapid-fire trades in the same mint.
_MINT_SWAP_CACHE: dict = {}
_CACHE_TTL_S = 5.0


def _fetch_mint_swaps(mint: str, key: str, limit: int = 5) -> list:
    """Get recent SWAPs on a mint from Helius Enhanced, newest-first.

    Tiny in-process cache to avoid repeated calls on rapid same-mint activity.
    """
    cached = _MINT_SWAP_CACHE.get(mint)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_S:
        return cached[1]
    url = (f"https://api.helius.xyz/v0/addresses/{mint}/transactions"
           f"?api-key={key}&limit={limit}&type=SWAP")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "slip-meas"}),
            timeout=8,
        ) as r:
            txs = json.load(r)
        _MINT_SWAP_CACHE[mint] = (time.time(), txs)
        return txs
    except Exception:
        return []


def _price_from_swap(tx: dict, mint: str) -> float | None:
    """Effective price in SOL per token from a single swap tx.

    For a buy: SOL out / tokens in.  For a sell: SOL in / tokens out.
    Either way it's sol_flow / token_flow on the mint side.
    """
    sol_change = 0.0
    tok_change = 0.0
    for tt in tx.get("tokenTransfers", []) or []:
        if tt.get("mint") == mint:
            tok_change += abs(float(tt.get("tokenAmount") or 0))
    # Sum |sol delta| across all accounts other than fee_payer noise — simpler
    # to just use the wallet that's the counterparty (whoever moved SOL).
    for ad in tx.get("accountData", []) or []:
        delta = abs((ad.get("nativeBalanceChange") or 0) / 1e9)
        if delta > 0.001:  # ignore dust
            sol_change = max(sol_change, delta)
    if tok_change > 0 and sol_change > 0:
        return sol_change / tok_change
    return None


def effective_entry_price(
    mint: str,
    their_price: float,
    their_ts: int,
    helius_key: str,
    modeled_lag_pct: float = 5.0,
) -> tuple[float, float, str]:
    """Return (fill_price, slip_pct_vs_their_price, source).

    source ∈ {"measured", "modeled", "modeled_no_data"}:
      - "measured": we found a post-wallet swap and used its empirical price
      - "modeled":  we found one but it was BETTER than modeled, so we kept
                    modeled as the floor (conservative)
      - "modeled_no_data": no post-wallet swap available within the window

    Always returns a price ≥ their_price × (1 + modeled_lag_pct/100). Live
    execution would fill at the worse of measured/modeled, never better.
    """
    modeled_price = their_price * (1.0 + modeled_lag_pct / 100.0)

    if not helius_key:
        return modeled_price, modeled_lag_pct, "modeled_no_data"

    swaps = _fetch_mint_swaps(mint, helius_key, limit=8)
    # Find first swap strictly AFTER their_ts (oldest such)
    post_swaps = [s for s in swaps if (s.get("timestamp") or 0) > their_ts]
    if not post_swaps:
        return modeled_price, modeled_lag_pct, "modeled_no_data"
    # Helius returns newest-first; the earliest-post-wallet is the LAST in that list
    next_swap = post_swaps[-1]
    measured_price = _price_from_swap(next_swap, mint)
    if not measured_price or measured_price <= 0:
        return modeled_price, modeled_lag_pct, "modeled_no_data"

    measured_slip_pct = (measured_price / their_price - 1.0) * 100.0
    if measured_price > modeled_price:
        # Real slip was worse than the 5% model — use the measured.
        return measured_price, round(measured_slip_pct, 2), "measured"
    # Real slip was BETTER than the model — keep the model as the floor.
    return modeled_price, modeled_lag_pct, "modeled"


if __name__ == "__main__":
    # Smoke test against a known recent trade
    import os
    from dotenv import dotenv_values
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    key = dotenv_values(os.path.join(ROOT, ".env")).get("HELIUS_API_KEY", "")
    # Pick a recent open from the log
    log = os.path.join(ROOT, "logs", "copy_follower_trades.jsonl")
    last_open = None
    for line in open(log, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("event") == "open":
            last_open = r
    if not last_open:
        print("no opens in log")
        raise SystemExit(0)
    print(f"testing on most recent open: mint={last_open.get('mint','')[:12]}  "
          f"their_entry={last_open.get('their_entry')}  ts={last_open.get('ts')}")
    fill, slip, src = effective_entry_price(
        last_open.get("mint"),
        last_open.get("their_entry"),
        int(last_open.get("ts") or 0),
        key,
    )
    print(f"  fill_price:     {fill:.4e}")
    print(f"  slip_vs_them:   {slip:.2f}%")
    print(f"  source:         {src}")
    if last_open.get("our_entry"):
        print(f"  (logged our_entry was {last_open['our_entry']:.4e}, "
              f"slip_pct {last_open.get('entry_slip_pct','?')}%)")
