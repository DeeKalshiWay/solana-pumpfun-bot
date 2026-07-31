"""
tools/copyable_revalidate.py

The +14.7 SOL out-of-sample holdout included trades from fast-flip wallets we
CANNOT actually copy at our latency (median hold 1-30s). This re-runs the
out-of-sample test and the friction break-even on ONLY the copyable wallets
(median hold >= MIN_HOLD_S), to see whether the REAL, capturable edge survives.

Pure analysis on logs/_raw_txns. Run: python -m tools.copyable_revalidate
"""

from __future__ import annotations

import json
import os
import statistics as st

from tools.wallet_speed_profile import closed_positions, RAW_CACHE
from tools.friction_analysis import net_at, side_drag

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROVEN = os.path.join(ROOT, "logs", "copyable_wallets.json")
MIN_HOLD_S = 120          # copyable = median hold >= 2 min (10s polling is fine)
TRAIN_MIN_N = 4
TRAIN_MIN_MEAN_XB = 15.0


def positions_with_time(w):
    cp = os.path.join(RAW_CACHE, w + ".json")
    if not os.path.exists(cp):
        return []
    # closed_positions returns pct + hold_s but not entry_ts; recompute entry_ts here
    txns = json.load(open(cp))
    # reuse closed_positions for pct/hold, and grab fbuy via a second pass
    pos = closed_positions(w, txns)
    return pos  # has pct, hold_s, entry_price, pnl


def _entry_times(w):
    """Return list of (entry_ts, pct) for closed positions — mirrors closed_positions but keeps ts."""
    from tools.wallet_speed_profile import _native, WSOL, LAMPORTS, ROUNDTRIP_TOL, MIN_BUY_SOL
    txns = json.load(open(os.path.join(RAW_CACHE, w + ".json")))
    agg = {}
    for t in txns:
        if t.get("source") not in ("PUMP_FUN", "PUMP_AMM", "PUMPSWAP") or t.get("type") != "SWAP":
            continue
        m = d = None; qty = 0.0
        for tt in t.get("tokenTransfers", []) or []:
            mm = tt.get("mint")
            if not mm or mm == WSOL:
                continue
            a = float(tt.get("tokenAmount") or 0)
            if a <= 0:
                continue
            if tt.get("toUserAccount") == w: m, d, qty = mm, "buy", a; break
            if tt.get("fromUserAccount") == w: m, d, qty = mm, "sell", a; break
        if not m:
            continue
        sol = _native(t, w) / LAMPORTS; ts = t.get("timestamp", 0)
        a = agg.setdefault(m, {"spent": 0.0, "recv": 0.0, "tin": 0.0, "tout": 0.0, "fbuy": None})
        if d == "buy":
            a["spent"] += -sol if sol < 0 else 0.0; a["tin"] += qty
            if ts and (a["fbuy"] is None or ts < a["fbuy"]): a["fbuy"] = ts
        else:
            a["recv"] += sol if sol > 0 else 0.0; a["tout"] += qty
    out = []
    for m, a in agg.items():
        if a["tin"] > 0 and a["tout"] > 0 and abs(a["tout"] - a["tin"]) / a["tin"] <= ROUNDTRIP_TOL \
           and a["spent"] >= MIN_BUY_SOL and a["fbuy"]:
            out.append((a["fbuy"], (a["recv"] - a["spent"]) / a["spent"] * 100))
    return out


def main():
    proven = json.load(open(PROVEN))
    copyable = []
    for w in proven:
        pos = positions_with_time(w)
        if pos and st.median([p["hold_s"] for p in pos]) >= MIN_HOLD_S:
            copyable.append(w)
    print(f"Copyable wallets (median hold >= {MIN_HOLD_S}s): {len(copyable)} of {len(proven)}")
    print(f"  {[w[:8] for w in copyable]}\n")
    if not copyable:
        print("none copyable"); return

    # out-of-sample split on copyable-only trades
    wallet_trades = {w: _entry_times(w) for w in copyable}
    all_ts = [ts for v in wallet_trades.values() for ts, _ in v]
    T = st.median(all_ts)
    import datetime as dt
    print(f"Split T = {dt.datetime.fromtimestamp(T, dt.UTC):%Y-%m-%d %H:%M}Z (median copyable-trade time)\n")

    train_robust = []
    for w, ts_pct in wallet_trades.items():
        tr = [p for t, p in ts_pct if t < T]
        if len(tr) < TRAIN_MIN_N:
            continue
        xb = (sum(sorted(tr)[:-1]) / (len(tr) - 1)) if len(tr) > 1 else tr[0]
        if sum(tr) > 0 and xb > TRAIN_MIN_MEAN_XB:   # net% positive & robust
            train_robust.append(w)

    test = [p for w in train_robust for t, p in wallet_trades[w] if t >= T]
    print(f"Train-robust copyable wallets: {len(train_robust)} | out-of-sample (post-T) trades: {len(test)}")
    if test:
        win = sum(1 for x in test if x > 0) / len(test)
        print(f"  OUT-OF-SAMPLE raw: mean {st.mean(test):+.1f}% | median {st.median(test):+.1f}% | win {win*100:.0f}%")
        print("\n  Net SOL after friction (copy-lag entry slippage swept), 0.10 SOL/trade:")
        for lag in (0, 5, 10, 15, 20):
            print(f"    lag {lag:>2}%:  {net_at(test, 0.10, lag):+.3f} SOL  (EV {net_at(test,0.10,lag)/len(test):+.4f}/trade)")
    else:
        print("  no post-T copyable trades from train-robust wallets (sample too thin)")

    # also: full-history (in-sample) copyable performance for context
    allp = [p for w in copyable for _, p in wallet_trades[w]]
    print(f"\nContext — ALL copyable trades (in-sample, {len(allp)}): mean {st.mean(allp):+.1f}% | "
          f"win {sum(1 for x in allp if x>0)/len(allp)*100:.0f}% | net@0.10/0%lag {net_at(allp,0.10,0):+.2f} SOL")


if __name__ == "__main__":
    main()
