"""
tools/audit_friction.py

Operational guard for the three pillars (latency, friction, real slippage)
from `memory/rule_paper_honesty_three_pillars.md`. Run before trusting any
paper PnL number.

Checks the trade log and reports PASS / FAIL for each pillar:

  LATENCY    — every open event has detection_latency_ms (real measurement).
               Reports p50 / p90 / max latency distribution.
  FRICTION   — friction parameters match the calibrated model (ENTRY_LAG,
               EXIT_LAG, FEE_PCT). Spot-checks a sample of closes to verify
               (our_exit/our_entry - 1) * 100 - FEE_PCT ≈ net_pct.
  SLIPPAGE   — every open event in paper mode is tagged
               slip_source="modeled". No open is `clamped` or marked
               slip_suspect (those indicate corrupt price reads).

If anything FAILs, the numbers in the dashboard are not trustworthy.

Run: python -m tools.audit_friction
"""

from __future__ import annotations

import json
import os
import statistics as st
import sys

from tools.copy_follower import ENTRY_LAG_PCT, EXIT_LAG_PCT, FEE_PCT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES = os.path.join(ROOT, "logs", "copy_follower_trades.jsonl")


def _load():
    opens, closes = [], []
    if not os.path.exists(TRADES):
        return opens, closes
    for line in open(TRADES, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("source") == "replay":
            continue  # replays don't get audited here
        e = r.get("event")
        if e == "open":
            opens.append(r)
        elif e == "close":
            closes.append(r)
    return opens, closes


def _ok(passed: bool) -> str:
    return "\033[32mPASS\033[0m" if passed else "\033[31mFAIL\033[0m"


def main():
    opens, closes = _load()
    print(f"Auditing {len(opens)} live opens / {len(closes)} live closes "
          f"(model: ENTRY_LAG={ENTRY_LAG_PCT}%, EXIT_LAG={EXIT_LAG_PCT}%, FEE={FEE_PCT}%)")
    print("=" * 80)
    overall_pass = True

    # ── LATENCY ───────────────────────────────────────────────────────────
    print("\n[1/3] LATENCY")
    if not opens:
        print("  (no opens yet — N/A)")
    else:
        lats = [o.get("detection_latency_ms") for o in opens if o.get("detection_latency_ms") is not None]
        missing = len(opens) - len(lats)
        if missing > 0:
            print(f"  {_ok(False)}  {missing}/{len(opens)} opens missing detection_latency_ms")
            overall_pass = False
        else:
            print(f"  {_ok(True)}  all {len(opens)} opens recorded latency")
        if lats:
            lats_sorted = sorted(lats)
            p50 = lats_sorted[len(lats)//2]
            p90 = lats_sorted[int(len(lats)*0.9)]
            print(f"        distribution: p50={p50}ms  p90={p90}ms  max={max(lats)}ms")
            if p50 > 5000:
                print(f"  {_ok(False)}  median latency >5s — streaming may be unhealthy")
                overall_pass = False

    # ── FRICTION ──────────────────────────────────────────────────────────
    print("\n[2/3] FRICTION")
    all_in = ENTRY_LAG_PCT + EXIT_LAG_PCT + FEE_PCT
    print(f"  configured all-in friction ~= {all_in:.1f}%  "
          f"(calibration target from friction_analysis.py: ~10% at 0.10-0.25 SOL)")
    if all_in < 7 or all_in > 15:
        print(f"  {_ok(False)}  all-in friction outside calibration band [7%, 15%]")
        overall_pass = False
    else:
        print(f"  {_ok(True)}  all-in friction within calibration band")
    # Spot-check: for each close that's not stop-loss, verify the math.
    mismatch = 0
    checked = 0
    for c in closes:
        if c.get("exit_reason") != "wallet_sell":
            continue
        oe = c.get("our_entry") or 0
        ox = c.get("our_exit") or 0
        if not oe or not ox:
            continue
        expected_gross = (ox / oe - 1) * 100
        expected_net = expected_gross - FEE_PCT
        if abs(expected_net - c.get("net_pct", 0)) > 0.5:
            mismatch += 1
        checked += 1
    if checked:
        ok = mismatch == 0
        print(f"  {_ok(ok)}  spot-check on {checked} wallet-sell closes: {mismatch} mismatched "
              f"(net_pct vs (exit/entry-1)*100 - FEE_PCT)")
        if not ok:
            overall_pass = False

    # ── SLIPPAGE ──────────────────────────────────────────────────────────
    print("\n[3/3] SLIPPAGE")
    if not opens:
        print("  (no opens yet — N/A)")
    else:
        modeled = sum(1 for o in opens if o.get("slip_source") == "modeled")
        un_marked = sum(1 for o in opens if o.get("slip_source") is None)
        suspect = sum(1 for o in opens if o.get("slip_suspect"))
        ok = un_marked == 0
        print(f"  {_ok(ok)}  {modeled}/{len(opens)} opens tagged slip_source='modeled', "
              f"{un_marked} un-tagged (legacy)")
        if not ok:
            print(f"        legacy un-tagged opens are pre-pillar-rule; new opens must be tagged")
            overall_pass = False
        if suspect:
            print(f"  {_ok(False)}  {suspect} open(s) flagged slip_suspect — corrupt price read")
            overall_pass = False
        # Check actual gross_pct value (the old `clamped` flag had a buggy
        # float-precision comparison that marked every close as clamped even
        # when no clamp engaged; we ignore that legacy flag).
        actually_clamped = [c for c in closes
                            if c.get("gross_pct") is not None
                            and (c["gross_pct"] <= -100.0 or c["gross_pct"] >= 1000.0)]
        if actually_clamped:
            print(f"  {_ok(False)}  {len(actually_clamped)} close(s) at gross-PnL boundary "
                  f"(actual gross ≤ -100% or ≥ +1000%) — price feed corrupt")
            overall_pass = False
        else:
            print(f"  {_ok(True)}  no closes at gross-PnL clamp boundary (legacy `clamped` flag was buggy, ignored)")

    print("\n" + "=" * 80)
    print(f"OVERALL: {_ok(overall_pass)}")
    if not overall_pass:
        print("\n  Per memory/rule_paper_honesty_three_pillars.md:")
        print("  If any pillar fails, the paper PnL numbers are not trustworthy.")
        sys.exit(1)


if __name__ == "__main__":
    main()
