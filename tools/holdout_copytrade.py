"""
tools/holdout_copytrade.py

Out-of-sample test of the copy-trade thesis. Selecting wallets because they were
profitable over their whole history is look-ahead bias. This asks the decision-
relevant question instead:

  If, as of a past split date T, I had picked the "robust" wallets using ONLY
  their pre-T trades, would copying them on their POST-T trades have made money?

Reads cached raw txns (logs/_raw_txns/), reconstructs each closed (round-tripped)
position with an entry timestamp, splits globally at T (default: median position
time), classifies wallets robust on the train side, then measures the test-side
realized PnL of those train-robust wallets vs the whole population.

No API calls. Run: python -m tools.holdout_copytrade
"""

from __future__ import annotations

import json
import os
import statistics as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_CACHE = os.path.join(ROOT, "logs", "_raw_txns")
WSOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
MIN_BUY_SOL = 0.05
ROUNDTRIP_TOL = 0.10

# Train-side robustness bars (looser n because we only see pre-T trades)
TRAIN_MIN_N = 5
TRAIN_MIN_MEAN_XB = 15.0   # mean % after dropping best trade


def _native(tx, w):
    for a in tx.get("accountData", []):
        if a.get("account") == w:
            return a.get("nativeBalanceChange", 0) or 0
    return 0


def _mint_dir_qty(tx, w):
    for tt in tx.get("tokenTransfers", []) or []:
        m = tt.get("mint")
        if not m or m == WSOL:
            continue
        amt = float(tt.get("tokenAmount") or 0)
        if tt.get("toUserAccount") == w:
            return m, "buy", amt
        if tt.get("fromUserAccount") == w:
            return m, "sell", amt
    return None, None, 0.0


def positions_for(w, txns):
    """Return list of closed positions: dict(mint, entry_ts, pct, pnl_sol, spent)."""
    agg = {}
    for t in txns:
        if t.get("source") not in ("PUMP_FUN", "PUMP_AMM", "PUMPSWAP") or t.get("type") != "SWAP":
            continue
        m, d, qty = _mint_dir_qty(t, w)
        if not m:
            continue
        sol = _native(t, w) / LAMPORTS
        ts = t.get("timestamp", 0)
        a = agg.setdefault(m, {"spent": 0.0, "recv": 0.0, "tin": 0.0, "tout": 0.0, "first_buy_ts": None})
        if d == "buy":
            a["spent"] += -sol if sol < 0 else 0.0
            a["tin"] += qty
            if a["first_buy_ts"] is None or (ts and ts < a["first_buy_ts"]):
                a["first_buy_ts"] = ts
        else:
            a["recv"] += sol if sol > 0 else 0.0
            a["tout"] += qty
    out = []
    for m, a in agg.items():
        if a["tin"] > 0 and a["tout"] > 0 and abs(a["tout"] - a["tin"]) / a["tin"] <= ROUNDTRIP_TOL \
           and a["spent"] >= MIN_BUY_SOL and a["first_buy_ts"]:
            pnl = a["recv"] - a["spent"]
            out.append({"mint": m, "entry_ts": a["first_buy_ts"], "pnl_sol": pnl,
                        "pct": pnl / a["spent"] * 100, "spent": a["spent"]})
    return out


def _mean_drop_best(pcts):
    if len(pcts) < 2:
        return pcts[0] if pcts else 0.0
    r = list(pcts); r.remove(max(r))
    return sum(r) / len(r)


def main():
    if not os.path.isdir(RAW_CACHE):
        raise SystemExit("No raw cache — run tools.wallet_pnl_helius first.")
    # Build all closed positions tagged by wallet
    by_wallet = {}
    all_ts = []
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
    if not all_ts:
        raise SystemExit("No closed positions found.")
    T = st.median(all_ts)
    import datetime as dt
    print(f"Wallets with closed positions: {len(by_wallet)} | total positions: {len(all_ts)}")
    print(f"Split date T = {dt.datetime.utcfromtimestamp(T):%Y-%m-%d %H:%M} UTC (median position time)\n")

    # Classify on train (pre-T), evaluate on test (post-T)
    train_robust = []
    for w, ps in by_wallet.items():
        tr = [p for p in ps if p["entry_ts"] < T]
        if len(tr) < TRAIN_MIN_N:
            continue
        net_tr = sum(p["pnl_sol"] for p in tr)
        mxb = _mean_drop_best([p["pct"] for p in tr])
        if net_tr > 0 and mxb > TRAIN_MIN_MEAN_XB:
            train_robust.append(w)

    def test_stats(wallets):
        pos = [p for w in wallets for p in by_wallet[w] if p["entry_ts"] >= T]
        if not pos:
            return 0, 0.0, 0.0, 0.0
        net = sum(p["pnl_sol"] for p in pos)
        pcts = [p["pct"] for p in pos]
        winr = sum(1 for x in pcts if x > 0) / len(pcts)
        return len(pos), net, st.mean(pcts), winr

    n_rob, net_rob, mean_rob, win_rob = test_stats(train_robust)
    n_all, net_all, mean_all, win_all = test_stats(list(by_wallet))

    print(f"TRAIN-ROBUST wallets (robust using only pre-T trades): {len(train_robust)}")
    print("\n--- OUT-OF-SAMPLE (post-T) realized performance ---")
    print(f"{'group':<22}{'positions':>10}{'net SOL':>10}{'mean %':>9}{'win rate':>10}")
    print(f"{'train-robust wallets':<22}{n_rob:>10}{net_rob:>10.1f}{mean_rob:>9.1f}{win_rob*100:>9.0f}%")
    print(f"{'all wallets (baseline)':<22}{n_all:>10}{net_all:>10.1f}{mean_all:>9.1f}{win_all*100:>9.0f}%")
    print("\nVERDICT:", end=" ")
    if net_rob > 0 and mean_rob > 0 and mean_rob > mean_all:
        print("PASS — wallets picked as robust BEFORE T were profitable AFTER T, and beat the population. Copy signal is predictive.")
    elif net_rob > 0:
        print("WEAK PASS — train-robust wallets were post-T positive but not clearly above baseline. Marginal edge.")
    else:
        print("FAIL — robustness did not persist out-of-sample. Copy signal is descriptive, not predictive.")


if __name__ == "__main__":
    main()
