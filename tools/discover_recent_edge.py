"""
tools/discover_recent_edge.py

Find edge wallets that are TRADING RIGHT NOW. The strict scan over cached
history surfaced only `6RrqZQ7W`, and several of the previously-copyable
wallets have gone dormant (4-15 days inactive). This re-runs the strict
criterion but adds a RECENCY filter so we don't backtest dead wallets.

Phase 1 (cheap sweep): for every address in bot_wallets.json (8,599),
fetch the last 10 txns and flag wallets with a pump.fun SWAP in the last
RECENCY_DAYS days.

Phase 2: for each recent-active wallet, ensure a deep cache (re-fetch up
to DEEP_PAGES pages) and apply the same strict edge criterion used by
`tools/discover_edge_wallets.py` (n>=50, span>=14d, mean>=25%, win>=60%,
mxb>=20%, hold>=120s).

Output: `logs/_recent_edge_wallets.json` — qualifying recent-active wallets,
ready to be added to the streaming roster.

Cost: ~8,500 cheap calls (concurrent, ~5 min) + ~5-20 deep wallet fetches
(~5 min). Total ~10-15 min.

Run: python -m tools.discover_recent_edge
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from dotenv import dotenv_values

from tools.copy_replay import positions_full, RAW_CACHE
from tools.discover_edge_wallets import (profile_wallet, passes_strict, fmt_p,
                                         MIN_N, MIN_SPAN_DAYS, MIN_MEAN_PCT,
                                         MIN_WIN_RATE, MIN_MEAN_DROP_BEST,
                                         MIN_MEDIAN_HOLD_S)
from tools.fetch_deeper import fetch_deep

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_WALLETS = os.path.join(ROOT, "logs", "bot_wallets.json")
OUT = os.path.join(ROOT, "logs", "_recent_edge_wallets.json")
RECENT_SWEEP_OUT = os.path.join(ROOT, "logs", "_recent_active_wallets.json")
RECENCY_DAYS = 7
DEEP_PAGES = 15
PUMP_SOURCES = {"PUMP_FUN", "PUMP_AMM", "PUMPSWAP"}


def _key():
    k = dotenv_values(os.path.join(ROOT, ".env")).get("HELIUS_API_KEY", "")
    if not k:
        sys.exit("No HELIUS_API_KEY")
    return k


def _is_recent_active(wallet: str, key: str, cutoff_ts: float) -> bool:
    """Cheap check: last 10 txns include a pump.fun SWAP after cutoff_ts."""
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={key}&limit=10"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rsw"})
        with urllib.request.urlopen(req, timeout=20) as r:
            txns = json.load(r)
    except Exception:
        return False
    return any(
        t.get("source") in PUMP_SOURCES
        and t.get("type") == "SWAP"
        and t.get("timestamp", 0) >= cutoff_ts
        for t in (txns or [])
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--sleep", type=float, default=0.04)
    ap.add_argument("--limit", type=int, default=0, help="cap wallets scanned (0 = no cap)")
    args = ap.parse_args()
    key = _key()

    wallets = list(json.load(open(BOT_WALLETS)).keys())
    if args.limit:
        wallets = wallets[: args.limit]
    cutoff = time.time() - RECENCY_DAYS * 86400

    # === Phase 1: recency sweep ===========================================
    print(f"Phase 1: sweeping {len(wallets)} wallets for pump.fun activity in last {RECENCY_DAYS}d "
          f"({args.workers} workers)")
    t0 = time.time()
    recent_active = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_is_recent_active, w, key, cutoff): w for w in wallets}
        done = 0
        for fut in futures:
            w = futures[fut]
            try:
                if fut.result():
                    recent_active.append(w)
            except Exception:
                pass
            done += 1
            if done % 500 == 0 or done == len(wallets):
                el = time.time() - t0
                rate = done / el if el > 0 else 0
                eta = (len(wallets) - done) / rate if rate > 0 else 0
                print(f"  [{done}/{len(wallets)}] active so far: {len(recent_active)} "
                      f"({el:.0f}s, ~{eta:.0f}s left)", flush=True)
            time.sleep(args.sleep)
    json.dump(recent_active, open(RECENT_SWEEP_OUT, "w"), indent=1)
    print(f"\nPhase 1 done: {len(recent_active)}/{len(wallets)} wallets pump.fun-active "
          f"in last {RECENCY_DAYS}d. Saved to {RECENT_SWEEP_OUT}.\n")

    if not recent_active:
        print("No recent-active wallets — strategy roster will go stale.")
        return

    # === Phase 2: deepen + apply strict edge criterion ====================
    print(f"Phase 2: deepening cache + scoring {len(recent_active)} recent-active wallets "
          f"against strict criterion")
    print(f"  n>={MIN_N}, span>={MIN_SPAN_DAYS}d, mean>={MIN_MEAN_PCT}%, "
          f"win>={MIN_WIN_RATE*100:.0f}%, mxb>={MIN_MEAN_DROP_BEST}%, hold>={MIN_MEDIAN_HOLD_S}s\n")
    qualifiers = []
    borderline = []
    for i, w in enumerate(recent_active, 1):
        cp = os.path.join(RAW_CACHE, w + ".json")
        # If cache shallow, deepen. Reuse existing cache if already deep enough.
        cache_n = 0
        if os.path.exists(cp):
            try:
                cache_n = len(json.load(open(cp)))
            except Exception:
                cache_n = 0
        if cache_n < DEEP_PAGES * 80:  # ~80 txns/page average (with stoppage)
            txns = fetch_deep(w, key, max_pages=DEEP_PAGES, sleep=0.06)
            if txns:
                json.dump(txns, open(cp, "w"))
        else:
            txns = json.load(open(cp))
        p = profile_wallet(w, txns)
        if not p:
            continue
        if passes_strict(p):
            qualifiers.append(p)
            print(fmt_p(p, True) + "  <- QUALIFIES")
        elif (p["n"] >= MIN_N * 0.7 and p["mean_pct"] >= MIN_MEAN_PCT * 0.7
              and p["win_rate"] >= MIN_WIN_RATE * 0.7):
            borderline.append(p)
        if i % 25 == 0 or i == len(recent_active):
            print(f"  [{i}/{len(recent_active)}] qualifiers={len(qualifiers)}, borderline={len(borderline)}",
                  flush=True)

    qualifiers.sort(key=lambda p: p["mean_drop_best"], reverse=True)
    borderline.sort(key=lambda p: p["mean_drop_best"], reverse=True)

    print(f"\n=== RECENT-ACTIVE EDGE WALLETS ({len(qualifiers)}) ===")
    for p in qualifiers:
        print(fmt_p(p, True))
    print(f"\n=== Recent-active BORDERLINES (top 15) ===")
    for p in borderline[:15]:
        print(fmt_p(p, False))

    json.dump([p["wallet"] for p in qualifiers], open(OUT, "w"), indent=1)
    print(f"\nWrote {len(qualifiers)} recent-active edge wallet(s) to {OUT}")


if __name__ == "__main__":
    main()
