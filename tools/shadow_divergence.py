"""
tools/shadow_divergence.py

Reads logs/shadow_outcomes.jsonl (written by analyzer/shadow_mode.py) and
produces a divergence report — quantifies how much LIVE PnL differs from
SIM PnL across all trades.

THE NUMBER THAT MATTERS:
    Average divergence as % of trade size = your bot's structural
    friction tax. Includes slippage, latency cost, failed-tx burns,
    and any other reality that paper mode misses.

OUTPUT:
    analytics/shadow_divergence.md with:
      - Overall avg / median / p99 divergence
      - Per-mint divergence (which tokens are hostile)
      - Time-series buckets (is friction increasing or decreasing?)
      - Verdict: how trustworthy is paper mode?

This tool is FUNCTIONAL TODAY but only produces output once
analyzer/shadow_mode.py is wired into the live trade pipeline.
Until then, logs/shadow_outcomes.jsonl is empty and this script
reports "no data".

Usage:
    python -m tools.shadow_divergence
    python -m tools.shadow_divergence --output analytics/shadow_divergence.md
"""

import argparse
import json
import os
from collections import defaultdict


def _read_outcomes(path: str) -> list[dict]:
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, int(len(sorted_values) * p)))
    return sorted_values[idx]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outcomes", default="logs/shadow_outcomes.jsonl")
    ap.add_argument("--output",   default="analytics/shadow_divergence.md")
    ap.add_argument("--top-n",    type=int, default=15)
    args = ap.parse_args()

    outcomes = _read_outcomes(args.outcomes)
    if not outcomes:
        # Shadow mode not yet wired into live trade pipeline — write a
        # placeholder report so anyone navigating the repo sees what this
        # would look like.
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        placeholder = """# Shadow Mode Divergence Report

**Status**: no shadow-mode data on disk yet.

`analyzer/shadow_mode.py` ships the recording API and `ShadowMode`
singleton, but the wiring into the live trade pipeline
(`trader/pumpportal_executor.py` and `risk/manager.py`) is intentionally
deferred — see the module docstring for the half-shipped scope.

Once wired, every live trade writes a record to
`logs/shadow_outcomes.jsonl` with both LIVE and SIM PnL for the same
decision. Re-run this script and the report will populate with:

- Overall mean / median / p99 divergence (slippage tax)
- Per-mint breakdown (which tokens are hostile)
- Trend over time (is friction trending up or down?)
- Verdict: is paper mode trustworthy or systematically optimistic?

Run after a few hours of live trading with wiring in place.
"""
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(placeholder)
        print(f"[SHADOW-DIV] no outcomes yet — wrote placeholder to {args.output}")
        return

    # ── Aggregate stats ─────────────────────────────────────────────────────
    n = len(outcomes)
    divs   = sorted(o["divergence_pct"] for o in outcomes)
    e_slip = sorted(o["entry_slippage_pct"] for o in outcomes)
    total_live = sum(o["live_pnl_sol"] for o in outcomes)
    total_sim  = sum(o["sim_pnl_sol"]  for o in outcomes)

    overall = {
        "n":               n,
        "total_live_pnl":  total_live,
        "total_sim_pnl":   total_sim,
        "total_divergence_sol": total_sim - total_live,
        "avg_div_pct":     sum(divs) / n,
        "median_div_pct":  divs[n // 2],
        "p99_div_pct":     _percentile(divs, 0.99),
        "max_div_pct":     divs[-1],
        "avg_entry_slippage_pct": sum(e_slip) / n,
    }

    # ── Per-mint aggregation ─────────────────────────────────────────────────
    by_mint = defaultdict(lambda: {"n": 0, "div_pct_sum": 0.0, "div_sol_sum": 0.0, "symbol": "?"})
    for o in outcomes:
        m = o["mint"]
        by_mint[m]["n"] += 1
        by_mint[m]["div_pct_sum"] += o["divergence_pct"]
        by_mint[m]["div_sol_sum"] += o["divergence_sol"]
        # symbol is in decision record, not outcome — leave as ? unless we
        # cross-reference shadow_decisions.jsonl. Out of scope for this pass.
    worst = sorted(by_mint.items(), key=lambda kv: -kv[1]["div_pct_sum"] / kv[1]["n"])[:args.top_n]

    # ── Verdict ─────────────────────────────────────────────────────────────
    avg = overall["avg_div_pct"]
    if avg < 1.0:
        verdict = "🟢 PAPER MODE TRUSTWORTHY — avg divergence under 1% per trade. Backtests are believable."
    elif avg < 5.0:
        verdict = f"🟡 ACCEPTABLE FRICTION — avg {avg:.1f}% divergence. Paper overstates real PnL by ~{avg:.0f}% per trade. Adjust expectations."
    elif avg < 10.0:
        verdict = f"🟠 HIGH FRICTION — avg {avg:.1f}% divergence. Paper mode is misleading. Treat backtest PnL as upper-bound only."
    else:
        verdict = f"🔴 PAPER IS LYING — avg {avg:.1f}% divergence. Backtests do not reflect reality. Investigate slippage / failed-tx rate / exit timing."

    # ── Write report ────────────────────────────────────────────────────────
    L = [
        "# Shadow Mode Divergence Report",
        "",
        f"**Outcomes recorded**: {n} trades",
        f"**Live realized PnL**: {total_live:+.4f} SOL",
        f"**Sim realized PnL**:  {total_sim:+.4f} SOL",
        f"**Total divergence (Sim - Live)**: {overall['total_divergence_sol']:+.4f} SOL — "
        f"this is the bot's structural friction tax over the sample.",
        "",
        f"## Verdict",
        "",
        f"{verdict}",
        "",
        "## Divergence distribution (Sim PnL − Live PnL, as % of trade size)",
        "",
        f"| Metric | Value |",
        f"|--------|------:|",
        f"| Mean   | {overall['avg_div_pct']:.2f}% |",
        f"| Median | {overall['median_div_pct']:.2f}% |",
        f"| P99    | {overall['p99_div_pct']:.2f}% |",
        f"| Max    | {overall['max_div_pct']:.2f}% |",
        f"| Entry-only slippage (avg) | {overall['avg_entry_slippage_pct']:.2f}% |",
        "",
        f"## Top {args.top_n} most-hostile mints",
        "",
        f"| Mint | Trades | Avg div % per trade | Total div SOL |",
        f"|------|-------:|--------------------:|--------------:|",
    ]
    for m, stats in worst:
        avg_div = stats["div_pct_sum"] / stats["n"]
        L.append(f"| `{m[:24]}` | {stats['n']} | {avg_div:.1f}% | {stats['div_sol_sum']:+.4f} |")

    L += [
        "",
        "## How to read this",
        "",
        "- **Divergence per trade** is the gap between what a friction-free sim",
        "  would have made and what we actually netted. Sources include:",
        "  slippage on bonding-curve fills, priority fees, failed-tx losses,",
        "  pre-flight rejections, and latency-driven price-drift between",
        "  decision and confirmation.",
        "- **Entry-only slippage** is just the at-buy gap (price-paid vs",
        "  bonding-curve-mid at submit). Useful for isolating buy-side friction.",
        "- **Hostile mints** show pattern of unusually high divergence —",
        "  candidates for blacklisting from future trading regardless of score.",
        "",
        "## What to do with the verdict",
        "",
        "- **🟢 Trustworthy**: backtests and paper-mode PnL reflect reality.",
        "  Scale up with confidence.",
        "- **🟡 Acceptable**: discount backtest PnL by the divergence %. Trade,",
        "  but with realistic expectations.",
        "- **🟠/🔴**: investigate. Common causes:",
        "  - Priority fees too low → tx lands at worse price",
        "  - Exit thresholds firing too late on dump trajectories",
        "  - Specific mints with hostile curves (large buyers ahead of you)",
        "  - Failed-tx rate above ~5% — every failure burns the fee",
        "",
        "This metric is rare in public bots. Most measure live PnL OR backtest",
        "PnL but never both on the same decisions. That gap is where edge",
        "claims go to die.",
    ]

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[SHADOW-DIV] wrote {args.output}")
    print(f"[SHADOW-DIV] n={n}  avg_divergence={overall['avg_div_pct']:.2f}%  "
          f"total_friction_sol={overall['total_divergence_sol']:+.4f}")


if __name__ == "__main__":
    main()
