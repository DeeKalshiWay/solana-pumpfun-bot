"""
tools/creator_audit.py

Detect self-ruggers: wallets that CREATE pump.fun tokens and also TRADE them.
A wallet whose trading edge comes from rugging tokens it created looks
profitable on-chain (they sell at the top of their own pump) but is poison
for copy-traders — we'd be their exit liquidity.

Cheap detection: a wallet's own tx history shows both:
  - type=CREATE source=PUMP_FUN — they minted/initialized a token
  - type=SWAP source=PUMP_FUN — they traded a token
If those sets overlap >= MIN_SELF_RATIO, blacklist.

Run standalone to audit cached wallets:
    python -m tools.creator_audit               # report only
    python -m tools.creator_audit --add         # add offenders to blacklist
"""

from __future__ import annotations

import argparse
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_CACHE = os.path.join(ROOT, "logs", "_raw_txns")
BLACKLIST = os.path.join(ROOT, "logs", "_bleeders_blacklist.json")
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE = {WSOL, USDC, USDT}

# Thresholds tuned to catch obvious self-ruggers without false-flagging
# legitimate devs who occasionally launch tokens but mostly trade.
MIN_CREATES = 3              # must have created >= this many tokens
MIN_SELF_RATIO = 0.30        # >= 30% of traded mints must be wallet's own creations
PUMP_SOURCES = {"PUMP_FUN", "PUMP_AMM", "PUMPSWAP"}


def detect_self_rugger(wallet: str, txns: list[dict]) -> tuple[bool, dict]:
    """Returns (is_rugger, stats_dict). stats_dict has counts even if not a rugger."""
    created_mints: set[str] = set()
    traded_mints: set[str] = set()

    for t in txns:
        if t.get("source") not in PUMP_SOURCES:
            continue
        ty = t.get("type")
        if ty == "CREATE":
            # The wallet appears as creator if they're the feePayer of the
            # CREATE tx (pump.fun's creator field is the tx initiator).
            if t.get("feePayer") == wallet:
                for tt in t.get("tokenTransfers", []) or []:
                    m = tt.get("mint")
                    if m and m not in QUOTE:
                        created_mints.add(m)
        elif ty == "SWAP":
            for tt in t.get("tokenTransfers", []) or []:
                m = tt.get("mint")
                if not m or m in QUOTE:
                    continue
                if tt.get("toUserAccount") == wallet or tt.get("fromUserAccount") == wallet:
                    traded_mints.add(m)
                    break

    self_traded = created_mints & traded_mints
    self_ratio = len(self_traded) / len(traded_mints) if traded_mints else 0.0
    stats = {
        "created": len(created_mints),
        "traded": len(traded_mints),
        "self_traded": len(self_traded),
        "self_ratio": round(self_ratio, 2),
    }
    is_rugger = (len(created_mints) >= MIN_CREATES and self_ratio >= MIN_SELF_RATIO)
    return is_rugger, stats


def load_blacklist() -> set[str]:
    if not os.path.exists(BLACKLIST):
        return set()
    try:
        return set(json.load(open(BLACKLIST)))
    except Exception:
        return set()


def save_blacklist(addrs: set[str]):
    json.dump(sorted(addrs), open(BLACKLIST, "w"), indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", action="store_true",
                    help="add detected ruggers to logs/_bleeders_blacklist.json")
    ap.add_argument("--wallet", type=str, help="audit a single wallet address only")
    args = ap.parse_args()

    blacklist = load_blacklist()
    detected = []
    audited = 0

    if args.wallet:
        cp = os.path.join(RAW_CACHE, args.wallet + ".json")
        if not os.path.exists(cp):
            raise SystemExit(f"no cache for {args.wallet}")
        ok, stats = detect_self_rugger(args.wallet, json.load(open(cp)))
        print(f"{args.wallet[:12]}: {stats}  rugger={ok}")
        if ok and args.add and args.wallet not in blacklist:
            blacklist.add(args.wallet)
            detected.append(args.wallet)
        if args.add and detected:
            save_blacklist(blacklist)
            print(f"added {len(detected)} -> {BLACKLIST}")
        return

    print(f"auditing all cached wallets in {RAW_CACHE}")
    for f in sorted(os.listdir(RAW_CACHE)):
        if not f.endswith(".json"):
            continue
        w = f[:-5]
        try:
            txns = json.load(open(os.path.join(RAW_CACHE, f)))
        except Exception:
            continue
        audited += 1
        ok, stats = detect_self_rugger(w, txns)
        if ok:
            already = "(already blacklisted)" if w in blacklist else ""
            print(f"  RUGGER {w[:12]}  creates={stats['created']} traded={stats['traded']} "
                  f"self={stats['self_traded']} ratio={stats['self_ratio']:.2f}  {already}")
            if w not in blacklist:
                detected.append(w)

    print(f"\naudited {audited} wallets, found {len(detected)} new self-ruggers")
    if args.add and detected:
        for w in detected:
            blacklist.add(w)
        save_blacklist(blacklist)
        print(f"added {len(detected)} -> {BLACKLIST}")
    elif detected and not args.add:
        print("(re-run with --add to write to blacklist)")


if __name__ == "__main__":
    main()
