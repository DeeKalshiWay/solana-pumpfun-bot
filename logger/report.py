"""
logger/report.py
Periodic performance snapshot logger.

Writes a JSONL row to logs/report.jsonl every hour (one line = one snapshot)
so you can chart equity, win rate, and trade flow over the paper-trading period.

Also produces a "verdict" — a research-backed read on whether the strategy looks
profitable enough to risk real money on.
"""

import asyncio
import json
import os
import time

from loguru import logger

REPORT_FILE       = "logs/report.jsonl"
SNAPSHOT_INTERVAL = 3600   # 1 hour between snapshots
MIN_SNAPSHOTS_FOR_VERDICT = 24 * 7   # 7 days of hourly data before we judge


class ReportLogger:
    """
    Background task that periodically dumps the bot's state to a JSONL file.
    Each line is a self-contained JSON object: easy to grep, pipe, or chart.
    """

    def __init__(self, risk_manager, signal_scorer):
        self.risk_mgr = risk_manager
        self.scorer   = signal_scorer
        self.start_ts = time.time()
        self.running  = False
        os.makedirs("logs", exist_ok=True)

    # ── Snapshot construction ────────────────────────────────────────────────
    async def _snapshot(self) -> dict:
        stats   = self.risk_mgr.get_stats()
        balance = await self.risk_mgr.wallet.get_sol_balance()

        # Per-exit-reason aggregates
        exit_breakdown = {}
        for t in self.risk_mgr.closed_trades:
            r = t.get("reason", "unknown")
            if r not in exit_breakdown:
                exit_breakdown[r] = {"count": 0, "pnl_sol": 0.0}
            exit_breakdown[r]["count"]   += 1
            exit_breakdown[r]["pnl_sol"] += t.get("pnl_sol", 0)
        for r in exit_breakdown:
            exit_breakdown[r]["pnl_sol"] = round(exit_breakdown[r]["pnl_sol"], 6)

        # Winner/loser averages
        wins   = [t["pnl_pct"] for t in self.risk_mgr.closed_trades if t["pnl_sol"] >  0]
        losses = [t["pnl_pct"] for t in self.risk_mgr.closed_trades if t["pnl_sol"] <= 0]

        avg_winner = round(sum(wins)   / len(wins),   2) if wins   else 0
        avg_loser  = round(sum(losses) / len(losses), 2) if losses else 0
        best_trade  = round(max(wins),   2) if wins   else 0
        worst_trade = round(min(losses), 2) if losses else 0

        return {
            "ts":               int(time.time()),
            "uptime_hours":     round((time.time() - self.start_ts) / 3600, 2),
            "balance_sol":      round(balance, 6),
            "starting_sol":     round(self.risk_mgr.starting_sol_balance, 6),
            "total_pnl_sol":    round(stats["total_pnl_sol"], 6),
            "pnl_pct":          self._pnl_pct(),
            "open_positions":   stats["open_positions"],
            "exposure_sol":     round(stats["total_exposure"], 6),
            "closed_trades":    stats["closed_trades"],
            "wins":             len(wins),
            "losses":           len(losses),
            "win_rate":         round(stats["win_rate"] * 100, 2),
            "avg_winner_pct":   avg_winner,
            "avg_loser_pct":    avg_loser,
            "best_trade_pct":   best_trade,
            "worst_trade_pct":  worst_trade,
            "expected_value":   self._expected_value(wins, losses),
            "scored_count":     self.scorer.scored_count,
            "exit_breakdown":   exit_breakdown,
            "emergency_stop":   stats["emergency_stop"],
        }

    def _pnl_pct(self) -> float:
        start = self.risk_mgr.starting_sol_balance
        if start <= 0:
            return 0
        stats = self.risk_mgr.get_stats()
        return round((stats["total_pnl_sol"] / start) * 100, 2)

    def _expected_value(self, wins: list, losses: list) -> float:
        """Per-trade expected value as %, rounded."""
        n = len(wins) + len(losses)
        if n == 0:
            return 0
        avg_w = (sum(wins)   / len(wins))   if wins   else 0
        avg_l = (sum(losses) / len(losses)) if losses else 0
        win_p = len(wins) / n
        return round((win_p * avg_w) + ((1 - win_p) * avg_l), 2)

    # ── Verdict logic ────────────────────────────────────────────────────────
    def verdict(self, snapshots: list) -> dict:
        """
        Decide whether the strategy looks fundable based on accumulated data.
        Honest thresholds — no hype.
        """
        n = len(snapshots)
        if n < 24:
            return {
                "status":  "warming_up",
                "label":   "WARMING UP",
                "color":   "blue",
                "message": f"Only {n} hours of data. Need at least 24h to start drawing conclusions.",
            }
        if n < MIN_SNAPSHOTS_FOR_VERDICT:
            days = round(n / 24, 1)
            return {
                "status":  "early",
                "label":   "EARLY DATA",
                "color":   "blue",
                "message": f"{days} days of data so far. Decision threshold is 7 days.",
            }

        # We have at least a week. Look at cumulative PnL % and trade count.
        latest    = snapshots[-1]
        pnl_pct   = latest.get("pnl_pct", 0)
        trades    = latest.get("closed_trades", 0)
        ev        = latest.get("expected_value", 0)
        win_rate  = latest.get("win_rate", 0)

        if trades < 20:
            return {
                "status":  "low_volume",
                "label":   "LOW VOLUME",
                "color":   "gold",
                "message": (f"Only {trades} closed trades after {round(n/24,1)} days. "
                            f"Sample too small. Either market is dry or filters are too strict — "
                            f"consider lowering MIN_BUY_SCORE."),
            }

        if pnl_pct >= 5 and ev > 0:
            return {
                "status":  "positive",
                "label":   "POSITIVE EV",
                "color":   "green",
                "message": (f"+{pnl_pct}% over {round(n/24,1)} days, "
                            f"win rate {win_rate}%, EV {ev}%/trade. "
                            f"Consider going live with 0.5 SOL after upgrading to Helius RPC."),
            }
        if -5 <= pnl_pct <= 5:
            return {
                "status":  "breakeven",
                "label":   "BREAK-EVEN",
                "color":   "gold",
                "message": (f"{pnl_pct:+.1f}% over {round(n/24,1)} days. "
                            f"Strategy works but stack drag (latency, slippage) eats the edge. "
                            f"Don't go live until you upgrade RPC or improve filters."),
            }
        return {
            "status":  "negative",
            "label":   "NEGATIVE EV",
            "color":   "red",
            "message": (f"{pnl_pct:+.1f}% over {round(n/24,1)} days, "
                        f"EV {ev}%/trade. Strategy is bleeding. "
                        f"Do not fund this. Tune filters or shelve."),
        }

    # ── File I/O ─────────────────────────────────────────────────────────────
    def append(self, snapshot: dict):
        try:
            with open(REPORT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot) + "\n")
        except Exception as e:
            logger.warning(f"[REPORT] write error: {e}")

    def load_all(self) -> list:
        if not os.path.exists(REPORT_FILE):
            return []
        out = []
        try:
            with open(REPORT_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"[REPORT] read error: {e}")
        return out

    # ── Background loop ──────────────────────────────────────────────────────
    async def run(self):
        self.running = True
        logger.info(f"Report logger started — snapshot every {SNAPSHOT_INTERVAL//60} min to {REPORT_FILE}")

        # Take an initial snapshot 5 min after startup so we have a t0 baseline
        await asyncio.sleep(300)
        try:
            snap = await self._snapshot()
            self.append(snap)
            logger.info(f"[REPORT] initial snapshot written | balance={snap['balance_sol']} SOL")
        except Exception as e:
            logger.warning(f"[REPORT] initial snapshot error: {e}")

        while self.running:
            await asyncio.sleep(SNAPSHOT_INTERVAL)
            try:
                snap = await self._snapshot()
                self.append(snap)
                logger.info(
                    f"[REPORT] snapshot @ {snap['uptime_hours']}h | "
                    f"PnL {snap['pnl_pct']:+.2f}% | "
                    f"{snap['closed_trades']} trades | "
                    f"win_rate {snap['win_rate']}%"
                )
            except Exception as e:
                logger.warning(f"[REPORT] snapshot error: {e}")

    def stop(self):
        self.running = False
