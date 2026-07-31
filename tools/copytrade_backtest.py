"""
tools/copytrade_backtest.py

Out-of-sample backtest of the copy-trade STRATEGY net of costs.

Wallet selection was validated by tools/holdout_copytrade.py. This goes further:
it simulates actually COPYING the train-robust wallets' post-split trades and
asks the only question that matters for go-live — does it make money after
friction and execution lag?

Method (kept honest / out-of-sample):
  1. Reconstruct each wallet's closed (round-tripped) positions w/ entry time
     from cached raw txns (reuses tools.holdout_copytrade.positions_for).
  2. Split globally at T = median position time. Select wallets that are robust
     using ONLY pre-T trades (same rule as the holdout).
  3. Over POST-T trades of those wallets, model our copied return per trade:
        our_multiple = (1 + wallet_pct/100) * (1 - exit_slip) / (1 + entry_slip)
        our_pct      = (our_multiple - 1)*100 - fee_pct
     entry_slip absorbs the latency penalty of entering AFTER the wallet.
  4. Size each copy at FIXED_SOL, aggregate to net SOL, win rate, EV/trade.
  5. Concentration check (strategy stays positive after dropping its best trade)
     + sensitivity across friction assumptions.

No API, no live infra. Run: python -m tools.copytrade_backtest
"""

from __future__ import annotations

import os
import statistics as st

from tools.holdout_copytrade import positions_for, RAW_CACHE, TRAIN_MIN_N, TRAIN_MIN_MEAN_XB

import json

FIXED_SOL = 0.10   # per copied trade

# (entry_slip, exit_slip, fee_pct) friction scenarios. entry_slip includes the
# latency penalty of entering after the wallet (fast pumps move against us).
SCENARIOS = {
    "optimistic (5/3/1%)":  (0.05, 0.03, 1.0),
    "realistic (8/5/2%)":   (0.08, 0.05, 2.0),
    "pessimistic (12/8/2%)":(0.12, 0.08, 2.0),
    "harsh (18/10/2%)":     (0.18, 0.10, 2.0),
}


def _mean_drop_best(xs):
    if len(xs) < 2:
        return xs[0] if xs else 0.0
    r = list(xs); r.remove(max(r))
    return sum(r) / len(r)


def load_positions():
    by_wallet, all_ts = {}, []
    for f in os.listdir(RAW_CACHE):
        if not f.endswith(".json"):
            continue
        w = f[:-5]
        try:
            txns = json.load(open(os.path.join(RAW_CACHE, f)))
        except Exception:
            continue
        ps = positions_for(w, txns)
        if ps:
            by_wallet[w] = ps
            all_ts += [p["entry_ts"] for p in ps]
    return by_wallet, all_ts


def copied_pct(wallet_pct, entry_slip, exit_slip, fee_pct):
    mult = (1 + wallet_pct / 100.0) * (1 - exit_slip) / (1 + entry_slip)
    return (mult - 1) * 100.0 - fee_pct


def main():
    by_wallet, all_ts = load_positions()
    T = st.median(all_ts)
    import datetime as dt
    print(f"Closed-position wallets: {len(by_wallet)} | positions: {len(all_ts)} | "
          f"split T={dt.datetime.fromtimestamp(T, dt.UTC):%Y-%m-%d %H:%M}Z\n")

    # Select robust wallets on PRE-T data only (out-of-sample discipline)
    robust = []
    for w, ps in by_wallet.items():
        tr = [p for p in ps if p["entry_ts"] < T]
        if len(tr) < TRAIN_MIN_N:
            continue
        if sum(p["pnl_sol"] for p in tr) > 0 and _mean_drop_best([p["pct"] for p in tr]) > TRAIN_MIN_MEAN_XB:
            robust.append(w)

    # Post-T trades we would have copied
    copy_trades = [p["pct"] for w in robust for p in by_wallet[w] if p["entry_ts"] >= T]
    print(f"Train-robust wallets: {len(robust)} | post-T trades we'd copy: {len(copy_trades)} | size: {FIXED_SOL} SOL/trade\n")
    if not copy_trades:
        print("No post-T trades to copy."); return

    print(f"{'friction scenario':<24}{'net SOL':>9}{'EV/trade':>10}{'mean%':>8}{'median%':>9}{'win%':>7}{'net-x-best':>11}")
    print("-" * 78)
    for name, (es, xs_, fee) in SCENARIOS.items():
        ours = [copied_pct(p, es, xs_, fee) for p in copy_trades]
        ours = [max(o, -100.0) for o in ours]   # can't lose more than the position
        net = sum(FIXED_SOL * o / 100.0 for o in ours)
        ev = net / len(ours)
        win = sum(1 for o in ours if o > 0) / len(ours)
        # strategy stays positive after dropping its single best trade?
        net_xb = net - FIXED_SOL * max(ours) / 100.0
        print(f"{name:<24}{net:>9.2f}{ev:>10.4f}{st.mean(ours):>8.1f}{st.median(ours):>9.1f}"
              f"{win*100:>6.0f}%{net_xb:>11.2f}")

    print("\nReading: EV/trade is net SOL per copied trade at 0.10 SOL size. Positive across")
    print("scenarios = robust to cost assumptions. 'net-x-best' = net SOL excluding the single")
    print("best trade — if still positive, the strategy isn't carried by one outlier.")


if __name__ == "__main__":
    main()
