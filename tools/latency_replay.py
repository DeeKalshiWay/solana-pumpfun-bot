"""
tools/latency_replay.py

Reports the impact of the latency-honest exit model on paper PnL.

Reads logs/closed_trades.jsonl and aggregates trades closed AFTER the
latency patch (those carry `sol_received_optimistic` and `exit_latency_s`
fields). For each stall-class exit it compares:
  - realized   sol_received (with latency lookback + stampede multiplier)
  - optimistic sol_received (latest tick, no stampede — the pre-patch model)
…and reports the gap.

Acceptance signal: stall-class realized PnL should land 30-70% below the
optimistic counterfactual. If the gap is ~0 the patch isn't binding — most
likely because price_history isn't populating with raw prices (regression
in risk/manager.update_price). If the gap is >90%, the stampede multiplier
is set too aggressively.

Run after ~48h of paper trading post-patch.

Usage:
    python -m tools.latency_replay
    python -m tools.latency_replay --since 2026-05-10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

CLOSED_TRADES = "logs/closed_trades.jsonl"
STAMPEDE_REASONS = {"momentum_stall", "no_movement", "time_exit"}


def _parse_since(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()
    except ValueError:
        print(f"[ERROR] --since must be ISO date (YYYY-MM-DD), got {s!r}", file=sys.stderr)
        sys.exit(2)


def load_records(path: str, since_ts: float) -> list[dict]:
    if not os.path.exists(path):
        print(f"[ERROR] {path} not found — run the bot in paper mode first.", file=sys.stderr)
        sys.exit(1)
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("exit_time", 0) < since_ts:
                continue
            rows.append(r)
    return rows


def summarize(rows: list[dict]) -> None:
    stall_rows = [
        r for r in rows
        if r.get("reason") in STAMPEDE_REASONS
        and r.get("sol_received_optimistic") is not None
    ]
    other_rows = [r for r in rows if r.get("reason") not in STAMPEDE_REASONS]

    if not stall_rows:
        print("No latency-patched stall-class trades found yet.")
        print(f"(Total rows: {len(rows)}, stall reasons present: "
              f"{sum(1 for r in rows if r.get('reason') in STAMPEDE_REASONS)}, "
              f"none carry sol_received_optimistic — bot hasn't traded since the patch.)")
        return

    realized_total   = sum(r["sol_received"] - r["sol_invested"] for r in stall_rows)
    optimistic_total = sum(r["sol_received_optimistic"] - r["sol_invested"] for r in stall_rows)
    delta = realized_total - optimistic_total
    gap_pct = 100.0 * delta / optimistic_total if optimistic_total else 0.0

    print("=" * 64)
    print(" LATENCY-HONEST EXIT REPLAY")
    print("=" * 64)
    print(f" Stall-class trades (post-patch): {len(stall_rows)}")
    print(f" Other trades (TP, trail, etc.):  {len(other_rows)}")
    print()
    print(" Stall-class PnL aggregate:")
    print(f"   pre-patch (optimistic):  {optimistic_total:+.4f} SOL")
    print(f"   post-patch (realized):   {realized_total:+.4f} SOL")
    print(f"   delta:                   {delta:+.4f} SOL ({gap_pct:+.1f}%)")
    print()

    # Latency distribution
    latencies = sorted(r["exit_latency_s"] for r in stall_rows if r.get("exit_latency_s"))
    if latencies:
        def pct(p: float) -> float:
            i = max(0, min(len(latencies) - 1, int(round(p * (len(latencies) - 1)))))
            return latencies[i]
        print(" Latency distribution (s):")
        print(f"   p50  {pct(0.50):.2f}   p90 {pct(0.90):.2f}   "
              f"p99 {pct(0.99):.2f}   max {latencies[-1]:.2f}")
        print()

    # Per-reason breakdown
    by_reason: dict[str, list[dict]] = {}
    for r in stall_rows:
        by_reason.setdefault(r["reason"], []).append(r)
    print(" Per-reason breakdown:")
    print(f"   {'reason':<18} {'n':>5} {'opt':>10} {'realized':>10} {'gap':>10}")
    for reason, rs in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        opt = sum(r["sol_received_optimistic"] - r["sol_invested"] for r in rs)
        real = sum(r["sol_received"] - r["sol_invested"] for r in rs)
        print(f"   {reason:<18} {len(rs):>5} {opt:>+10.4f} {real:>+10.4f} {(real-opt):>+10.4f}")
    print()

    # Verdict
    if gap_pct > -5:
        verdict = "🔴 patch is not binding — gap < 5%. Check that price_history populates with raw prices."
    elif gap_pct > -25:
        verdict = "🟡 mild gap (<25%). Latency model is on but stampede may be undersized for this trade mix."
    elif gap_pct > -75:
        verdict = "🟢 expected range (-25% to -75%). Paper PnL is now closer to live-realistic."
    else:
        verdict = "🟠 very large gap (>75%). Verify STAMPEDE_MULT_STALL is not over-aggressive."
    print(f" Verdict: {verdict}")
    print("=" * 64)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="ISO date (YYYY-MM-DD); only count trades closed on/after this date")
    ap.add_argument("--path", default=CLOSED_TRADES, help=f"closed-trades log (default {CLOSED_TRADES})")
    args = ap.parse_args()
    rows = load_records(args.path, _parse_since(args.since))
    summarize(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
