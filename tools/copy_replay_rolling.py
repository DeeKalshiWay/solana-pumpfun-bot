"""
tools/copy_replay_rolling.py

Honest rolling out-of-sample test of the copy-trade strategy. For each split
point T, qualify wallets using ONLY trades with entry_ts < T, then replay the
strategy on trades in [T, T + WINDOW_DAYS). Fresh seed each window so the
results compare apples-to-apples.

Reports per-window stats AND aggregate facts:
  - fraction of windows positive
  - median / worst / best window return
  - per-window wallet contribution (so we can spot one-wallet-windows)
  - return at multiple copy-lag assumptions
The point: a strategy that works should be positive in MOST windows, not just
one cherry-picked one.

Pure replay on logs/_raw_txns. No API calls.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics as st

from tools.copy_replay import positions_full, RAW_CACHE, _size_for_trade

COPYABLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "logs", "copyable_wallets.json")
ALL_IN_FRICTION_PCT = 10.0
STOP_LOSS_PCT = 30.0
SEED_SOL = 250.0 / 170.0
TRAIN_MIN_N = 4
TRAIN_MIN_MEAN_XB = 15.0
TRAIN_MIN_MEDIAN_HOLD_S = 120.0


def load_by_wallet():
    out = {}
    for w in json.load(open(COPYABLE)):
        cp = os.path.join(RAW_CACHE, w + ".json")
        if not os.path.exists(cp):
            continue
        ps = positions_full(w, json.load(open(cp)))
        for p in ps:
            p["wallet"] = w
        out[w] = ps
    return out


def qualifies(pre_trades):
    if len(pre_trades) < TRAIN_MIN_N:
        return False
    pcts = [p["pct_raw"] for p in pre_trades]
    holds = [p["hold_s"] for p in pre_trades]
    if sum(pcts) <= 0:
        return False
    mxb = (sum(sorted(pcts)[:-1]) / (len(pcts) - 1)) if len(pcts) > 1 else pcts[0]
    if mxb <= TRAIN_MIN_MEAN_XB:
        return False
    if st.median(holds) < TRAIN_MIN_MEDIAN_HOLD_S:
        return False
    return True


def replay(test_trades, copy_lag_pct=0.0):
    """Simulate strategy on test_trades, return (final_bal, wins, total_pnl, top1_share, dd_pct, max_concurrent_w)."""
    bal = SEED_SOL
    peak = SEED_SOL
    max_dd = 0.0
    test_trades = sorted(test_trades, key=lambda p: p["entry_ts"])
    open_pos = {}
    pnls = []
    wallets_seen = set()
    for t in test_trades:
        for m in list(open_pos.keys()):
            if open_pos[m]["exit_ts"] <= t["entry_ts"]:
                del open_pos[m]
        committed = sum(p["size_sol"] for p in open_pos.values())
        size, reason = _size_for_trade(bal, committed)
        if reason != "ok":
            continue
        gross = max(-STOP_LOSS_PCT, t["pct_raw"])
        net = gross - ALL_IN_FRICTION_PCT - copy_lag_pct
        pnl = size * net / 100
        bal += pnl
        peak = max(peak, bal)
        if peak > 0:
            max_dd = max(max_dd, (peak - bal) / peak * 100)
        pnls.append(pnl)
        wallets_seen.add(t["wallet"])
        open_pos[t["mint"]] = {"size_sol": size, "exit_ts": t["exit_ts"]}
    if not pnls:
        return None
    wins = sum(1 for x in pnls if x > 0)
    total = sum(pnls)
    top1 = max(pnls) if pnls else 0
    top1_share = (top1 / total * 100) if total > 0 else 0
    return {
        "balance": bal, "return_pct": (bal / SEED_SOL - 1) * 100,
        "n": len(pnls), "wins": wins, "win_rate": wins / len(pnls) * 100,
        "total_pnl": total, "top1_share": top1_share, "max_dd": max_dd,
        "n_wallets": len(wallets_seen),
        "wallets": sorted(wallets_seen),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=float, default=14.0)
    ap.add_argument("--stride-days", type=float, default=7.0)
    ap.add_argument("--copy-lag", type=float, default=0.0)
    args = ap.parse_args()
    WINDOW = args.window_days * 86400
    STRIDE = args.stride_days * 86400

    by_w = load_by_wallet()
    all_trades = sorted([p for ps in by_w.values() for p in ps], key=lambda p: p["entry_ts"])
    t0, t1 = all_trades[0]["entry_ts"], all_trades[-1]["entry_ts"]
    print(f"Dataset: {len(all_trades)} trades, "
          f"{dt.datetime.fromtimestamp(t0,dt.UTC):%Y-%m-%d}->{dt.datetime.fromtimestamp(t1,dt.UTC):%Y-%m-%d} "
          f"({(t1-t0)/86400:.0f} days)")
    print(f"Rolling test: {args.window_days:.0f}-day windows, {args.stride_days:.0f}-day stride, copy-lag {args.copy_lag}%\n")

    # split points: walk T from t0 + 14d through t1 - WINDOW
    T = t0 + 14 * 86400  # first split needs at least 14d of training data
    results = []
    print(f"{'#':>3} {'split':<12} {'qual':>5} {'test_n':>7} {'ret%':>8} {'win%':>6} {'top1%':>7} {'DD%':>6} {'wallets'}")
    print("-" * 92)
    i = 0
    while T <= t1 - WINDOW:
        i += 1
        # qualify per-wallet on pre-T
        qualified = [w for w, ps in by_w.items() if qualifies([p for p in ps if p["entry_ts"] < T])]
        test = [p for w in qualified for p in by_w[w] if T <= p["entry_ts"] < T + WINDOW]
        r = replay(test, args.copy_lag)
        date = dt.datetime.fromtimestamp(T, dt.UTC).strftime("%m-%d %H:%MZ")
        if r is None:
            print(f"{i:>3} {date:<12} {len(qualified):>5} {0:>7}      —      —      —     —")
        else:
            ws = "+".join(w[:6] for w in r["wallets"])
            print(f"{i:>3} {date:<12} {len(qualified):>5} {r['n']:>7} {r['return_pct']:>+8.1f} "
                  f"{r['win_rate']:>5.0f}% {r['top1_share']:>6.0f}% {r['max_dd']:>5.1f}%  {ws}")
            results.append(r)
        T += STRIDE

    if not results:
        print("\nno qualifying windows"); return

    rets = [r["return_pct"] for r in results]
    pos = sum(1 for r in rets if r > 0)
    print(f"\n=== AGGREGATE OVER {len(results)} WINDOWS ===")
    print(f"  positive windows: {pos}/{len(results)} ({pos*100/len(results):.0f}%)")
    print(f"  median return:    {st.median(rets):+.1f}%")
    print(f"  mean return:      {st.mean(rets):+.1f}%")
    print(f"  best window:      {max(rets):+.1f}%")
    print(f"  worst window:     {min(rets):+.1f}%")
    n_wallets_per = [r["n_wallets"] for r in results]
    print(f"  avg wallets/window: {st.mean(n_wallets_per):.1f}  (windows w/ >=2 wallets: {sum(1 for x in n_wallets_per if x>=2)}/{len(results)})")
    n_per = [r["n"] for r in results]
    print(f"  avg trades/window:  {st.mean(n_per):.1f}  (median {st.median(n_per):.0f})")
    top1_avg = st.mean(r["top1_share"] for r in results if r["total_pnl"] > 0)
    print(f"  avg top-1 share in winning windows: {top1_avg:.0f}%")

    # Sensitivity: try a few copy-lag values
    if args.copy_lag == 0.0:
        print("\n=== COPY-LAG SENSITIVITY (aggregate median return) ===")
        for lag in (0, 5, 10, 15, 20):
            rs = []
            T = t0 + 14 * 86400
            while T <= t1 - WINDOW:
                qualified = [w for w, ps in by_w.items() if qualifies([p for p in ps if p["entry_ts"] < T])]
                test = [p for w in qualified for p in by_w[w] if T <= p["entry_ts"] < T + WINDOW]
                r = replay(test, lag)
                if r: rs.append(r["return_pct"])
                T += STRIDE
            if rs:
                pos2 = sum(1 for x in rs if x > 0)
                print(f"  lag {lag:>2}%:  median {st.median(rs):>+6.1f}%  | mean {st.mean(rs):>+6.1f}%  | "
                      f"positive {pos2}/{len(rs)}  | worst {min(rs):>+6.1f}%")


if __name__ == "__main__":
    main()
