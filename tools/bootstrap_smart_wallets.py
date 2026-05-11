"""
tools/bootstrap_smart_wallets.py

One-time backfill of wallet_intel's smart-money classification from existing
historical data. Joins:

  logs/bot_wallets.json       wallet -> {buys, mints[]}   (~6K wallets)
  logs/counterfactual.jsonl   mint -> mc_delta_pct        (~7K mints)

…to produce per-wallet outcome lists, written to:

  logs/wallet_outcomes.json   {wallet: [pct1, pct2, ...]}

Then ranks and prints the top smart wallets + top noise wallets for sanity
check. The live bot's `wallet_intel._load_smart_money()` will pick this file
up on next restart; from then on `counterfactual.attribute_outcome()` keeps
it fresh incrementally.

Run:
    python -m tools.bootstrap_smart_wallets
    python -m tools.bootstrap_smart_wallets --min-outcomes 5    # looser
    python -m tools.bootstrap_smart_wallets --dry-run           # don't write
"""

from __future__ import annotations

import argparse
import json
import os
import sys

BOT_WALLETS_FILE      = "logs/bot_wallets.json"
COUNTERFACTUAL_FILE   = "logs/counterfactual.jsonl"
WALLET_OUTCOMES_FILE  = "logs/wallet_outcomes.json"

# Match wallet_intel.py defaults so we report what the live classifier will see
SMART_MIN_BUYS       = 10
SMART_WIN_PCT        = 0.60
PUMP_THRESHOLD       = 50.0
NOISE_MIN_BUYS       = 25
NOISE_MAX_WIN_PCT    = 0.30


def load_wallet_index(path: str) -> dict[str, list[str]]:
    """wallet -> list of mints they were observed buying (last 50)."""
    if not os.path.exists(path):
        print(f"[ERROR] {path} not found.", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    out: dict[str, list[str]] = {}
    for w, info in d.items():
        mints = info.get("mints") or []
        if isinstance(mints, list) and mints:
            out[w] = mints
    return out


def load_mint_outcomes(path: str) -> dict[str, float]:
    """mint -> mc_delta_pct (most recent if duplicated)."""
    if not os.path.exists(path):
        print(f"[ERROR] {path} not found.", file=sys.stderr)
        sys.exit(1)
    out: dict[str, float] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            mint = r.get("mint")
            pct  = r.get("mc_delta_pct")
            if mint is not None and pct is not None:
                # Last write wins — counterfactual rotates but order is preserved
                out[mint] = float(pct)
    return out


def build_outcomes(
    wallet_to_mints: dict[str, list[str]],
    mint_to_pct: dict[str, float],
) -> dict[str, list[float]]:
    """Walk every wallet, collect outcomes from any of its mints we have data for."""
    out: dict[str, list[float]] = {}
    for w, mints in wallet_to_mints.items():
        pcts = [mint_to_pct[m] for m in mints if m in mint_to_pct]
        if pcts:
            out[w] = pcts
    return out


def classify(outcomes: dict[str, list[float]], min_buys: int) -> dict[str, dict]:
    """Compute per-wallet stats and label. Returns wallet -> {n, wins, win_pct, label}."""
    rows: dict[str, dict] = {}
    for w, outs in outcomes.items():
        n = len(outs)
        if n < min_buys:
            label = "below-floor"
        else:
            wins = sum(1 for o in outs if o >= PUMP_THRESHOLD)
            win_pct = wins / n
            if win_pct >= SMART_WIN_PCT and n >= SMART_MIN_BUYS:
                label = "smart"
            elif n >= NOISE_MIN_BUYS and win_pct < NOISE_MAX_WIN_PCT:
                label = "noise"
            else:
                label = "unknown"
        wins = sum(1 for o in outs if o >= PUMP_THRESHOLD)
        rows[w] = {
            "n":       len(outs),
            "wins":    wins,
            "win_pct": wins / len(outs) if outs else 0.0,
            "label":   label,
        }
    return rows


def print_report(rows: dict[str, dict]) -> None:
    smart = [(w, r) for w, r in rows.items() if r["label"] == "smart"]
    noise = [(w, r) for w, r in rows.items() if r["label"] == "noise"]
    unknown = [(w, r) for w, r in rows.items() if r["label"] == "unknown"]
    below   = [(w, r) for w, r in rows.items() if r["label"] == "below-floor"]

    print("=" * 72)
    print(" SMART-MONEY BOOTSTRAP REPORT")
    print("=" * 72)
    print(f" Wallets with >=1 outcome:        {len(rows):>5}")
    print(f"   below floor (< {SMART_MIN_BUYS} outcomes):  {len(below):>5}")
    print(f"   smart (>={SMART_MIN_BUYS} & >={int(SMART_WIN_PCT*100)}% pumped):    {len(smart):>5}")
    print(f"   noise (>={NOISE_MIN_BUYS} & <{int(NOISE_MAX_WIN_PCT*100)}% pumped):    {len(noise):>5}")
    print(f"   unknown (in between):           {len(unknown):>5}")
    print()

    def _show(title: str, items: list[tuple[str, dict]], key, reverse=True, n=20):
        print(f" --- {title} (top {n}) ---")
        print(f"   {'wallet':<14} {'n':>4} {'wins':>5} {'win%':>6}")
        for w, r in sorted(items, key=key, reverse=reverse)[:n]:
            print(f"   {w[:12]}..  {r['n']:>4} {r['wins']:>5}  {r['win_pct']*100:>5.1f}%")
        print()

    if smart:
        _show("SMART (best win%)", smart, key=lambda x: (x[1]["win_pct"], x[1]["n"]))
    if noise:
        _show("NOISE (most volume + worst win%)", noise,
              key=lambda x: (x[1]["n"], -x[1]["win_pct"]))
    if unknown:
        _show("UNKNOWN (need more data - close to thresholds)", unknown,
              key=lambda x: x[1]["n"])
    print("=" * 72)


def write_outcomes(outcomes: dict[str, list[float]], path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(outcomes, f, ensure_ascii=True)
    os.replace(tmp, path)
    sz_kb = os.path.getsize(path) / 1024
    print(f" Wrote {path}  ({len(outcomes)} wallets, {sz_kb:.1f} KB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-outcomes", type=int, default=SMART_MIN_BUYS,
                    help=f"Minimum outcomes to qualify (default {SMART_MIN_BUYS})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print report but don't write wallet_outcomes.json")
    args = ap.parse_args()

    print("Loading wallet -> mints index...")
    w2m = load_wallet_index(BOT_WALLETS_FILE)
    print(f"  {len(w2m)} wallets with at least one mint recorded")

    print("Loading mint -> mc_delta_pct outcomes...")
    m2p = load_mint_outcomes(COUNTERFACTUAL_FILE)
    print(f"  {len(m2p)} mints with resolved outcomes")

    print("Joining...")
    outcomes = build_outcomes(w2m, m2p)
    print(f"  {len(outcomes)} wallets have >=1 outcome attributable\n")

    rows = classify(outcomes, args.min_outcomes)
    print_report(rows)

    if args.dry_run:
        print("--dry-run: not writing.")
        return 0

    # Persist only wallets that crossed min-buys so the file stays small and
    # the live classifier doesn't waste cycles on under-thresholded data.
    eligible = {w: outs for w, outs in outcomes.items() if len(outs) >= 5}
    write_outcomes(eligible, WALLET_OUTCOMES_FILE)
    print(" Next: restart the bot; wallet_intel._load_smart_money() picks this up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
