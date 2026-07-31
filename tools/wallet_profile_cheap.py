"""
tools/wallet_profile_cheap.py

Fallback chain that replaces wallet_scout's expensive `fetch_deep` call.

Old path:  every candidate → fetch_deep (up to 15 pages × 100 txs) via Helius
           Enhanced REST = ~4 Helius calls per candidate × 1,500 candidates per
           scout iteration = ~6,000 calls per iter × 96 iter/day = 576k/day.

New path:  try sources in order of cost, stop at first success.

  Tier 1 — local raw_txns cache (any age)         ← FREE, full profile
  Tier 2 — wallet_realized_pnl precomputed cache  ← FREE, partial profile
  Tier 3 — free public RPC (rpc_pool + parser)    ← FREE, slow, full profile
  Tier 4 — Helius shallow (1 page = 100 txs)      ← 1 Helius call (was ~4)

Returns the same `profile` dict shape wallet_scout already consumes:
  {wallet, n, mean_pct, win_rate, mean_drop_best, span_days, med_hold_s}

Birdeye and Solscan tiers were probed and require paid plans — left as stubs.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from tools.discover_edge_wallets import profile_wallet
from tools.raw_tx_parser import parse_raw_to_enhanced
from tools.rpc_pool import get_signatures, get_transaction


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_CACHE_DIR = os.path.join(ROOT, "logs", "_raw_txns")
PNL_CACHE_PATH = os.path.join(ROOT, "logs", "wallet_realized_pnl.json")

# Floors mirroring wallet_scout's SCORE_FLOOR_N
MIN_N = 10

# In-process PnL cache load (loaded lazily; the file is 43MB so keep it shared)
_PNL_CACHE: dict | None = None


def _load_pnl_cache() -> dict:
    global _PNL_CACHE
    if _PNL_CACHE is None:
        try:
            _PNL_CACHE = json.load(open(PNL_CACHE_PATH))
        except Exception:
            _PNL_CACHE = {}
    return _PNL_CACHE


# ---------- Tier 1: local raw_txns cache --------------------------------
def _profile_from_raw_cache(wallet: str) -> dict | None:
    cp = os.path.join(RAW_CACHE_DIR, wallet + ".json")
    if not os.path.exists(cp):
        return None
    try:
        txns = json.load(open(cp))
    except Exception:
        return None
    if not txns:
        return None
    return profile_wallet(wallet, txns)


# ---------- Tier 2: wallet_realized_pnl cache ---------------------------
def _profile_from_pnl_cache(wallet: str) -> dict | None:
    """Build a profile from the precomputed per-mint PnL.

    PnL cache shape per wallet:
      {mint: {spent, recv, buys, sells, tok_in, ...}}

    Computes: n (distinct mints), mean_pct (mean of return%), win_rate.
    Cannot compute span_days or med_hold_s — sets sentinel values that
    will fail the scout's span floor unless wallet has other proof.
    Used as gate-only signal (cheap pre-screen) when raw cache misses.
    """
    pnl = _load_pnl_cache()
    e = pnl.get(wallet)
    if not isinstance(e, dict) or not e:
        return None
    # Each value is a per-mint PnL dict
    rets = []
    for mint, m in e.items():
        if not isinstance(m, dict):
            continue
        spent = float(m.get("spent") or 0)
        recv = float(m.get("recv") or 0)
        # Treat unclosed positions (sells=0) as still-open → ignore for return calc
        if spent <= 0 or (m.get("sells") or 0) == 0:
            continue
        rets.append((recv / spent - 1.0) * 100.0)
    if len(rets) < MIN_N:
        return None
    n = len(rets)
    mean_pct = sum(rets) / n
    wins = sum(1 for r in rets if r > 0)
    win_rate = wins / n
    # mean_drop_best: drop the single best return and average the rest
    if n >= 2:
        sorted_rets = sorted(rets, reverse=True)
        mean_drop_best = sum(sorted_rets[1:]) / (n - 1)
    else:
        mean_drop_best = mean_pct
    # We don't have timestamps in the PnL cache. Use sentinels that the scout's
    # SCORE_FLOOR_SPAN_DAYS = 3.0 floor will catch unless the wallet also has
    # raw cache to corroborate. This tier is intentionally a SOFT signal.
    return {
        "wallet": wallet,
        "n": n,
        "mean_pct": round(mean_pct, 4),
        "win_rate": round(win_rate, 4),
        "mean_drop_best": round(mean_drop_best, 4),
        "span_days": 0.0,            # unknown — caller should down-weight
        "med_hold_s": 0,             # unknown
        "_source": "pnl_cache",
    }


# ---------- Tier 3: free public RPC via rpc_pool ------------------------
def _profile_from_free_rpc(wallet: str, limit: int = 100) -> dict | None:
    """Pull signatures via free RPC, enrich via raw_tx_parser, score.
    Slow (~25s for 10 txs on mainnet-beta) but doesn't use Helius credit.
    """
    sigs = get_signatures(wallet, limit=limit)
    if not sigs:
        return None
    txns = []
    for s in sigs:
        sig = s.get("signature")
        if not sig or s.get("err"):
            continue
        raw = get_transaction(sig)
        if not raw:
            continue
        enhanced = parse_raw_to_enhanced(raw)
        if enhanced:
            # Carry over blockTime from sig metadata if parser didn't populate
            if not enhanced.get("timestamp"):
                enhanced["timestamp"] = s.get("blockTime")
            txns.append(enhanced)
    if len(txns) < MIN_N:
        return None
    return profile_wallet(wallet, txns)


# ---------- Tier 4: Helius shallow (1 page) -----------------------------
def _profile_from_helius_shallow(wallet: str, helius_key: str, limit: int = 100) -> dict | None:
    """One Helius Enhanced REST page (100 txs) instead of fetch_deep's 15.
    Same cost as a single Helius call. Falls through if wallet has < MIN_N
    pump.fun swaps in the most-recent 100 txs (deeply inactive wallet).
    """
    if not helius_key:
        return None
    url = (f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
           f"?api-key={helius_key}&limit={limit}")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "scout-shallow"}),
            timeout=20,
        ) as r:
            txns = json.load(r)
    except Exception:
        return None
    if not txns or len(txns) < MIN_N:
        return None
    # Persist to raw_txns cache so the next iteration uses tier 1.
    try:
        os.makedirs(RAW_CACHE_DIR, exist_ok=True)
        cp = os.path.join(RAW_CACHE_DIR, wallet + ".json")
        if not os.path.exists(cp):
            json.dump(txns, open(cp, "w"))
    except Exception:
        pass
    return profile_wallet(wallet, txns)


# ---------- Public API --------------------------------------------------
def cheap_profile(wallet: str, helius_key: str = "",
                  *, allow_free_rpc: bool = True,
                  allow_shallow: bool = True) -> tuple[dict | None, str]:
    """Return (profile, source). source ∈
       {"raw_cache", "pnl_cache", "free_rpc", "helius_shallow", "none"}.

    Try tiers in order of cost. Stop on first success. The PnL-cache tier
    is treated as soft (returns a profile but with sentinel span_days=0);
    caller can decide whether to accept or push to a higher tier.
    """
    # Tier 1
    p = _profile_from_raw_cache(wallet)
    if p:
        return p, "raw_cache"
    # Tier 2 (soft — only useful as a pre-screen)
    p = _profile_from_pnl_cache(wallet)
    if p:
        return p, "pnl_cache"
    # Tier 3
    if allow_free_rpc:
        try:
            p = _profile_from_free_rpc(wallet)
            if p:
                return p, "free_rpc"
        except Exception:
            pass
    # Tier 4
    if allow_shallow and helius_key:
        p = _profile_from_helius_shallow(wallet, helius_key)
        if p:
            return p, "helius_shallow"
    return None, "none"


if __name__ == "__main__":
    # Smoke: profile every roster wallet via the cheap path, report sources
    import sys
    sys.path.insert(0, ROOT)
    from dotenv import dotenv_values
    helius_key = dotenv_values(os.path.join(ROOT, ".env")).get("HELIUS_API_KEY", "")
    roster = json.load(open(os.path.join(ROOT, "logs", "streaming_roster.json")))
    print(f"profiling {len(roster)} roster wallets via cheap path...\n")
    print(f'{"wallet":<14} {"source":<16} {"n":>4} {"mean%":>7} {"win%":>6}')
    print("-" * 60)
    counts = {}
    for w in roster:
        p, src = cheap_profile(w, helius_key, allow_free_rpc=False, allow_shallow=False)
        counts[src] = counts.get(src, 0) + 1
        if p:
            print(f'{w[:12]:<14} {src:<16} {p["n"]:>4} {p["mean_pct"]:>+6.1f}% {p["win_rate"]*100:>5.0f}%')
        else:
            print(f'{w[:12]:<14} {src:<16}  -')
    print()
    print(f"source breakdown: {counts}")
