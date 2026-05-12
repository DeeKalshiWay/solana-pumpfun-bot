"""
analytics/go_live_gate.py

Reads the bot's local state (logs/) and emits a pass/fail verdict on
the 5 criteria you need to hit before risking real money again.

The 5 criteria
--------------
1. EV per trade > 0 after ≥ MIN_TRADES (default 200).
2. PnL-weighted WR breakeven: avg_win × WR > |avg_loss| × (1-WR).
3. No single ticker contributes > MAX_SYMBOL_PCT (default 25%) of total PnL.
4. rug_memory has accumulated ≥ MIN_RUG_RECORDS (default 5) patterns
   — proves the learn-from-losers loop has data to act on.
5. auto_tuner.adjustment_count ≥ MIN_AUTO_TUNES (default 5) AND
   |offset| ≤ MAX_OFFSET (default 10) — proves the WR signal moved the
   threshold AND didn't oscillate wildly.

Usage
-----
    python -m analytics.go_live_gate              # default thresholds
    python -m analytics.go_live_gate --min-trades 100 --json
    python -m analytics.go_live_gate --logs-dir /path/to/other/logs

Exit codes
----------
    0 = GO (all 5 pass)
    1 = NO-GO (any fail)
    2 = INSUFFICIENT DATA (some criteria can't be evaluated yet)

Notes
-----
- Reads directly from logs/ files + the bot's modules. Does NOT poll the
  HTTP dashboard, so it works whether the bot is running or not.
- Designed to be checked into git so the gate is the same artifact the
  operator and reviewer both see.
"""

from __future__ import annotations

import argparse
import json as _json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyzer.auto_tuner import auto_tuner  # noqa: E402
from analyzer.rug_memory import rug_memory  # noqa: E402
from logger.trade_db import get_trade_db  # noqa: E402

# ── Defaults (env-overridable so a forgiving "warm-up" pass is one
#    env var away) ────────────────────────────────────────────────────────────
DEFAULT_MIN_TRADES     = int(os.environ.get("GATE_MIN_TRADES",     200))
DEFAULT_MAX_SYMBOL_PCT = float(os.environ.get("GATE_MAX_SYMBOL_PCT", 0.25))
DEFAULT_MIN_RUG_RECORDS = int(os.environ.get("GATE_MIN_RUG_RECORDS", 5))
DEFAULT_MIN_AUTO_TUNES = int(os.environ.get("GATE_MIN_AUTO_TUNES",  5))
DEFAULT_MAX_OFFSET     = int(os.environ.get("GATE_MAX_OFFSET",      10))


@dataclass
class Verdict:
    name:    str
    passed:  bool | None        # None = insufficient data
    detail:  str

    @property
    def symbol(self) -> str:
        return {True: "✓", False: "✗", None: "·"}[self.passed]


# ── Individual checks ────────────────────────────────────────────────────────

def _check_ev_per_trade(trades: list[dict], min_trades: int) -> Verdict:
    n = len(trades)
    if n == 0:
        return Verdict("1. EV per trade > 0", None,
                       f"no trades on disk yet — need ≥ {min_trades}")
    if n < min_trades:
        return Verdict("1. EV per trade > 0", None,
                       f"only {n} trades — need ≥ {min_trades} for a real read")
    total = sum(float(t.get("pnl_sol", 0) or 0) for t in trades)
    ev = total / n
    passed = ev > 0
    return Verdict("1. EV per trade > 0", passed,
                   f"{n} trades · total {total:+.4f} SOL · EV {ev:+.5f} SOL/trade")


def _check_payoff_breakeven(trades: list[dict]) -> Verdict:
    """avg_win × WR > |avg_loss| × (1-WR). Treats pnl_sol == 0 as a tie
    (excluded from both sides)."""
    if not trades:
        return Verdict("2. Payoff > breakeven", None, "no trades on disk yet")
    wins   = [float(t["pnl_sol"]) for t in trades if float(t.get("pnl_sol", 0) or 0) >  0]
    losses = [float(t["pnl_sol"]) for t in trades if float(t.get("pnl_sol", 0) or 0) <  0]
    if not wins or not losses:
        return Verdict("2. Payoff > breakeven", None,
                       f"need both wins ({len(wins)}) and losses ({len(losses)}) > 0")
    avg_win   = sum(wins)   / len(wins)
    avg_loss  = sum(losses) / len(losses)    # negative
    n         = len(wins) + len(losses)
    wr        = len(wins) / n
    expected  = avg_win * wr + avg_loss * (1 - wr)   # ev per trade with non-tie pop
    passed    = expected > 0
    return Verdict("2. Payoff > breakeven", passed,
                   f"WR {wr:.1%} · avg_win {avg_win:+.4f} · avg_loss {avg_loss:+.4f} "
                   f"→ EV {expected:+.5f} SOL/trade")


def _check_symbol_concentration(trades: list[dict], max_pct: float) -> Verdict:
    """No single symbol contributes > max_pct of total POSITIVE PnL (or of
    the absolute total). Reported against absolute total so a small
    positive sum with one big winner doesn't game the ratio."""
    if not trades:
        return Verdict("3. No symbol > 25% of PnL", None, "no trades on disk yet")
    per_sym: dict[str, float] = defaultdict(float)
    for t in trades:
        per_sym[t.get("symbol", "???")] += float(t.get("pnl_sol", 0) or 0)
    total_abs = sum(abs(v) for v in per_sym.values())
    if total_abs == 0:
        return Verdict("3. No symbol > 25% of PnL", None,
                       "no PnL movement on either side yet")
    top_sym, top_pnl = max(per_sym.items(), key=lambda kv: abs(kv[1]))
    share = abs(top_pnl) / total_abs
    passed = share <= max_pct
    return Verdict("3. No symbol > 25% of PnL", passed,
                   f"top sym {top_sym} = {share:.1%} of |total| "
                   f"({top_pnl:+.4f} of {total_abs:.4f} SOL) · cap {max_pct:.0%}")


def _check_rug_memory(min_records: int) -> Verdict:
    s = rug_memory.stats()
    total = int(s.get("total_rugs", 0))
    if total == 0:
        return Verdict("4. rug_memory accumulating", None,
                       f"0 patterns recorded — need ≥ {min_records}")
    passed = total >= min_records
    return Verdict("4. rug_memory accumulating", passed,
                   f"{total} rug patterns across {s.get('unique_sigs', 0)} signature buckets "
                   f"· need ≥ {min_records}")


def _check_auto_tuner(min_adjustments: int, max_offset: int) -> Verdict:
    s = auto_tuner.stats()
    n      = int(s.get("adjustment_count", 0))
    offset = int(s.get("offset", 0))
    if n == 0:
        return Verdict("5. auto_tuner exercised", None,
                       "0 adjustments — auto-tune hasn't fired yet")
    if n < min_adjustments:
        return Verdict("5. auto_tuner exercised", None,
                       f"only {n} adjustments — need ≥ {min_adjustments}")
    passed = abs(offset) <= max_offset
    return Verdict("5. auto_tuner exercised", passed,
                   f"{n} adjustments · offset {offset:+d} "
                   f"(cap |{max_offset}|) · effective {s.get('effective')}/100")


# ── Driver ──────────────────────────────────────────────────────────────────

def run_gate(*,
             min_trades: int     = DEFAULT_MIN_TRADES,
             max_symbol_pct: float = DEFAULT_MAX_SYMBOL_PCT,
             min_rug_records: int  = DEFAULT_MIN_RUG_RECORDS,
             min_auto_tunes: int   = DEFAULT_MIN_AUTO_TUNES,
             max_offset: int       = DEFAULT_MAX_OFFSET) -> list[Verdict]:
    trades = get_trade_db().load_all()
    return [
        _check_ev_per_trade(trades, min_trades),
        _check_payoff_breakeven(trades),
        _check_symbol_concentration(trades, max_symbol_pct),
        _check_rug_memory(min_rug_records),
        _check_auto_tuner(min_auto_tunes, max_offset),
    ]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    p.add_argument("--min-trades",      type=int,   default=DEFAULT_MIN_TRADES)
    p.add_argument("--max-symbol-pct",  type=float, default=DEFAULT_MAX_SYMBOL_PCT)
    p.add_argument("--min-rug-records", type=int,   default=DEFAULT_MIN_RUG_RECORDS)
    p.add_argument("--min-auto-tunes",  type=int,   default=DEFAULT_MIN_AUTO_TUNES)
    p.add_argument("--max-offset",      type=int,   default=DEFAULT_MAX_OFFSET)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args()

    verdicts = run_gate(
        min_trades      = args.min_trades,
        max_symbol_pct  = args.max_symbol_pct,
        min_rug_records = args.min_rug_records,
        min_auto_tunes  = args.min_auto_tunes,
        max_offset      = args.max_offset,
    )

    if args.json:
        print(_json.dumps([{"name": v.name, "passed": v.passed, "detail": v.detail}
                           for v in verdicts], indent=2))
    else:
        bar = "─" * 76
        print(bar)
        print("  GO-LIVE GATE  ·  5 criteria  ·  fail-shut by default")
        print(bar)
        for v in verdicts:
            print(f"  {v.symbol}  {v.name:<32} {v.detail}")
        print(bar)

    fails = sum(1 for v in verdicts if v.passed is False)
    nones = sum(1 for v in verdicts if v.passed is None)
    if fails > 0:
        if not args.json:
            print(f"  VERDICT: ✗  NO-GO ({fails} hard fail{'s' if fails != 1 else ''})")
        return 1
    if nones > 0:
        if not args.json:
            print(f"  VERDICT: ·  INSUFFICIENT DATA ({nones} criterion not yet evaluable)")
        return 2
    if not args.json:
        print("  VERDICT: ✓  GO — all 5 criteria pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
