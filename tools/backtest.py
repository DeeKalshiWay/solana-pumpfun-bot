"""
tools/backtest.py

Strategy A/B backtester. Replays every historical mint we've ever seen
through user-defined strategy configs and reports comparative PnL.

Data sources (already on disk from live operation):
    logs/counterfactual.jsonl — every REJECTED signal + its 10-min outcome
    logs/closed_trades.jsonl  — every ACCEPTED trade + its actual PnL

For each historical mint we know:
    - Features at score-time: init_buy_sol, bonding_curve_pct, score, creator
    - Outcome: rugged / pumped / flat (counterfactual gives mc_delta_pct,
      closed_trades gives pnl_sol)

The backtester applies a candidate strategy config to each mint and asks:
    "Under THIS config, would we have bought? If so, how much SOL? Given
     the observed outcome, what's the simulated PnL?"

Configs are simple dicts. Two are provided as examples (Baseline + Tighter);
you can define more in the CONFIGS dict at the bottom.

Honest limitations:
    1. We can't simulate execution latency or slippage at different trade
       sizes. The simulated PnL uses the actual outcome ratio.
    2. Concurrent-position constraints are NOT enforced (every qualifying
       signal "buys"). This is a generous backtest — real exposure caps
       would reduce trade count and PnL.
    3. Counterfactual outcomes are 10-min snapshots, not full trade
       lifecycles. They reflect "what would have happened to the price" not
       "what would we have netted with our TP ladder."
    4. For accepted trades we use actual pnl_sol (already includes our
       TP ladder behavior at the time). For rejected mints we estimate
       PnL = sol_invested × (mc_delta_pct / 100), which is a rough
       proxy assuming we'd exit at the 10-min snapshot.

Usage:
    python -m tools.backtest                          # all configs vs baseline
    python -m tools.backtest --output analytics/backtest.md
    python -m tools.backtest --configs Baseline,Tighter
"""

import argparse
import json
import os
from collections import defaultdict
from dataclasses import dataclass


# ── Signal record (unified shape from both data sources) ─────────────────────
@dataclass
class Signal:
    ts:           float
    mint:         str
    symbol:       str
    creator:      str
    score:        int             # raw score at score-time
    init_buy_sol: float
    curve_pct:    float
    # Outcome:
    pnl_pct:      float            # what happened (from MC for rejected, from realized for accepted)
    realized_sol: float | None     # actual SOL PnL if this was an accepted trade
    source:       str              # "rejected" or "accepted"


def _load_signals(cf_path: str, trades_path: str) -> list[Signal]:
    """Merge counterfactual + closed_trades into one chronological signal stream."""
    sigs: list[Signal] = []

    if os.path.exists(cf_path):
        with open(cf_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                sigs.append(Signal(
                    ts           = r.get("reject_ts", 0),
                    mint         = r.get("mint", ""),
                    symbol       = r.get("symbol", "?"),
                    creator      = r.get("creator", ""),
                    score        = int(r.get("score", 0)),
                    init_buy_sol = float(r.get("initial_buy_sol", 0) or 0),
                    curve_pct    = float(r.get("curve_pct", 0) or 0),
                    pnl_pct      = float(r.get("mc_delta_pct", 0) or 0),
                    realized_sol = None,
                    source       = "rejected",
                ))

    if os.path.exists(trades_path):
        with open(trades_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                sigs.append(Signal(
                    ts           = r.get("entry_time", 0),
                    mint         = r.get("mint", ""),
                    symbol       = r.get("symbol", "?"),
                    creator      = r.get("creator", ""),
                    score        = int(r.get("score", 0)),
                    init_buy_sol = 0.0,   # not preserved in trade records
                    curve_pct    = 0.0,   # not preserved either
                    pnl_pct      = float(r.get("pnl_pct", 0) or 0),
                    realized_sol = float(r.get("pnl_sol", 0) or 0),
                    source       = "accepted",
                ))

    sigs.sort(key=lambda s: s.ts)
    return sigs


# ── Strategy configs ────────────────────────────────────────────────────────
# Each config is a dict with thresholds the backtester applies.
# Add your own and pass them in --configs.

CONFIGS: dict[str, dict] = {
    "Baseline": {
        # What we ran historically (loose enough to actually trade)
        "min_score":          32,
        "max_init_buy_sol":   1.5,
        "max_curve_pct":      80,
        "trade_size_sol":     0.025,    # what we were actually deploying
    },
    "PostAudit": {
        # The new config recommended in PRE_LIVE_400_PLAYBOOK §1.2
        "min_score":          32,
        "max_init_buy_sol":   1.5,
        "max_curve_pct":      80,
        "trade_size_sol":     0.10,     # 4x bigger to clear friction floor
    },
    "Tighter": {
        # If we trust only the held-out-validated filters: tighter min_score
        # + dropping the init_buy 1.5 ceiling (validated only at >=4 SOL)
        "min_score":          35,
        "max_init_buy_sol":   4.0,
        "max_curve_pct":      60,
        "trade_size_sol":     0.10,
    },
    "ScoreOnly": {
        # Pure score-based, no init-buy or curve filters
        "min_score":          35,
        "max_init_buy_sol":   999,
        "max_curve_pct":      100,
        "trade_size_sol":     0.10,
    },
}


def _qualifies(sig: Signal, cfg: dict) -> bool:
    """Apply config filters to a signal."""
    if sig.score < cfg["min_score"]:
        return False
    if sig.init_buy_sol > cfg["max_init_buy_sol"]:
        return False
    if sig.curve_pct > cfg["max_curve_pct"]:
        return False
    return True


def _simulate_pnl(sig: Signal, cfg: dict, friction_sol: float = 0.005) -> float:
    """Estimate PnL for this signal under the config.

    For accepted trades we have actual realized SOL — scale linearly to the
    config's trade size (rough but consistent).

    For rejected mints we use mc_delta_pct as a proxy outcome:
        simulated_pnl = trade_size × (delta/100) - friction
    This assumes we'd ride from entry to the 10-min snapshot, which is
    optimistic on pumps (might exit earlier on stops) and pessimistic on
    rugs (real stops cut losses below the full -50%+ drop).
    """
    size = cfg["trade_size_sol"]
    if sig.source == "accepted" and sig.realized_sol is not None:
        # Scale historical realized PnL to this config's trade size.
        # Original trade size approximated as 0.025 SOL (our pre-audit average).
        original_size = 0.025
        scale = size / original_size if original_size > 0 else 1.0
        return sig.realized_sol * scale - friction_sol
    # Rejected mints: estimate from MC delta
    raw_pct = sig.pnl_pct / 100.0
    # Cap upside at +200% (most stops/TPs would exit before that anyway)
    # Cap downside at -50% (early-rug stops fire well before -100%)
    eff_pct = max(-0.50, min(2.0, raw_pct))
    return size * eff_pct - friction_sol


def _run_config(sigs: list[Signal], cfg: dict) -> dict:
    """Run one config across all signals, return stats."""
    selected = [s for s in sigs if _qualifies(s, cfg)]
    pnls     = [_simulate_pnl(s, cfg) for s in selected]
    n        = len(pnls)
    wins     = sum(1 for p in pnls if p > 0)
    total    = sum(pnls)
    avg      = total / n if n else 0
    avg_w    = (sum(p for p in pnls if p > 0) / wins) if wins else 0
    losses   = n - wins
    avg_l    = (sum(p for p in pnls if p <= 0) / losses) if losses else 0
    # Max-drawdown on the cumulative-PnL curve
    cum      = 0.0
    peak     = 0.0
    max_dd   = 0.0
    for p in pnls:
        cum  += p
        peak  = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    return {
        "n_signals_total":  len(sigs),
        "n_traded":         n,
        "trade_rate":       n / len(sigs) if sigs else 0,
        "wins":             wins,
        "losses":           losses,
        "win_rate":         wins / n if n else 0,
        "total_pnl_sol":    total,
        "avg_pnl_per_trade":avg,
        "avg_win":          avg_w,
        "avg_loss":         avg_l,
        "max_drawdown":     max_dd,
    }


# ── Reporting ──────────────────────────────────────────────────────────────
def _render_report(results: dict[str, dict], baseline: str = "Baseline") -> str:
    L = ["# Backtester — A/B strategy comparison", ""]
    L += [f"Baseline: **{baseline}**. Other configs scored against it.", ""]

    base = results.get(baseline)

    L += ["## Side-by-side", ""]
    L += ["| Config | Trades | WR | Total PnL | Avg/trade | Avg Win | Avg Loss | Max DD | vs Baseline |"]
    L += ["|--------|-------:|---:|----------:|----------:|--------:|---------:|-------:|------------:|"]
    for name, r in results.items():
        delta = ""
        if base is not None and name != baseline:
            d = r["total_pnl_sol"] - base["total_pnl_sol"]
            delta = f"{d:+.4f} SOL"
        L.append(
            f"| {name} | {r['n_traded']} | {r['win_rate']*100:.1f}% | "
            f"{r['total_pnl_sol']:+.4f} | {r['avg_pnl_per_trade']:+.5f} | "
            f"{r['avg_win']:+.5f} | {r['avg_loss']:+.5f} | "
            f"{r['max_drawdown']:.4f} | {delta} |"
        )

    L += ["", "## Configs tested", ""]
    for name in results:
        cfg = CONFIGS[name]
        L.append(f"### {name}")
        L += ["```",
              f"min_score        = {cfg['min_score']}",
              f"max_init_buy_sol = {cfg['max_init_buy_sol']}",
              f"max_curve_pct    = {cfg['max_curve_pct']}",
              f"trade_size_sol   = {cfg['trade_size_sol']}",
              "```", ""]

    L += [
        "## How to read this",
        "",
        "- **Trades**: number of historical signals the config would have entered.",
        "  More trades is not better — fewer high-quality entries can win.",
        "- **WR**: win rate. 67% historical is great BUT see the concentration",
        "  audit — most of it was driven by a few moonshots.",
        "- **Total PnL**: cumulative simulated SOL profit. **Compare RELATIVE",
        "  to baseline, not absolute** — this is a generous backtest (no",
        "  position-count caps, no slippage scaling, no latency).",
        "- **Max DD**: deepest underwater the cumulative curve went. A config",
        "  that ends positive but had a −5 SOL drawdown along the way may be",
        "  psychologically unrunnable.",
        "",
        "## Honest limitations",
        "",
        "1. No concurrent-position constraint — every qualifying signal trades.",
        "   Real-world caps (MAX_OPEN_POSITIONS, MAX_TOTAL_EXPOSURE_SOL) would",
        "   reduce trade count and PnL.",
        "2. Friction is flat 0.005 SOL/round-trip regardless of trade size.",
        "   At larger sizes friction is a smaller %; this backtest understates",
        "   the friction-floor advantage of bigger trades.",
        "3. Rejected-mint outcomes are 10-min MC snapshots, not full lifecycles.",
        "   Real strategies would exit on stops/TPs before the snapshot.",
        "4. Survivorship bias: the data only includes mints we *saw*. Mints",
        "   that died too fast for the WS to deliver them never enter the set.",
        "",
        "Use this for **relative ranking** of configs, not absolute PnL forecasts.",
    ]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--counterfactual", default="logs/counterfactual.jsonl")
    ap.add_argument("--trades",         default="logs/closed_trades.jsonl")
    ap.add_argument("--output",         default="analytics/backtest.md")
    ap.add_argument("--configs",        default=",".join(CONFIGS.keys()),
                    help="comma-separated config names to compare")
    ap.add_argument("--baseline",       default="Baseline")
    args = ap.parse_args()

    sigs = _load_signals(args.counterfactual, args.trades)
    print(f"[BACKTEST] loaded {len(sigs)} signals from {args.counterfactual} + {args.trades}")

    chosen = [c.strip() for c in args.configs.split(",") if c.strip() in CONFIGS]
    if args.baseline not in chosen:
        chosen.insert(0, args.baseline)

    results = {name: _run_config(sigs, CONFIGS[name]) for name in chosen}

    report = _render_report(results, baseline=args.baseline)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"[BACKTEST] wrote {args.output}")
    for name, r in results.items():
        print(f"  {name:12s}  trades={r['n_traded']:5d}  "
              f"WR={r['win_rate']*100:5.1f}%  PnL={r['total_pnl_sol']:+.4f} SOL  "
              f"MaxDD={r['max_drawdown']:.4f}")


if __name__ == "__main__":
    main()
