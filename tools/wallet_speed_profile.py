"""
tools/wallet_speed_profile.py

Which of the proven wallets are actually COPYABLE at our latency?

Friction analysis showed the edge dies if our copy-lag entry slippage exceeds
~20%. That risk is driven by how FAST these wallets trade: a wallet that flips a
token in seconds is a latency race we lose; one that holds minutes gives our
detect->execute pipeline room to land a similar fill.

This profiles each proven wallet from cached on-chain history:
  - closed-position count, win rate, net SOL
  - HOLD TIME distribution (first buy -> last sell) = the copyability signal
  - median entry price level (proxy for how early on the curve they enter)
  - a copyability verdict

Pure analysis on logs/_raw_txns. Run: python -m tools.wallet_speed_profile
"""

from __future__ import annotations

import json
import os
import statistics as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_CACHE = os.path.join(ROOT, "logs", "_raw_txns")
PROVEN = os.path.join(ROOT, "logs", "proven_wallets.json")
WSOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
ROUNDTRIP_TOL = 0.10
MIN_BUY_SOL = 0.05


def _native(tx, w):
    for a in tx.get("accountData", []):
        if a.get("account") == w:
            return a.get("nativeBalanceChange", 0) or 0
    return 0


def closed_positions(w, txns):
    """Per mint: spent, recv, tok_in/out, first_buy_ts, last_sell_ts."""
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
        sol = _native(t, w) / LAMPORTS
        ts = t.get("timestamp", 0)
        a = agg.setdefault(m, {"spent": 0.0, "recv": 0.0, "tin": 0.0, "tout": 0.0, "fbuy": None, "lsell": None})
        if d == "buy":
            a["spent"] += -sol if sol < 0 else 0.0; a["tin"] += qty
            if ts and (a["fbuy"] is None or ts < a["fbuy"]): a["fbuy"] = ts
        else:
            a["recv"] += sol if sol > 0 else 0.0; a["tout"] += qty
            if ts and (a["lsell"] is None or ts > a["lsell"]): a["lsell"] = ts
    out = []
    for m, a in agg.items():
        if a["tin"] > 0 and a["tout"] > 0 and abs(a["tout"] - a["tin"]) / a["tin"] <= ROUNDTRIP_TOL \
           and a["spent"] >= MIN_BUY_SOL and a["fbuy"] and a["lsell"] and a["lsell"] >= a["fbuy"]:
            out.append({"pnl": a["recv"] - a["spent"], "pct": (a["recv"] - a["spent"]) / a["spent"] * 100,
                        "hold_s": a["lsell"] - a["fbuy"], "entry_price": a["spent"] / a["tin"]})
    return out


def verdict(median_hold_s):
    if median_hold_s < 30:
        return "FAST-FLIP — uncopyable at our latency (10s lag >> hold)"
    if median_hold_s < 120:
        return "scalper — borderline; needs sub-5s detection"
    if median_hold_s < 900:
        return "scalper/swing — COPYABLE with fast detection"
    return "swing — COPYABLE, latency-tolerant"


def fmt_dur(s):
    s = int(s)
    if s < 90: return f"{s}s"
    if s < 5400: return f"{s//60}m"
    return f"{s//3600}h"


def main():
    proven = json.load(open(PROVEN))
    rows = []
    for w in proven:
        cp = os.path.join(RAW_CACHE, w + ".json")
        if not os.path.exists(cp):
            continue
        pos = closed_positions(w, json.load(open(cp)))
        if not pos:
            continue
        holds = sorted(p["hold_s"] for p in pos)
        rows.append({
            "w": w, "n": len(pos),
            "net": sum(p["pnl"] for p in pos),
            "win": sum(1 for p in pos if p["pnl"] > 0) / len(pos),
            "med_hold": st.median(holds),
            "p25_hold": holds[len(holds) // 4],
            "fast_share": sum(1 for h in holds if h < 30) / len(holds),
        })
    rows.sort(key=lambda r: r["net"], reverse=True)
    print(f"Copyability profile of {len(rows)} proven wallets (hold time = first buy -> last sell):\n")
    print(f"{'wallet':<14}{'n':>4}{'net SOL':>9}{'win%':>6}{'med hold':>10}{'p25 hold':>10}{'<30s%':>7}  verdict")
    print("-" * 110)
    for r in rows:
        print(f"{r['w'][:12]:<14}{r['n']:>4}{r['net']:>9.1f}{r['win']*100:>5.0f}%"
              f"{fmt_dur(r['med_hold']):>10}{fmt_dur(r['p25_hold']):>10}{r['fast_share']*100:>6.0f}%  {verdict(r['med_hold'])}")
    copyable = [r for r in rows if r["med_hold"] >= 120]
    print(f"\nCOPYABLE at sub-5s detection (median hold >= 2min): {len(copyable)} of {len(rows)}")
    print("Fast-flips (median hold < 30s) are a latency race we lose at 10s polling — argues for")
    print("Helius streaming, and/or restricting the copy roster to the slower-holding wallets.")


if __name__ == "__main__":
    main()
