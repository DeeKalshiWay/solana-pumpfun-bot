"""
analyzer/score_bins.py

Reads logs/closed_trades.jsonl and aggregates trade outcomes by entry-score
band (40-49, 50-59, 60-69, ...). Surfaces:

    - count of trades per band
    - win rate per band
    - average PnL % per band
    - expected value per band

If your score=70+ trades win at 55% but score=42-49 trades win at 18%, the
chart makes it obvious that MIN_BUY_SCORE should be raised.

This is read-only computation over the trade log — it doesn't write anything,
so it's safe to call from the web API on demand.
"""

import json
import os
from collections import defaultdict

CLOSED_TRADES_FILE = "logs/closed_trades.jsonl"
BAND_WIDTH         = 10   # group scores in 10-pt bins


def _load_trades() -> list:
    if not os.path.exists(CLOSED_TRADES_FILE):
        return []
    out = []
    try:
        with open(CLOSED_TRADES_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def aggregate_by_score() -> dict:
    """
    Returns:
        {
            "bands": [
                {"band": "40-49", "count": 12, "win_rate": 25.0,
                 "avg_pnl_pct": -3.4, "avg_pnl_sol": -0.0008,
                 "ev_pct": -3.4, "wins": 3, "losses": 9},
                ...
            ],
            "total": 38,
        }
    """
    trades = _load_trades()
    if not trades:
        return {"bands": [], "total": 0}

    # closed_trades.jsonl rows include score because we attached pos.score on close.
    # Fall back to 0 if missing.
    by_band = defaultdict(list)
    for t in trades:
        score = int(t.get("score", 0) or 0)
        band  = (score // BAND_WIDTH) * BAND_WIDTH
        by_band[band].append(t)

    out = []
    for band in sorted(by_band.keys()):
        rows = by_band[band]
        n    = len(rows)
        wins = [r for r in rows if r.get("pnl_sol", 0) > 0]
        losses = [r for r in rows if r.get("pnl_sol", 0) <= 0]
        avg_pnl_pct = sum(r.get("pnl_pct", 0) for r in rows) / n
        avg_pnl_sol = sum(r.get("pnl_sol", 0) for r in rows) / n
        win_rate    = len(wins) / n * 100 if n else 0
        # EV = win_rate * avg_winner + (1-win_rate) * avg_loser
        avg_winner = (sum(r["pnl_pct"] for r in wins) / len(wins)) if wins else 0
        avg_loser  = (sum(r["pnl_pct"] for r in losses) / len(losses)) if losses else 0
        ev = (win_rate / 100) * avg_winner + (1 - win_rate / 100) * avg_loser
        out.append({
            "band":         f"{band}-{band + BAND_WIDTH - 1}",
            "band_low":     band,
            "count":        n,
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(win_rate, 1),
            "avg_pnl_pct":  round(avg_pnl_pct, 2),
            "avg_pnl_sol":  round(avg_pnl_sol, 6),
            "avg_winner":   round(avg_winner, 2),
            "avg_loser":    round(avg_loser, 2),
            "ev_pct":       round(ev, 2),
        })

    return {"bands": out, "total": len(trades)}
