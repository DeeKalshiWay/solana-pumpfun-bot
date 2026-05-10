"""
tools/holdout_validate.py

Train/test split validation of every score-rejection filter.

The bot logs every rejected signal to logs/counterfactual.jsonl and polls
its outcome 10 minutes later. Over 7,000+ records that's a real dataset.
Until now we eyeballed aggregates and called filters "validated" without
out-of-sample testing — which is exactly the trap survivorship bias sets.

This script splits the counterfactual data chronologically:
    first 70%  →  training set (the data the filters were tuned against)
    last 30%   →  held-out test set (filters never saw this)

For each rejection reason it computes:
    rug_rate  = fraction of rejections that fell >= 50% (filter worked)
    pump_rate = fraction that rose >= 100% (filter cost us)

A filter is "validated" iff its rug-rate on the HELD-OUT set materially
beats the base rate (across all rejections) and the 95% Wilson CI on
the difference excludes 0. Anything else is overfit pattern recognition.

Usage:
    python -m tools.holdout_validate
    python -m tools.holdout_validate --threshold 50  # different rug %

Writes a markdown report to logs/holdout_validation.md.
"""

import argparse
import json
import math
import os
from collections import defaultdict
from typing import Iterable


# ── Wilson score interval (better than normal-approx for small n / extreme p) ──
def _wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson CI for proportion k/n. Returns (lo, hi)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    spread = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def _split_chronological(records: list[dict], train_frac: float = 0.7) -> tuple[list, list]:
    """Sort by reject_ts and cut chronologically. No shuffling — order matters."""
    ordered = sorted(records, key=lambda r: r.get("reject_ts", 0))
    cut = int(len(ordered) * train_frac)
    return ordered[:cut], ordered[cut:]


def _classify(records: Iterable[dict], rug_pct: float, pump_pct: float) -> dict:
    """Return counts: total / rugged / pumped per reason."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "rug": 0, "pump": 0})
    for r in records:
        reason = r.get("reason", "unknown")
        delta  = r.get("mc_delta_pct", 0)
        out[reason]["n"]   += 1
        if delta <= -rug_pct:
            out[reason]["rug"] += 1
        if delta >= pump_pct:
            out[reason]["pump"] += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--counterfactual", default="logs/counterfactual.jsonl")
    ap.add_argument("--output",         default="analytics/holdout_validation.md")
    ap.add_argument("--rug-threshold",  type=float, default=50.0,
                    help="mc_delta_pct <= -X considered a rug")
    ap.add_argument("--pump-threshold", type=float, default=100.0,
                    help="mc_delta_pct >= X considered a pump (filter cost us)")
    ap.add_argument("--train-frac",     type=float, default=0.7)
    ap.add_argument("--min-n-test",     type=int,   default=30,
                    help="reject categories with fewer test samples are skipped")
    args = ap.parse_args()

    if not os.path.exists(args.counterfactual):
        print(f"[HOLDOUT] {args.counterfactual} not found — run the bot to accumulate data first")
        return

    records = []
    with open(args.counterfactual, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    if not records:
        print(f"[HOLDOUT] no records in {args.counterfactual}")
        return

    train, test = _split_chronological(records, args.train_frac)
    train_stats = _classify(train, args.rug_threshold, args.pump_threshold)
    test_stats  = _classify(test,  args.rug_threshold, args.pump_threshold)

    # Base rate on the HELD-OUT set — what fraction of all rejections rugged?
    total_test = sum(s["n"]   for s in test_stats.values())
    total_rug  = sum(s["rug"] for s in test_stats.values())
    base_rate  = total_rug / total_test if total_test else 0.0

    # Build rows for every reason that has >= min_n_test in the test set.
    rows = []
    for reason, ts in test_stats.items():
        n_t = ts["n"]
        if n_t < args.min_n_test:
            continue
        rug_rate = ts["rug"] / n_t
        lo, hi   = _wilson_ci(ts["rug"], n_t)
        # Lift vs base rate; CI on lift via subtracting base from CI bounds.
        lift     = rug_rate - base_rate
        lift_lo  = lo - base_rate
        lift_hi  = hi - base_rate
        # Train-set rug rate for comparison (how stable is the pattern?)
        trs       = train_stats.get(reason, {"n": 0, "rug": 0})
        n_tr      = trs["n"]
        rug_train = trs["rug"] / n_tr if n_tr else 0.0
        # Pump rate — how often did we reject a +100% winner?
        pump_rate = ts["pump"] / n_t
        # Verdict: validated iff lift CI > 0 AND train/test rates within 10pp.
        validated = lift_lo > 0 and abs(rug_train - rug_rate) < 0.10
        cost_warn = pump_rate > 0.10  # rejecting >10% pumps means real opportunity cost
        rows.append((reason, n_tr, rug_train, n_t, rug_rate, lift, lift_lo, lift_hi,
                     pump_rate, validated, cost_warn))

    rows.sort(key=lambda r: -r[5])  # by lift desc

    # ── Write report ────────────────────────────────────────────────────────
    lines = [
        "# Held-Out Counterfactual Validation",
        "",
        f"**Dataset**: `{args.counterfactual}` — {len(records)} total rejections",
        f"**Split**: first {int(args.train_frac*100)}% train / "
        f"last {int((1-args.train_frac)*100)}% held-out test",
        f"**Train**: {len(train)} records | **Test**: {len(test)} records",
        f"**Rug threshold**: `mc_delta_pct <= -{args.rug_threshold:.0f}%`",
        f"**Pump threshold**: `mc_delta_pct >= +{args.pump_threshold:.0f}%`",
        "",
        f"**Base rug-rate on held-out set**: **{base_rate*100:.1f}%** "
        f"({total_rug}/{total_test} of ALL rejections rugged)",
        "",
        "## Filter performance — sorted by lift over base rate",
        "",
        "Only categories with >= " + str(args.min_n_test) + " held-out samples shown. "
        "A filter is **validated** iff:",
        "1. Its 95% Wilson CI on lift over base rate excludes 0 (statistical significance), AND",
        "2. Train rug-rate and test rug-rate differ by less than 10pp (stable pattern, not drift)",
        "",
        "| Reason | n(train) | rug%(train) | n(test) | rug%(test) | "
        "lift | 95% CI on lift | pump%(test) | verdict |",
        "|--------|---------:|------------:|--------:|-----------:|"
        "-----:|:--------------:|------------:|:--------|",
    ]
    for (reason, n_tr, rt_tr, n_t, rt_t, lift, lo, hi, pump, validated, cost) in rows:
        v = "✅ validated" if validated else "❌"
        if cost:
            v += " ⚠️ KILLS WINNERS"
        lines.append(
            f"| `{reason}` | {n_tr} | {rt_tr*100:.1f}% | {n_t} | {rt_t*100:.1f}% | "
            f"{lift*100:+.1f}pp | [{lo*100:+.1f}, {hi*100:+.1f}]pp | "
            f"{pump*100:.1f}% | {v} |"
        )

    lines += [
        "",
        "## How to read this",
        "",
        "- **rug%(test)**: of the rejections we made in the held-out window, "
        "what fraction went on to rug? Higher is better — the filter is catching real rugs.",
        "- **lift**: rug%(test) minus base rate. If positive, this filter is selecting "
        "for *worse-than-average* rugs (good — it's a meaningful signal).",
        "- **95% CI on lift**: if the entire interval is above 0, the lift is statistically "
        "significant at 95% confidence. If it crosses 0, the filter's edge could be noise.",
        "- **pump%(test)**: of the rejections, what fraction went on to >+100%? "
        "If > 10%, the filter is paying meaningful opportunity cost — every pump rejected "
        "was a winner we missed.",
        "- **verdict**: a filter that passes both train→test stability AND CI-excludes-zero "
        "is genuinely validated. Anything else is fragile and may not survive in the future.",
        "",
        "## Action items",
        "",
        "- **For each ❌ row**: consider relaxing or removing the filter. It's not "
        "demonstrably better than rejecting randomly.",
        "- **For each ⚠️ KILLS WINNERS row**: the filter rejects too many real pumps. "
        "Even if validated as a rug-catcher, the opportunity cost may exceed the savings.",
        "- **For each ✅ row**: keep the filter. The signal is real and stable.",
    ]

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # ── Console summary ─────────────────────────────────────────────────────
    print(f"[HOLDOUT] wrote {args.output}")
    print(f"[HOLDOUT] base test-set rug rate: {base_rate*100:.1f}%")
    validated_n = sum(1 for r in rows if r[9])
    killing_n   = sum(1 for r in rows if r[10])
    print(f"[HOLDOUT] {len(rows)} filters tested, "
          f"{validated_n} validated, {killing_n} killing winners (>10% pumps rejected)")


if __name__ == "__main__":
    main()
