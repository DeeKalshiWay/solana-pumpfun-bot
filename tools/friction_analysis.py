"""
tools/friction_analysis.py

Friction is the make-or-break variable for copy-trading. This separates the two
kinds of cost and finds the number that actually decides go/no-go:

  EXECUTION friction  — the bot's own cost to round-trip, using the live-
                        calibrated model in trader/paper_executor.py (base
                        slippage, size impact, MEV, asymmetric priority fees).
                        Known, modest at small size.
  COPY-LAG friction   — extra entry slippage from buying AFTER the proven
                        wallet (price moved in the gap). Unknown until the live
                        follower measures it. THE swing factor.

Outputs:
  1. Execution-friction breakdown at several trade sizes (where do fees vs
     slippage dominate?).
  2. BREAK-EVEN copy-lag: the max extra entry slippage the validated edge can
     absorb before out-of-sample EV goes to zero. The follower's measured
     slippage just has to come in under this.

Pure analysis on cached data + real config params. Run: python -m tools.friction_analysis
"""

from __future__ import annotations

import os
import statistics as st

from tools.holdout_copytrade import positions_for, RAW_CACHE, TRAIN_MIN_N, TRAIN_MIN_MEAN_XB
import json

# Live-calibrated params copied from trader/paper_executor.py
BASE_SLIPPAGE_PCT = 0.025
SIZE_SLIPPAGE_COEFF = 0.6
MEV_BASE_PCT = 0.005
MEV_SIZE_COEFF = 0.4
TX_FAIL_RATE = 0.05
PRIORITY_FEE_SOL = 0.001        # buy cap
SELL_PRIORITY_FEE_SOL = 0.005   # sell cap
SMALL_SELL_FEE_FALLBACK = 0.001
TYPICAL_CURVE_SOL = 30.0


def buy_fee(s):
    return min(PRIORITY_FEE_SOL, max(s * 0.05, 0.0005))


def sell_fee(value):
    return min(SELL_PRIORITY_FEE_SOL, value * 0.05) if value > 0.1 else min(SELL_PRIORITY_FEE_SOL, SMALL_SELL_FEE_FALLBACK)


def side_drag(s, curve):
    frac = min(s / max(curve, 5.0), 1.0)
    slip = BASE_SLIPPAGE_PCT + frac * SIZE_SLIPPAGE_COEFF
    mev = MEV_BASE_PCT + frac * MEV_SIZE_COEFF
    return slip + mev


def exec_breakdown(s, curve=TYPICAL_CURVE_SOL):
    bd = side_drag(s, curve); sd = side_drag(s, curve)
    fees = buy_fee(s) + sell_fee(s)
    fee_pct = fees / s * 100
    return bd * 100, sd * 100, fee_pct, (bd + sd) * 100 + fee_pct


def load_copy_trades():
    by_wallet, all_ts = {}, []
    for f in os.listdir(RAW_CACHE):
        if not f.endswith(".json"):
            continue
        w = f[:-5]
        try:
            ps = positions_for(w, json.load(open(os.path.join(RAW_CACHE, f))))
        except Exception:
            continue
        if ps:
            by_wallet[w] = ps; all_ts += [p["entry_ts"] for p in ps]
    T = st.median(all_ts)
    robust = [w for w, ps in by_wallet.items()
              if len([p for p in ps if p["entry_ts"] < T]) >= TRAIN_MIN_N
              and sum(p["pnl_sol"] for p in ps if p["entry_ts"] < T) > 0
              and (lambda xs: (sorted(xs)[:-1] and sum(sorted(xs)[:-1]) / (len(xs) - 1) or xs[0]))([p["pct"] for p in ps if p["entry_ts"] < T]) > TRAIN_MIN_MEAN_XB]
    trades = [p["pct"] for w in robust for p in by_wallet[w] if p["entry_ts"] >= T]
    return trades, len(robust)


def net_at(trades, size, lag_pct, curve=TYPICAL_CURVE_SOL):
    """Total net SOL copying `trades` at `size`, with copy-lag entry slippage lag_pct."""
    bd = side_drag(size, curve); sd = side_drag(size, curve)
    fees = buy_fee(size) + sell_fee(size)
    total = 0.0
    for wp in trades:
        # tx fail: lose the buy fee, no position
        eff_fail = TX_FAIL_RATE * (-buy_fee(size))
        mult = (1 + wp / 100.0) * (1 - sd) / ((1 + lag_pct / 100.0) * (1 + bd))
        pnl = size * (mult - 1) - fees
        pnl = max(pnl, -size)  # capped loss
        total += (1 - TX_FAIL_RATE) * pnl + eff_fail
    return total


def main():
    print("=== EXECUTION FRICTION (bot's own round-trip cost, live-calibrated model) ===")
    print(f"{'size SOL':>9}{'buy slip%':>11}{'sell slip%':>11}{'fees %':>9}{'all-in %':>10}")
    for s in (0.05, 0.10, 0.25, 0.50, 1.0, 2.0):
        b, sd, fp, allin = exec_breakdown(s)
        print(f"{s:>9.2f}{b:>11.1f}{sd:>11.1f}{fp:>9.1f}{allin:>10.1f}")
    print("(curve assumed 30 SOL. Note: fees dominate at small size, slippage at large size.)\n")

    trades, n_rob = load_copy_trades()
    if not trades:
        print("no out-of-sample copy trades"); return
    print(f"=== BREAK-EVEN COPY-LAG (out-of-sample: {len(trades)} trades from {n_rob} robust wallets) ===")
    print("Max EXTRA entry slippage (from buying after them) the edge can absorb before EV<=0:\n")
    print(f"{'size SOL':>9}{'net@0% lag':>12}{'break-even lag%':>16}")
    for s in (0.10, 0.25, 0.50, 1.0):
        base = net_at(trades, s, 0.0)
        be = None
        lag = 0.0
        while lag <= 100:
            if net_at(trades, s, lag) <= 0:
                be = lag; break
            lag += 0.5
        print(f"{s:>9.2f}{base:>12.2f}{(f'{be:.1f}%' if be is not None else '>100%'):>16}")
    print("\nReading: the live follower measures our REAL copy-lag entry slippage. As long as")
    print("it stays UNDER the break-even above, the strategy keeps positive EV out-of-sample.")
    print("Larger size lifts net SOL (fees amortize) but real price-impact would erode the")
    print("break-even at sizes that are a meaningful fraction of the curve.")


if __name__ == "__main__":
    main()
