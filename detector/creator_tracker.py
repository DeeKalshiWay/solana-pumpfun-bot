"""
detector/creator_tracker.py
Tracks pump.fun creator performance over time.

Strategy: FLOCK4H/Dexter approach — rank creators by graduation rate,
only score-boost tokens from proven top creators.

Graduation = token reached ~85 SOL bonding curve (~$69K MC) and migrated.
Top 10 creators show statistically significant graduation advantage.
"""

import json
import os
import time

from loguru import logger

from config import CREATOR_DB_FILE, CREATOR_MIN_LAUNCHES, CREATOR_TOP_N


class CreatorTracker:
    """
    Maintains a persistent leaderboard of pump.fun creators.
    Tracks: total launches, successful trades, win rate, average hold time.
    """

    def __init__(self):
        self._db: dict = {}     # creator_address -> stats dict
        self._load()

    def _load(self):
        os.makedirs("logs", exist_ok=True)
        if os.path.exists(CREATOR_DB_FILE):
            try:
                with open(CREATOR_DB_FILE) as f:
                    self._db = json.load(f)
                logger.info(f"[CREATOR] Loaded {len(self._db)} creators from DB")
            except Exception as e:
                logger.warning(f"[CREATOR] Could not load DB: {e}")
                self._db = {}

    def _save(self):
        # Atomic write — write to .tmp then os.replace to avoid half-written
        # files when the process dies mid-save (which corrupted the DB last time).
        tmp = CREATOR_DB_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._db, f, indent=2, ensure_ascii=True)
            os.replace(tmp, CREATOR_DB_FILE)
        except Exception as e:
            logger.debug(f"[CREATOR] Save error: {e}")
            try: os.remove(tmp)
            except OSError: pass

    def _get(self, creator: str) -> dict:
        if creator not in self._db:
            self._db[creator] = {
                "launches":        0,
                "wins":            0,       # profitable exits
                "losses":          0,
                "total_pnl_sol":   0.0,
                "last_seen":       0,
                "graduation_count": 0,      # tokens that hit migration
            }
        return self._db[creator]

    # ── Record events ─────────────────────────────────────────────────────────
    def record_launch(self, creator: str):
        """Call when a new token from this creator is detected."""
        s = self._get(creator)
        s["launches"] += 1
        s["last_seen"] = time.time()
        self._save()

    def record_trade_result(self, creator: str, pnl_sol: float, graduated: bool = False):
        """Call when a position opened on this creator's token is closed."""
        s = self._get(creator)
        if pnl_sol > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["total_pnl_sol"] = round(s["total_pnl_sol"] + pnl_sol, 6)
        if graduated:
            s["graduation_count"] += 1
        self._save()

    # ── Query ─────────────────────────────────────────────────────────────────
    def get_win_rate(self, creator: str) -> float:
        s = self._db.get(creator, {})
        total = s.get("wins", 0) + s.get("losses", 0)
        if total == 0:
            return 0.0
        return s["wins"] / total

    def get_score_bonus(self, creator: str) -> int:
        """
        Return a score bonus (0-20) for tokens from this creator.
        Top 10 creators: +20, top 50: +10, known bad: -10, unknown: 0.
        """
        s = self._db.get(creator, {})
        launches = s.get("launches", 0)

        if launches < CREATOR_MIN_LAUNCHES:
            return 0   # not enough data

        win_rate  = self.get_win_rate(creator)
        rank      = self._get_rank(creator)

        if rank is None:
            # Known creator but not in top N
            if win_rate < 0.2:
                return -10   # known bad actor
            return 0

        if rank <= 10:
            return 20
        if rank <= CREATOR_TOP_N:
            return 10
        return 0

    def is_top_creator(self, creator: str) -> bool:
        return self._get_rank(creator) is not None

    def is_blacklisted(self, creator: str) -> bool:
        """
        Hard-reject creator if our own trades on their tokens have demonstrated
        a real edge against us: 3+ closed trades, net negative SOL, win rate <25%.
        """
        s = self._db.get(creator, {})
        wins   = s.get("wins", 0)
        losses = s.get("losses", 0)
        total  = wins + losses
        if total < 3:
            return False
        pnl = s.get("total_pnl_sol", 0.0)
        wr  = wins / total
        return pnl < 0 and wr < 0.25

    def _get_rank(self, creator: str) -> int | None:
        """Return 1-based rank of creator in top-N list, or None."""
        if not self._db:
            return None

        qualified = {
            addr: data for addr, data in self._db.items()
            if data.get("launches", 0) >= CREATOR_MIN_LAUNCHES
        }
        ranked = sorted(
            qualified.items(),
            key=lambda x: (
                self.get_win_rate(x[0]),
                x[1].get("total_pnl_sol", 0)
            ),
            reverse=True
        )
        for i, (addr, _) in enumerate(ranked[:CREATOR_TOP_N], start=1):
            if addr == creator:
                return i
        return None

    def get_leaderboard(self, top_n: int = 10) -> list:
        """Return top N creators with stats, for logging."""
        qualified = [
            (addr, data) for addr, data in self._db.items()
            if data.get("launches", 0) >= CREATOR_MIN_LAUNCHES
        ]
        ranked = sorted(
            qualified,
            key=lambda x: (self.get_win_rate(x[0]), x[1].get("total_pnl_sol", 0)),
            reverse=True
        )
        result = []
        for addr, data in ranked[:top_n]:
            result.append({
                "creator":    addr[:12] + "...",
                "launches":   data["launches"],
                "win_rate":   round(self.get_win_rate(addr) * 100, 1),
                "pnl_sol":    data["total_pnl_sol"],
            })
        return result


# Singleton — imported by signal_scorer and risk_manager
creator_tracker = CreatorTracker()
