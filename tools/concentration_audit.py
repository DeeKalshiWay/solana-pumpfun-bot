"""
tools/concentration_audit.py

Survivorship-bias check: is our PnL real edge, or is one moonshot doing all
the lifting?

For every closed trade, attribute its PnL to the token symbol. Then ask:
    1. What % of total PnL came from each symbol?
    2. If we cap any single symbol's contribution at 10% of total PnL,
       does the cumulative curve still climb, or does it flatline?

A strategy with real edge produces gains across MANY trades. A strategy
that 100x'd in backtest because one token went +5,000% has no edge —
it has a sample-size-of-one.

This script computes:
    - Top-N symbols by gross PnL
    - Gini coefficient of PnL distribution
    - Capped-equity curve vs uncapped
    - Verdict: "edge" / "lucky" / "indeterminate"

Usage:
    python -m tools.concentration_audit
    python -m tools.concentration_audit --cap-pct 5.0  # tighter cap
    python -m tools.concentration_audit --output logs/concentration.md
"""

import argparse
import json
import os
from collections import defaultdict


def _read_trades(path: str) -> list[dict]:
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


def _gini(values: list[float]) -> float:
    """Gini coefficient. 0 = perfectly equal, 1 = one item has everything.
    Computed on absolute values (so losses contribute to inequality too)."""
    if not values:
        return 0.0
    sorted_abs = sorted(abs(v) for v in values)
    n = len(sorted_abs)
    total = sum(sorted_abs)
    if total == 0:
        return 0.0
    cum = 0.0
    for i, v in enumerate(sorted_abs):
        cum += (2 * (i + 1) - n - 1) * v
    return cum / (n * total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="logs/closed_trades.jsonl")
    ap.add_argument("--output", default="analytics/concentration_audit.md")
    ap.add_argument("--cap-pct", type=float, default=10.0,
                    help="cap each ticker's PnL contribution at this %% of total")
    ap.add_argument("--top-n", type=int, default=15)
    args = ap.parse_args()

    trades = _read_trades(args.trades)
    if not trades:
        print(f"[CONC] no trades in {args.trades}")
        return

    # Group PnL by symbol
    by_sym: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "n": 0, "wins": 0})
    for t in trades:
        sym = t.get("symbol", "?")
        pnl = t.get("pnl_sol", 0)
        by_sym[sym]["pnl"] += pnl
        by_sym[sym]["n"]   += 1
        if pnl > 0:
            by_sym[sym]["wins"] += 1

    total_pnl       = sum(t.get("pnl_sol", 0) for t in trades)
    n_trades        = len(trades)
    n_symbols       = len(by_sym)
    pnls_per_symbol = [s["pnl"] for s in by_sym.values()]
    gini            = _gini(pnls_per_symbol)

    # Cap analysis: any single symbol's contribution capped at cap_pct% of |total|
    cap_abs = abs(total_pnl) * (args.cap_pct / 100.0) if total_pnl else 0.0
    # When total is 0 or negative, cap on absolute scale instead — use mean pnl × 5
    if cap_abs <= 0:
        cap_abs = sum(abs(s["pnl"]) for s in by_sym.values()) * (args.cap_pct / 100.0)

    capped_total = 0.0
    capped_per_sym = {}
    for sym, s in by_sym.items():
        capped = max(-cap_abs, min(cap_abs, s["pnl"]))
        capped_per_sym[sym] = capped
        capped_total += capped

    # Top contributors
    top = sorted(by_sym.items(), key=lambda kv: -kv[1]["pnl"])[:args.top_n]
    bot = sorted(by_sym.items(), key=lambda kv: kv[1]["pnl"])[:args.top_n]

    # Concentration metrics
    top1_share  = (top[0][1]["pnl"]    / total_pnl * 100) if total_pnl else 0
    top3_share  = (sum(t[1]["pnl"] for t in top[:3])  / total_pnl * 100) if total_pnl else 0
    top10_share = (sum(t[1]["pnl"] for t in top[:10]) / total_pnl * 100) if total_pnl else 0

    # ── Verdict ─────────────────────────────────────────────────────────────
    if total_pnl <= 0:
        verdict = "🔴 NEGATIVE EDGE — total PnL is non-positive. Concentration math is moot; the strategy is losing on average. Increase trade size (friction-floor escape) or tighten filters before judging concentration."
    elif top1_share > 50:
        verdict = "🔴 LUCKY — one ticker contributed >50% of total PnL. Without that single trade you'd be near zero or negative. This is not edge, this is sample-size-of-one."
    elif top3_share > 70:
        verdict = "🟡 INDETERMINATE — top 3 tickers contributed >70% of PnL. Possible edge, but the curve is fragile. Either: (a) the bot has real talent for spotting moonshots, or (b) we're 2 unlucky moonshots away from break-even."
    elif capped_total > 0 and capped_total / abs(total_pnl) > 0.5:
        verdict = (f"🟢 EDGE — capped PnL retains {capped_total/abs(total_pnl)*100:.0f}% "
                   "of total. Gains are spread across enough tickers that no single "
                   "one drives the curve.")
    else:
        verdict = "🟡 MARGINAL — gains exist but concentration is high. Worth running more trades before declaring edge."

    # ── Write report ────────────────────────────────────────────────────────
    lines = [
        "# Concentration Audit — is the PnL real edge or one lucky moonshot?",
        "",
        f"**Dataset**: `{args.trades}` — {n_trades} closed trades, {n_symbols} unique symbols",
        f"**Total PnL**: {total_pnl:+.4f} SOL",
        f"**Per-ticker cap**: {args.cap_pct}% of |total PnL| = ±{cap_abs:.4f} SOL",
        "",
        f"## Verdict",
        "",
        f"{verdict}",
        "",
        f"## Concentration metrics",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Gini coefficient (PnL distribution) | **{gini:.3f}** ({'highly concentrated' if gini > 0.7 else 'moderately concentrated' if gini > 0.5 else 'distributed'}) |",
        f"| Top 1 symbol's share of total PnL | **{top1_share:.1f}%** |",
        f"| Top 3 symbols' share | **{top3_share:.1f}%** |",
        f"| Top 10 symbols' share | **{top10_share:.1f}%** |",
        f"| Total PnL uncapped | **{total_pnl:+.4f} SOL** |",
        f"| Total PnL capped at ±{cap_abs:.4f} per ticker | **{capped_total:+.4f} SOL** |",
        f"| PnL retention after cap | **{capped_total/total_pnl*100 if total_pnl else 0:.1f}%** |",
        "",
        f"## Top {args.top_n} contributors",
        "",
        f"| # | Symbol | trades | wins | gross PnL | % of total | capped PnL |",
        f"|---|--------|-------:|-----:|----------:|-----------:|-----------:|",
    ]
    for i, (sym, s) in enumerate(top, 1):
        share = (s["pnl"] / total_pnl * 100) if total_pnl else 0
        lines.append(
            f"| {i} | `{sym[:20]}` | {s['n']} | {s['wins']} | "
            f"{s['pnl']:+.4f} | {share:+.1f}% | {capped_per_sym[sym]:+.4f} |"
        )

    lines += [
        "",
        f"## Bottom {args.top_n} contributors (biggest losers)",
        "",
        f"| # | Symbol | trades | gross PnL | % of total |",
        f"|---|--------|-------:|----------:|-----------:|",
    ]
    for i, (sym, s) in enumerate(bot, 1):
        share = (s["pnl"] / total_pnl * 100) if total_pnl else 0
        lines.append(f"| {i} | `{sym[:20]}` | {s['n']} | {s['pnl']:+.4f} | {share:+.1f}% |")

    lines += [
        "",
        "## How to read this",
        "",
        "- **Gini > 0.7**: highly concentrated. A few tickers dominate. Edge claim is fragile.",
        "- **Top 1 > 50% of PnL**: one moonshot is doing all the work. **Not edge — luck.**",
        "- **Capped retention < 30%**: most of the PnL comes from a few outliers. Cap them and the curve flatlines.",
        "- **Capped retention > 70%**: gains are distributed. The strategy has structural edge.",
        "",
        "## Why this matters",
        "",
        "Survivorship bias is the #1 way trading bot operators fool themselves. A backtest "
        "that 100×'d may have done so because one token in the sample went 5,000×, lifting "
        "everything else into the noise. Capping each ticker's contribution at a small "
        "percentage of the total kills that effect and shows what the strategy looked like "
        "*on the median trade* — which is what tomorrow's trade will be.",
        "",
        "If you can't survive a cap, you don't have edge. You have a winning lottery ticket.",
    ]

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[CONC] wrote {args.output}")
    print(f"[CONC] total PnL: {total_pnl:+.4f} SOL across {n_trades} trades / {n_symbols} symbols")
    print(f"[CONC] gini={gini:.3f}, top1={top1_share:.1f}%, top3={top3_share:.1f}%")
    print(f"[CONC] capped total ({args.cap_pct}% per ticker): {capped_total:+.4f} SOL")


if __name__ == "__main__":
    main()
