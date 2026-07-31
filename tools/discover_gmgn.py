"""
tools/discover_gmgn.py  (Discovery 5)

Pull curated "smart money" candidates from GMGN's public ranking API. GMGN's
top-PnL wallet leaderboards are the de-facto curated list pump.fun snipers use,
so they're a much higher-signal candidate funnel than raw on-chain scans.

Two-stage filter:
  1. Pull top-N wallets from GMGN's 7d AND 30d PnL rankings.
  2. Pre-filter on GMGN's own metrics to match OUR criteria:
       - avg_holding_period >= MIN_HOLD_S          (copyable at our latency)
       - winrate >= MIN_WIN                         (real edge)
       - txs >= MIN_TXS                             (meaningful sample)
       - NOT >50% of wins are 5x-or-greater         (concentration sanity)
       - last_active within RECENCY_DAYS            (still trading)

Output: `logs/_candidates_gmgn.json` (just addresses). Feed into
`wallet_pnl_helius --wallets-file` for independent verification — we don't
trust GMGN's numbers blindly; their metrics decide who's worth verifying.

Run: python -m tools.discover_gmgn [--limit 200]
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "logs", "_candidates_gmgn.json")

# Our filter — match the strict edge criterion at the GMGN-data level
MIN_HOLD_S = 120        # copyable
MIN_WIN = 0.55          # winrate floor (GMGN's 7d winrate is decimal)
MIN_TXS = 30            # meaningful sample
MAX_5X_SHARE = 0.50     # of wins, at most 50% can be 5x+ (concentration sanity)
RECENCY_DAYS = 14       # last_active within window
PERIODS = ("7d", "30d")  # rank windows to pull from

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://gmgn.ai/",
}


def fetch_rank(period: str, limit: int) -> list[dict]:
    url = (f"https://gmgn.ai/defi/quotation/v1/rank/sol/wallets/{period}"
           f"?orderby=pnl_{period}&direction=desc&limit={limit}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
    except Exception as e:
        print(f"  fetch {period} failed: {type(e).__name__}: {e}")
        return []
    return (d.get("data") or {}).get("rank") or []


def passes_filter(e: dict, period: str) -> tuple[bool, str]:
    hold = e.get(f"avg_holding_period_{period}", 0) or 0
    win = e.get(f"winrate_{period}", 0) or 0
    txs = e.get(f"txs_{period}", 0) or 0
    last_active = e.get("last_active", 0) or 0
    pnl_5x = e.get(f"pnl_gt_5x_num_{period}", 0) or 0
    pnl_25 = e.get(f"pnl_2x_5x_num_{period}", 0) or 0
    pnl_lt2 = e.get(f"pnl_lt_2x_num_{period}", 0) or 0
    wins_total = pnl_5x + pnl_25 + pnl_lt2
    if hold < MIN_HOLD_S:
        return False, f"hold {int(hold)}s < {MIN_HOLD_S}"
    if win < MIN_WIN:
        return False, f"win {win:.2f} < {MIN_WIN}"
    if txs < MIN_TXS:
        return False, f"txs {txs} < {MIN_TXS}"
    if wins_total > 0 and pnl_5x / wins_total > MAX_5X_SHARE:
        return False, f"5x share {pnl_5x/wins_total:.2f} > {MAX_5X_SHARE} (lottery)"
    if last_active and (time.time() - last_active) > RECENCY_DAYS * 86400:
        days = (time.time() - last_active) / 86400
        return False, f"stale {days:.1f}d"
    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200,
                    help="top-N wallets to pull from each period")
    ap.add_argument("--show-all", action="store_true",
                    help="print every entry with its filter verdict (verbose)")
    args = ap.parse_args()

    all_addrs = {}    # addr -> {period: True, ...}
    rejected = {"hold": 0, "win": 0, "txs": 0, "5x": 0, "stale": 0}
    accepted_records = {}
    for period in PERIODS:
        print(f"\nPulling top {args.limit} GMGN-PnL wallets for period={period}…")
        entries = fetch_rank(period, args.limit)
        print(f"  got {len(entries)} entries")
        n_pass = 0
        for e in entries:
            addr = e.get("address") or e.get("wallet_address")
            if not addr:
                continue
            ok, reason = passes_filter(e, period)
            if args.show_all:
                pnl = e.get(f"realized_profit_{period}", 0) or 0
                win = e.get(f"winrate_{period}", 0) or 0
                hold = e.get(f"avg_holding_period_{period}", 0) or 0
                txs = e.get(f"txs_{period}", 0) or 0
                print(f"    {addr[:12]:<12}  $${pnl:>10,.0f}  win={win:.2f}  hold={int(hold):>5}s  txs={txs:>4}  "
                      f"-> {'PASS' if ok else 'reject: '+reason}")
            if ok:
                all_addrs.setdefault(addr, []).append(period)
                accepted_records[addr] = e
                n_pass += 1
            else:
                if "hold" in reason: rejected["hold"] += 1
                elif "win" in reason: rejected["win"] += 1
                elif "txs" in reason: rejected["txs"] += 1
                elif "5x" in reason: rejected["5x"] += 1
                elif "stale" in reason: rejected["stale"] += 1
        print(f"  {period} passes: {n_pass}/{len(entries)}")

    print(f"\n=== AGGREGATE ===")
    print(f"  total unique passing addresses: {len(all_addrs)}")
    print(f"  reject reasons: {rejected}")
    print()
    def _num(v):
        try: return float(v)
        except (TypeError, ValueError): return 0.0

    # Sort by 7d realized profit (or 30d fallback)
    ranked = sorted(
        accepted_records.items(),
        key=lambda kv: _num(kv[1].get("realized_profit_7d") or kv[1].get("realized_profit_30d")),
        reverse=True,
    )
    # Save FIRST so a print bug can never lose results
    addrs = [a for a, _ in ranked]
    json.dump(addrs, open(OUT, "w"), indent=1)
    print(f"\nWrote {len(addrs)} candidates -> {OUT}\n")
    print(f"  top 20 by realized profit:")
    print(f"  {'wallet':<14} {'periods':<8} {'$ profit 7d':>14} {'win 7d':>8} {'hold 7d':>10} {'txs 7d':>8}")
    for addr, e in ranked[:20]:
        periods = "+".join(all_addrs[addr])
        prof = _num(e.get("realized_profit_7d"))
        win = _num(e.get("winrate_7d"))
        hold = _num(e.get("avg_holding_period_7d"))
        txs = int(_num(e.get("txs_7d")))
        print(f"  {addr[:12]:<14} {periods:<8} {prof:>14,.0f} {win:>8.2f} {int(hold):>9}s {txs:>8}")
    print("Feed into Helius verification:")
    print(f"  python -m tools.wallet_pnl_helius --wallets-file {OUT} --pages 6 --workers 6 --resume")


if __name__ == "__main__":
    main()
