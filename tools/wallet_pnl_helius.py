"""
tools/wallet_pnl_helius.py

Compute REAL realized PnL per wallet from on-chain history via the Helius
Enhanced Transactions API, then rank wallets for copy-trading.

WHY
---
The bot's stored wallet_outcomes (mc_delta_pct at +10min) is the wrong yardstick
for copy-trading and yields 0 proven wallets (see analyzer/wallet_ranker.py).
This tool replaces the proxy with on-chain truth: for each candidate wallet it
sums the wallet's native SOL balance change across every pump.fun SWAP touching
a given mint. For a fully-exited position that sum IS the realized PnL in SOL;
the % return is realized / SOL-spent-on-buys.

Output:
  logs/wallet_realized_pnl.json   { wallet: {mint: {spent, recv, pnl_sol, pct, closed}, ...} }
  logs/wallet_realized_outcomes.json  { wallet: [pct, ...] }  (closed positions only; ranker input)

Then runs analyzer.wallet_ranker on the corrected outcomes.

Cost-aware: caps wallets (--n) and pages-per-wallet (--pages, 100 txns/page),
sleeps between calls, writes partial results so a crash loses nothing.

Usage:
  python -m tools.wallet_pnl_helius --n 120 --pages 2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_WALLETS = os.path.join(ROOT, "logs", "bot_wallets.json")
OUT_PNL = os.path.join(ROOT, "logs", "wallet_realized_pnl.json")
OUT_OUTCOMES = os.path.join(ROOT, "logs", "wallet_realized_outcomes.json")
RAW_CACHE = os.path.join(ROOT, "logs", "_raw_txns")  # per-wallet raw txn cache

WSOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
MIN_BUY_SOL = 0.05    # ignore dust positions below this total spend
ROUNDTRIP_TOL = 0.10  # |tokens_out - tokens_in| / tokens_in must be <= this to count as a verified round-trip


def _key() -> str:
    k = dotenv_values(os.path.join(ROOT, ".env")).get("HELIUS_API_KEY", "")
    if not k:
        sys.exit("No HELIUS_API_KEY in .env")
    return k


def _native_change(tx: dict, wallet: str) -> int:
    for a in tx.get("accountData", []):
        if a.get("account") == wallet:
            return a.get("nativeBalanceChange", 0) or 0
    return 0


def _pump_mint_and_dir(tx: dict, wallet: str):
    """Return (mint, 'buy'|'sell', token_amount) for a pump.fun swap, else (None,None,0)."""
    for tt in tx.get("tokenTransfers", []) or []:
        mint = tt.get("mint")
        if not mint or mint == WSOL:
            continue
        amt = float(tt.get("tokenAmount") or 0)
        if tt.get("toUserAccount") == wallet:
            return mint, "buy", amt
        if tt.get("fromUserAccount") == wallet:
            return mint, "sell", amt
    return None, None, 0.0


def _cache_path(wallet: str) -> str:
    return os.path.join(RAW_CACHE, wallet + ".json")


def fetch_wallet_txns(wallet: str, key: str, pages: int, sleep: float) -> list[dict]:
    out, before = [], None
    for _ in range(pages):
        url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={key}&limit=100"
        if before:
            url += f"&before={before}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pnl"})
            with urllib.request.urlopen(req, timeout=40) as r:
                page = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0); continue
            break
        except Exception:
            break
        if not page:
            break
        out.extend(page)
        before = page[-1].get("signature")
        time.sleep(sleep)
        if len(page) < 100:
            break
    # cache raw so PnL logic can be re-derived offline without re-spending API
    try:
        os.makedirs(RAW_CACHE, exist_ok=True)
        json.dump(out, open(_cache_path(wallet), "w"))
    except Exception:
        pass
    return out


def wallet_pnl(txns: list[dict], wallet: str) -> dict:
    """Aggregate net SOL flow per mint across pump.fun swaps.

    A position only counts as `closed` (valid for PnL) if it ROUND-TRIPPED
    inside the fetched window: tokens bought ~= tokens sold (within
    ROUNDTRIP_TOL). This rejects boundary artifacts where a wallet sells a bag
    accumulated before the window (sell with no in-window buy), which otherwise
    inflate returns to absurd levels.
    """
    by_mint: dict[str, dict] = {}
    for t in txns:
        if t.get("source") not in ("PUMP_FUN", "PUMP_AMM", "PUMPSWAP"):
            continue
        if t.get("type") not in ("SWAP",):
            continue
        mint, direction, qty = _pump_mint_and_dir(t, wallet)
        if not mint:
            continue
        sol = _native_change(t, wallet) / LAMPORTS  # +recv (sell) / -spent (buy)
        m = by_mint.setdefault(mint, {"spent": 0.0, "recv": 0.0, "buys": 0, "sells": 0,
                                      "tok_in": 0.0, "tok_out": 0.0})
        if direction == "buy":
            m["spent"] += -sol if sol < 0 else 0.0
            m["tok_in"] += qty
            m["buys"] += 1
        else:
            m["recv"] += sol if sol > 0 else 0.0
            m["tok_out"] += qty
            m["sells"] += 1
    # finalize
    for mint, m in by_mint.items():
        m["pnl_sol"] = round(m["recv"] - m["spent"], 5)
        roundtrip = (m["tok_in"] > 0 and m["tok_out"] > 0
                     and abs(m["tok_out"] - m["tok_in"]) / m["tok_in"] <= ROUNDTRIP_TOL)
        m["closed"] = bool(roundtrip and m["spent"] >= MIN_BUY_SOL)
        m["pct"] = round((m["pnl_sol"] / m["spent"]) * 100, 1) if m["closed"] else None
        m["spent"] = round(m["spent"], 5); m["recv"] = round(m["recv"], 5)
        m["tok_in"] = round(m["tok_in"], 2); m["tok_out"] = round(m["tok_out"], 2)
    return by_mint


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120, help="cap on number of candidate wallets")
    ap.add_argument("--min-buys", type=int, default=0, help="select all wallets with buys>=this (overrides top-n selection, still capped by --n)")
    ap.add_argument("--pages", type=int, default=2, help="max 100-txn pages per wallet")
    ap.add_argument("--sleep", type=float, default=0.05, help="seconds between API calls")
    ap.add_argument("--workers", type=int, default=6, help="concurrent fetch workers")
    ap.add_argument("--wallets-file", type=str, default="", help="JSON list of wallet addresses to scan (from a discovery tool)")
    ap.add_argument("--resume", action="store_true", help="skip wallets already in the output files")
    ap.add_argument("--recompute", action="store_true", help="recompute PnL from cached raw txns only (no API calls)")
    args = ap.parse_args()

    if args.recompute:
        return _recompute_from_cache()

    key = _key()
    if args.wallets_file:
        candidates = json.load(open(args.wallets_file, encoding="utf-8"))[: args.n]
        print(f"Loaded {len(candidates)} candidate wallets from {args.wallets_file}")
    else:
        bw = json.load(open(BOT_WALLETS, encoding="utf-8"))
        ranked = sorted(bw.items(), key=lambda kv: kv[1].get("buys", 0), reverse=True)
        if args.min_buys > 0:
            candidates = [w for w, v in ranked if v.get("buys", 0) >= args.min_buys][: args.n]
        else:
            candidates = [w for w, _ in ranked][: args.n]

    pnl_all, outcomes = {}, {}
    if args.resume and os.path.exists(OUT_PNL):
        pnl_all = json.load(open(OUT_PNL))
        outcomes = json.load(open(OUT_OUTCOMES)) if os.path.exists(OUT_OUTCOMES) else {}
        candidates = [w for w in candidates if w not in pnl_all]
        print(f"Resume: {len(pnl_all)} wallets already done; {len(candidates)} remaining")
    print(f"Candidates to fetch: {len(candidates)} | pages/wallet: {args.pages} | workers: {args.workers}")

    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _get(w):
        cp = _cache_path(w)
        if os.path.exists(cp):
            try: return w, json.load(open(cp))   # reuse cache, no API call
            except Exception: pass
        return w, fetch_wallet_txns(w, key, args.pages, args.sleep)

    t0 = time.time()
    lock = threading.Lock()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in as_completed([ex.submit(_get, w) for w in candidates]):
            w, txns = fut.result()
            by_mint = wallet_pnl(txns, w)
            pnl_all[w] = by_mint
            outcomes[w] = [d["pct"] for d in by_mint.values() if d["closed"] and d["pct"] is not None]
            with lock:
                done += 1
                if done % 25 == 0 or done == len(candidates):
                    json.dump(pnl_all, open(OUT_PNL, "w"), indent=1)
                    json.dump(outcomes, open(OUT_OUTCOMES, "w"), indent=1)
                    tot = sum(sum(d["pnl_sol"] for d in v.values() if d["closed"]) for v in pnl_all.values())
                    print(f"  [{done}/{len(candidates)}] net SOL(closed): {tot:+.1f} | {time.time()-t0:.0f}s", flush=True)

    json.dump(pnl_all, open(OUT_PNL, "w"), indent=1)
    json.dump(outcomes, open(OUT_OUTCOMES, "w"), indent=1)
    print(f"\nWrote {OUT_PNL} and {OUT_OUTCOMES}")
    _rank_and_print(outcomes, pnl_all)


def _recompute_from_cache():
    """Re-derive PnL + outcomes from all cached raw txns; no API calls."""
    if not os.path.isdir(RAW_CACHE):
        sys.exit("No raw cache dir — run a fetch first.")
    pnl_all, outcomes = {}, {}
    files = [f for f in os.listdir(RAW_CACHE) if f.endswith(".json")]
    for f in files:
        w = f[:-5]
        try:
            txns = json.load(open(os.path.join(RAW_CACHE, f)))
        except Exception:
            continue
        by_mint = wallet_pnl(txns, w)
        pnl_all[w] = by_mint
        outcomes[w] = [d["pct"] for d in by_mint.values() if d["closed"] and d["pct"] is not None]
    json.dump(pnl_all, open(OUT_PNL, "w"), indent=1)
    json.dump(outcomes, open(OUT_OUTCOMES, "w"), indent=1)
    print(f"Recomputed {len(files)} cached wallets -> {OUT_PNL}")
    _rank_and_print(outcomes, pnl_all)
    return 0


def _rank_and_print(outcomes: dict, pnl_all: dict):
    from analyzer.wallet_ranker import rank_wallets
    net = {w: round(sum(d["pnl_sol"] for d in v.values() if d.get("closed")), 3) for w, v in pnl_all.items()}
    nonempty = {w: o for w, o in outcomes.items() if o}
    scores = rank_wallets(nonempty)
    profitable = [w for w, n in net.items() if n > 0]
    robust = [s for s in scores if s.n >= 8 and s.mean_drop_best > 15 and net.get(s.wallet, 0) > 0]
    print("\n=== RANKING ON REAL ROUND-TRIPPED PnL (token-reconciled) ===")
    print(f"evaluated: {len(pnl_all)} | with verified closed positions: {len(nonempty)} | "
          f"net-profitable: {len(profitable)} | aggregate net SOL: {sum(net.values()):+.1f}")
    print(f"CONCENTRATION-ROBUST (n>=8, profitable after dropping best, net>0): {len(robust)}")
    hdr = f"{'wallet':<14}{'n':>4}{'netSOL':>9}{'hit@0':>7}{'hit@50':>7}{'mean':>8}{'mean-xb':>8}{'best%':>7}{'ROBUST':>7}"
    print(hdr); print("-" * len(hdr))
    robust_set = set(id(s) for s in robust)
    for s in scores[:30]:
        r = "Y" if id(s) in robust_set else ""
        print(f"{s.wallet[:12]:<14}{s.n:>4}{net.get(s.wallet,0):>9.2f}{s.hit_rate_0:>7.2f}{s.hit_rate_50:>7.2f}"
              f"{s.mean:>8.1f}{s.mean_drop_best:>8.1f}{s.best_share:>7.2f}{r:>7}")


if __name__ == "__main__":
    main()
