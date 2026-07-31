"""
tools/discover_edge_wallets.py

Find MORE 6RrqZQ7W-class wallets so the strategy isn't single-wallet-fragile.

Loose criteria (the old `copyable` filter) found 5 wallets but only 1 actually
clears the 10% friction. This applies the STRICT, edge-defining criterion:

  n_closed       >= 50           (meaningful sample)
  span_days      >= 14           (not a single-day snapshot)
  mean_pct       >= 25%          (substantially above 10% friction)
  win_rate       >= 60%          (solid hit rate)
  mean_w/o_best  >= 20%          (concentration-robust)
  median_hold    >= 120s         (copyable at our 15s polling latency)

Scans every wallet in `logs/_raw_txns/` (~3,900 cached during prior discovery
passes) and ranks the ones that clear the bar.

Phase 1 (no API): rank-from-cache. Reports qualifiers + close-but-no-cigar.
Phase 2 (--deepen, optional): for the close-but-no-cigar list, deepen Helius
history and re-rank — catches wallets whose shallow cache understated their n.

Output: logs/_edge_wallets.json  (just the qualifying addresses)

Run: python -m tools.discover_edge_wallets [--deepen]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics as st

from tools.copy_replay import positions_full, RAW_CACHE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "logs", "_edge_wallets.json")

# Strict criteria — calibrated to 6RrqZQ7W's profile (n=167, mean +39.7%, win 71%,
# mean-xb +35.4%, span 120d, hold 176s). "Find more like this."
MIN_N = 50
MIN_SPAN_DAYS = 14
MIN_MEAN_PCT = 25.0
MIN_WIN_RATE = 0.60
MIN_MEAN_DROP_BEST = 20.0
MIN_MEDIAN_HOLD_S = 120

# Borderline = within these of every threshold; might pass with more history
BORDERLINE_DROP_FRAC = 0.7   # achieves >= 70% of each threshold


def profile_wallet(w: str, txns: list[dict]) -> dict | None:
    ps = positions_full(w, txns)
    if not ps:
        return None
    pcts = [p["pct_raw"] for p in ps]
    holds = [p["hold_s"] for p in ps]
    ts = [p["entry_ts"] for p in ps]
    n = len(pcts)
    mean = st.mean(pcts)
    win = sum(1 for x in pcts if x > 0) / n
    mxb = (sum(sorted(pcts)[:-1]) / (n - 1)) if n > 1 else pcts[0]
    span_days = (max(ts) - min(ts)) / 86400 if ts else 0
    med_hold = st.median(holds)
    return {
        "wallet": w, "n": n, "mean_pct": mean, "win_rate": win,
        "mean_drop_best": mxb, "span_days": span_days, "med_hold_s": med_hold,
    }


def passes_strict(p: dict) -> bool:
    return (p["n"] >= MIN_N
            and p["span_days"] >= MIN_SPAN_DAYS
            and p["mean_pct"] >= MIN_MEAN_PCT
            and p["win_rate"] >= MIN_WIN_RATE
            and p["mean_drop_best"] >= MIN_MEAN_DROP_BEST
            and p["med_hold_s"] >= MIN_MEDIAN_HOLD_S)


def is_borderline(p: dict) -> bool:
    # Achieves most-but-not-all bars at >= 70% of threshold
    if passes_strict(p):
        return False
    hits = sum([
        p["n"] >= MIN_N * BORDERLINE_DROP_FRAC,
        p["span_days"] >= MIN_SPAN_DAYS * BORDERLINE_DROP_FRAC,
        p["mean_pct"] >= MIN_MEAN_PCT * BORDERLINE_DROP_FRAC,
        p["win_rate"] >= MIN_WIN_RATE * BORDERLINE_DROP_FRAC,
        p["mean_drop_best"] >= MIN_MEAN_DROP_BEST * BORDERLINE_DROP_FRAC,
        p["med_hold_s"] >= MIN_MEDIAN_HOLD_S * BORDERLINE_DROP_FRAC,
    ])
    return hits >= 5   # missed at most one bar


def fmt_p(p: dict, pass_=False):
    mark = "*" if pass_ else " "
    return (f"  {mark}{p['wallet'][:12]:<13} n={p['n']:>4} mean={p['mean_pct']:>+6.1f}% "
            f"win={p['win_rate']*100:>4.0f}% mxb={p['mean_drop_best']:>+6.1f}% "
            f"span={p['span_days']:>5.1f}d  hold={int(p['med_hold_s'])}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deepen", action="store_true",
                    help="Deepen Helius history on borderline candidates and re-rank")
    args = ap.parse_args()

    files = [f for f in os.listdir(RAW_CACHE) if f.endswith(".json")]
    print(f"Phase 1: scanning {len(files)} cached wallets against strict criterion")
    print(f"  n>={MIN_N}, span>={MIN_SPAN_DAYS}d, mean>={MIN_MEAN_PCT}%, "
          f"win>={MIN_WIN_RATE*100:.0f}%, mxb>={MIN_MEAN_DROP_BEST}%, hold>={MIN_MEDIAN_HOLD_S}s\n")

    qualifiers, borderline = [], []
    for f in files:
        w = f[:-5]
        try:
            txns = json.load(open(os.path.join(RAW_CACHE, f)))
        except Exception:
            continue
        p = profile_wallet(w, txns)
        if not p:
            continue
        if passes_strict(p):
            qualifiers.append(p)
        elif is_borderline(p):
            borderline.append(p)

    qualifiers.sort(key=lambda p: p["mean_drop_best"], reverse=True)
    borderline.sort(key=lambda p: p["mean_drop_best"], reverse=True)

    print(f"=== QUALIFIERS ({len(qualifiers)}) ===")
    for p in qualifiers:
        print(fmt_p(p, True))
    print(f"\n=== BORDERLINE — close-but-no-cigar ({len(borderline)}; top 25) ===")
    for p in borderline[:25]:
        print(fmt_p(p, False))

    if args.deepen and borderline:
        # Phase 2: deepen + re-rank the top borderlines
        from tools.fetch_deeper import fetch_deep
        from dotenv import dotenv_values
        key = dotenv_values(os.path.join(ROOT, ".env")).get("HELIUS_API_KEY", "")
        if not key:
            print("\nNo HELIUS_API_KEY for --deepen")
        else:
            print(f"\n=== PHASE 2: deepening top {min(20, len(borderline))} borderlines ===")
            for p in borderline[:20]:
                w = p["wallet"]
                txns = fetch_deep(w, key, max_pages=25, sleep=0.08)
                json.dump(txns, open(os.path.join(RAW_CACHE, w + ".json"), "w"))
                p2 = profile_wallet(w, txns)
                if not p2:
                    continue
                if passes_strict(p2):
                    print(fmt_p(p2, True) + "  <- NOW QUALIFIES")
                    qualifiers.append(p2)
                else:
                    print(fmt_p(p2))

    json.dump([p["wallet"] for p in qualifiers], open(OUT, "w"), indent=1)
    print(f"\nWrote {len(qualifiers)} edge wallet(s) to {OUT}")


if __name__ == "__main__":
    main()
