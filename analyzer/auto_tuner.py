"""
analyzer/auto_tuner.py

Self-tuning score threshold. Watches recent closed-trade win rate and
shifts MIN_BUY_SCORE up when WR drops (be more selective) or down when
WR is high and the bot is starving for signals (loosen up).

Conservative by design:
  - Only adjusts after MIN_SAMPLE_SIZE closed trades in the lookback
  - Bounded offset in [-3, +5] so we never deviate too far from the
    operator's chosen base
  - Logs every adjustment so it's auditable
  - Persists to logs/auto_tune_state.json so it survives restart

The offset is added to the static MIN_BUY_SCORE from config — call
auto_tuner.effective_min_score() to get the live threshold.
"""

import asyncio
import json
import os
import time

from loguru import logger

from config import MIN_BUY_SCORE

STATE_FILE        = "logs/auto_tune_state.json"
TICK_INTERVAL_S   = 1800   # re-evaluate every 30 min
LOOKBACK_TRADES   = 100    # trades back to compute WR
MIN_SAMPLE_SIZE   = 30     # need at least this many before adjusting
WR_LOW_THRESHOLD  = 0.45   # WR below this → tighten (offset += 1)
WR_HIGH_THRESHOLD = 0.70   # WR above this → loosen (offset -= 1)
OFFSET_MIN        = -3     # never let it loosen more than 3 below base
OFFSET_MAX        =  5     # never let it tighten more than 5 above base


class AutoTuner:
    def __init__(self):
        self.offset            = 0          # additive to MIN_BUY_SCORE
        self.last_wr           = 0.0
        self.last_sample_size  = 0
        self.last_action       = "init"
        self.last_updated      = 0.0
        self.adjustment_count  = 0
        self.running           = False
        self._risk_mgr         = None
        self._load()

    def attach(self, risk_manager):
        """Plug in the live risk_manager so we can read closed_trades."""
        self._risk_mgr = risk_manager

    def _load(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self.offset           = int(data.get("offset", 0))
            self.last_wr          = float(data.get("last_wr", 0.0))
            self.last_sample_size = int(data.get("last_sample_size", 0))
            self.last_action      = data.get("last_action", "loaded")
            self.last_updated     = float(data.get("last_updated", 0.0))
            self.adjustment_count = int(data.get("adjustment_count", 0))
            logger.info(
                f"[AUTO-TUNE] Restored state: offset={self.offset:+d} "
                f"effective={self.effective_min_score()} "
                f"(prior WR={self.last_wr:.0%}, last_action={self.last_action})"
            )
        except Exception as e:
            logger.warning(f"[AUTO-TUNE] state load failed: {e}")

    def _save(self):
        os.makedirs("logs", exist_ok=True)
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "offset":            self.offset,
                    "last_wr":           round(self.last_wr, 4),
                    "last_sample_size":  self.last_sample_size,
                    "last_action":       self.last_action,
                    "last_updated":      self.last_updated,
                    "adjustment_count":  self.adjustment_count,
                    "effective":         self.effective_min_score(),
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"[AUTO-TUNE] save failed: {e}")

    def effective_min_score(self) -> int:
        """Live threshold. Other modules should call this, not the constant."""
        return MIN_BUY_SCORE + self.offset

    def stats(self) -> dict:
        return {
            "base_min_score":   MIN_BUY_SCORE,
            "offset":           self.offset,
            "effective":        self.effective_min_score(),
            "last_wr":          round(self.last_wr, 4),
            "last_sample_size": self.last_sample_size,
            "last_action":      self.last_action,
            "last_updated":     self.last_updated,
            "adjustment_count": self.adjustment_count,
        }

    def _evaluate(self):
        """One pass of the tuning logic. Reads closed_trades from risk_mgr."""
        if self._risk_mgr is None:
            return
        recent = list(self._risk_mgr.closed_trades[-LOOKBACK_TRADES:])
        n = len(recent)
        if n < MIN_SAMPLE_SIZE:
            self.last_action      = f"hold_low_sample({n})"
            self.last_sample_size = n
            self.last_updated     = time.time()
            return

        wins = sum(1 for t in recent if t.get("pnl_sol", 0) > 0)
        wr   = wins / n
        self.last_wr          = wr
        self.last_sample_size = n
        self.last_updated     = time.time()

        prev_offset = self.offset
        if wr < WR_LOW_THRESHOLD and self.offset < OFFSET_MAX:
            self.offset += 1
            self.last_action = f"tighten_wr_low({wr:.0%})"
            self.adjustment_count += 1
            logger.warning(
                f"[AUTO-TUNE] Tightening: WR={wr:.0%} over last {n} trades < "
                f"{WR_LOW_THRESHOLD:.0%}. Offset {prev_offset:+d} → {self.offset:+d} "
                f"(threshold now {self.effective_min_score()})"
            )
        elif wr > WR_HIGH_THRESHOLD and self.offset > OFFSET_MIN:
            self.offset -= 1
            self.last_action = f"loosen_wr_high({wr:.0%})"
            self.adjustment_count += 1
            logger.info(
                f"[AUTO-TUNE] Loosening: WR={wr:.0%} over last {n} trades > "
                f"{WR_HIGH_THRESHOLD:.0%}. Offset {prev_offset:+d} → {self.offset:+d} "
                f"(threshold now {self.effective_min_score()})"
            )
        else:
            self.last_action = f"hold_wr_normal({wr:.0%})"
            logger.debug(
                f"[AUTO-TUNE] Hold: WR={wr:.0%} over last {n} trades "
                f"(threshold stays {self.effective_min_score()})"
            )

        self._save()

    async def run(self):
        """Long-running background task. Evaluates every TICK_INTERVAL_S."""
        self.running = True
        # First pass on startup so we adapt quickly without waiting 30 min
        await asyncio.sleep(60)
        if self.running:
            try:
                self._evaluate()
            except Exception as e:
                logger.warning(f"[AUTO-TUNE] eval error: {e}")
        while self.running:
            await asyncio.sleep(TICK_INTERVAL_S)
            try:
                self._evaluate()
            except Exception as e:
                logger.warning(f"[AUTO-TUNE] eval error: {e}")

    def stop(self):
        self.running = False
        self._save()


# Singleton — imported by signal_scorer + main
auto_tuner = AutoTuner()
