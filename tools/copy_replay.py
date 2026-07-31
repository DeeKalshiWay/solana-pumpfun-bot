"""
tools/copy_replay.py

Replay every round-tripped trade of the copyable proven wallets through the
CURRENT strategy rules (corrected price math, dynamic sizing, -30% stop,
live-calibrated friction). Writes synthetic open/close events to the same
`logs/copy_follower_trades.jsonl` the live follower uses, so the dashboard
renders the result immediately. Each event is tagged `source: replay` so we
can tell backtest from live going forward.

Friction model matches `tools/friction_analysis.py` for size 0.10-0.25 SOL on
a typical 30-SOL curve (~10% all-in round-trip = entry slip + exit slip + fees).
The stop is applied to the wallet's REALIZED pct: anything worse than -30%
clamps to -30% before friction (modeling "we'd have stopped out at -30%").

Pure replay, no API calls. Run: python -m tools.copy_replay
"""

from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_CACHE = os.path.join(ROOT, "logs", "_raw_txns")
COPYABLE = os.path.join(ROOT, "logs", "copyable_wallets.json")
TRADES_LOG = os.path.join(ROOT, "logs", "copy_follower_trades.jsonl")
STATE_FILE = os.path.join(ROOT, "logs", "copy_follower_state.json")

# Live-calibrated friction at 0.10-0.25 SOL on a 30-SOL curve (matches
# friction_analysis): buy slip ~3.3% + sell slip ~3.3% + fees ~2-4% ≈ 10% round-trip.
ALL_IN_FRICTION_PCT = 10.0
STOP_LOSS_PCT = 30.0
FEE_PCT = 1.5  # already inside ALL_IN; kept here for parity with live close events

# Sizing
RISK_PCT = 0.02
MIN_SIZE_SOL = 0.05
MAX_SIZE_SOL = 0.50
MAX_OPEN_EXPOSURE = 0.25

# Account (per memory/reference_sol_price.md — operator-set $85/SOL)
SEED_USD = 500.0
SOL_PRICE_USD = 85.0


WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = {WSOL, USDC, USDT}
LAMPORTS = 1_000_000_000
ROUNDTRIP_TOL = 0.10
MIN_BUY_SOL = 0.05


def _native(tx, w):
    for a in tx.get("accountData", []) or []:
        if a.get("account") == w:
            return a.get("nativeBalanceChange", 0) or 0
    return 0


def positions_full(w, txns):
    """Closed positions with entry_ts, sell_ts, hold_s, raw pct, mint."""
    agg = {}
    for t in txns:
        if t.get("source") not in ("PUMP_FUN", "PUMP_AMM", "PUMPSWAP") or t.get("type") != "SWAP":
            continue
        m = d = None; qty = 0.0
        for tt in t.get("tokenTransfers", []) or []:
            mm = tt.get("mint")
            if not mm or mm in QUOTE_MINTS:  # exclude WSOL/USDC/USDT — these are the QUOTE side, not the asset
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
        a = agg.setdefault(m, {"spent": 0.0, "recv": 0.0, "tin": 0.0, "tout": 0.0,
                                "fbuy": None, "lsell": None})
        if d == "buy":
            a["spent"] += -sol if sol < 0 else 0.0
            a["tin"] += qty
            if ts and (a["fbuy"] is None or ts < a["fbuy"]): a["fbuy"] = ts
        else:
            a["recv"] += sol if sol > 0 else 0.0
            a["tout"] += qty
            if ts and (a["lsell"] is None or ts > a["lsell"]): a["lsell"] = ts
    out = []
    for m, a in agg.items():
        if (a["tin"] > 0 and a["tout"] > 0
            and abs(a["tout"] - a["tin"]) / a["tin"] <= ROUNDTRIP_TOL
            and a["spent"] >= MIN_BUY_SOL
            and a["fbuy"] and a["lsell"] and a["lsell"] >= a["fbuy"]):
            pct = (a["recv"] - a["spent"]) / a["spent"] * 100
            out.append({
                "mint": m, "entry_ts": a["fbuy"], "exit_ts": a["lsell"],
                "hold_s": a["lsell"] - a["fbuy"],
                "pct_raw": pct,
                "their_spent": a["spent"], "their_recv": a["recv"],
            })
    return out


def _size_for_trade(balance_sol, committed_sol):
    if balance_sol <= 0:
        return 0.0, "no_funds"
    base = balance_sol * RISK_PCT / (STOP_LOSS_PCT / 100.0)
    target = max(MIN_SIZE_SOL, min(MAX_SIZE_SOL, base))
    free = balance_sol - committed_sol
    if free < MIN_SIZE_SOL:
        return 0.0, "no_funds"
    room = max(0.0, balance_sol * MAX_OPEN_EXPOSURE - committed_sol)
    if room < MIN_SIZE_SOL:
        return 0.0, "exposure_cap"
    size = min(target, free, room)
    if size < MIN_SIZE_SOL:
        return 0.0, "min_floor"
    return round(size, 4), "ok"


def main():
    import argparse, statistics as st
    ap = argparse.ArgumentParser()
    ap.add_argument("--oos", action="store_true",
                    help="HONEST mode: split at median entry_ts; qualify wallets on pre-T data; replay ONLY post-T trades")
    args = ap.parse_args()

    copyable = json.load(open(COPYABLE))
    by_wallet = {}
    for w in copyable:
        cp = os.path.join(RAW_CACHE, w + ".json")
        if not os.path.exists(cp):
            continue
        for p in positions_full(w, json.load(open(cp))):
            p["wallet"] = w
            by_wallet.setdefault(w, []).append(p)
    all_trades = [p for ps in by_wallet.values() for p in ps]

    if args.oos:
        T = st.median(p["entry_ts"] for p in all_trades)
        qualified = []
        for w, ps in by_wallet.items():
            pre = [p for p in ps if p["entry_ts"] < T]
            if len(pre) < 4:
                continue
            pcts = [p["pct_raw"] for p in pre]
            holds = [p["hold_s"] for p in pre]
            # apply the SAME concentration + copyability rule used to pick the roster,
            # but on PRE-T data only — no look-ahead.
            mean_drop_best = (sum(sorted(pcts)[:-1]) / (len(pcts) - 1)) if len(pcts) > 1 else pcts[0]
            if sum(pcts) > 0 and mean_drop_best > 15 and st.median(holds) >= 120:
                qualified.append(w)
        trades = [p for w in qualified for p in by_wallet[w] if p["entry_ts"] >= T]
        trades.sort(key=lambda p: p["entry_ts"])
        import datetime as dt
        print(f"OUT-OF-SAMPLE: split at {dt.datetime.fromtimestamp(T, dt.UTC):%Y-%m-%d %H:%MZ}")
        print(f"  qualified wallets (pre-T robust + copyable): {len(qualified)} / {len(by_wallet)}")
        print(f"  post-T trades for replay: {len(trades)}\n")
    else:
        trades = sorted(all_trades, key=lambda p: p["entry_ts"])
        print(f"IN-SAMPLE replay of {len(trades)} trades across {len(by_wallet)} wallets (selection-biased — for comparison only)")

    seed_sol = round(SEED_USD / SOL_PRICE_USD, 5)
    balance = seed_sol
    log = []
    open_pos = {}     # mint -> {size_sol}  (model exposure)
    closed = 0; wins = 0; stops = 0; skipped = 0

    for t in trades:
        # Free up positions that closed before this trade's entry
        for m in list(open_pos.keys()):
            if open_pos[m]["exit_ts"] <= t["entry_ts"]:
                del open_pos[m]

        committed = sum(p["size_sol"] for p in open_pos.values())
        size, reason = _size_for_trade(balance, committed)
        if reason != "ok":
            skipped += 1
            log.append({"event": "skip", "reason": reason, "mint": t["mint"],
                        "wallet": t["wallet"], "balance_sol": round(balance, 4),
                        "ts": t["entry_ts"], "source": "replay"})
            continue

        # Apply stop + friction to the wallet's realized %
        raw = t["pct_raw"]
        stopped = raw < -STOP_LOSS_PCT
        gross = max(-STOP_LOSS_PCT, raw)
        net = gross - ALL_IN_FRICTION_PCT
        pnl = size * net / 100.0
        balance += pnl
        if pnl > 0: wins += 1
        if stopped: stops += 1
        closed += 1
        open_pos[t["mint"]] = {"size_sol": size, "exit_ts": t["exit_ts"]}

        log.append({"event": "open", "mint": t["mint"], "wallet": t["wallet"],
                    "their_entry": 1.0, "our_entry": 1.0,
                    "entry_slip_pct": 0.0, "slip_suspect": False, "rug_flag": 0,
                    "size_sol": size,
                    "size_basis": {"balance_sol": round(balance - pnl, 4),
                                    "committed_sol": round(committed, 4),
                                    "conviction": 1},
                    "ts": t["entry_ts"], "source": "replay"})
        log.append({"event": "close", "mint": t["mint"], "wallet": t["wallet"],
                    "our_entry": 1.0, "our_exit": 1.0,
                    "gross_pct": round(gross, 2), "net_pct": round(net, 2),
                    "pnl_sol": round(pnl, 5), "clamped": False,
                    "hold_s": int(t["hold_s"]),
                    "conviction": 1,
                    "exit_reason": "stop_loss" if stopped else "wallet_sell",
                    "ts": t["exit_ts"], "source": "replay"})

    # Write fresh log + seed state
    with open(TRADES_LOG, "w", encoding="utf-8") as f:
        for r in log:
            f.write(json.dumps(r) + "\n")
    state = {"seen_sigs": {}, "open": {},
             "account": {"seed_usd": SEED_USD, "sol_price_usd": SOL_PRICE_USD,
                         "seed_sol": seed_sol, "skipped_insufficient": 0}}
    json.dump(state, open(STATE_FILE, "w"))

    win_rate = wins / closed * 100 if closed else 0
    ret = (balance / seed_sol - 1) * 100
    print(f"\n=== REPLAY RESULT (seed ${SEED_USD} = {seed_sol} SOL, friction {ALL_IN_FRICTION_PCT}%, stop -{STOP_LOSS_PCT}%) ===")
    print(f"trades replayed: {closed}  | wins {wins} ({win_rate:.0f}%)  | stops {stops}  | skipped {skipped}")
    print(f"final balance:   {balance:.4f} SOL  (${balance * SOL_PRICE_USD:.2f})")
    print(f"return:          {ret:+.1f}%  (net {balance - seed_sol:+.4f} SOL)")
    # Sanity: how much rests on the top winner?
    pnls = sorted((r["pnl_sol"] for r in log if r.get("event") == "close"), reverse=True)
    top1 = pnls[0] if pnls else 0
    total = sum(pnls)
    print(f"top-1 trade:     {top1:+.4f} SOL  ({top1/total*100 if total else 0:.0f}% of total)")
    print(f"sum w/o top-1:   {sum(pnls[1:]):+.4f} SOL")


if __name__ == "__main__":
    main()
