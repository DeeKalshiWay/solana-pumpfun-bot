"""
analyzer/regime_filter.py

Regime filter — pauses new buys when pump.fun's new-mint rate has collapsed.

Memecoin pumps require liquidity, attention, and competitive bots burning
gas to chase the same launches. When the broad new-mint rate drops (overnight,
on holidays, after major market events), the bot's edge collapses faster than
its loss model — every trade is worse, not just the marginal one.

Mechanism:
  - subscribe_create callback on the shared PumpFunMonitor records each new
    mint's timestamp.
  - Sliding window: keep only the last 24h of timestamps.
  - should_pause() returns True iff:
      * we have at least REGIME_MIN_HOURS of data (bootstrap-safe), AND
      * the trailing 60-min count is < REGIME_PAUSE_RATIO * (median hourly
        count over the trailing 24h)

The signal_scorer queries should_pause() before passing a token through and
emits "regime_dead_hour" as the reject reason when it fires. That logs the
rejection into counterfactual.jsonl with the rest, so the held-out validator
can re-evaluate the filter periodically.

Singleton: `regime_filter` is constructed at module import time.
"""

from __future__ import annotations

import statistics
import time
from collections import deque

from loguru import logger

from config import REGIME_FILTER_ENABLED, REGIME_MIN_HOURS, REGIME_PAUSE_RATIO

WINDOW_SECONDS = 24 * 3600
HOUR_SECONDS   = 3600


class RegimeFilter:
    def __init__(self):
        # Ring buffer of mint create timestamps (epoch seconds). Pruned on
        # every record_new_mint() call. Memory-bounded at ~24h worth, which
        # for pump.fun is on the order of 50-100K entries — totally fine.
        self._timestamps: deque[float] = deque()
        # Cache the last computation so should_pause() doesn't recompute
        # the median on every signal_scorer call. Refreshed every 60s.
        self._last_check_ts: float = 0
        self._last_pause: bool     = False
        self._last_metrics: dict   = {}

    def record_new_mint(self, _data: dict | None = None) -> None:
        """Subscribe-callback signature for PumpFunMonitor. Argument is the
        create event dict; we only need to know that one happened."""
        now = time.time()
        self._timestamps.append(now)
        # Prune anything older than the 24h window
        cutoff = now - WINDOW_SECONDS
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def _compute(self) -> tuple[bool, dict]:
        """Returns (should_pause, metrics_dict)."""
        if not REGIME_FILTER_ENABLED:
            return False, {"enabled": False}

        now = time.time()
        if not self._timestamps:
            return False, {"reason": "no_data"}

        # Bootstrap guard: need at least REGIME_MIN_HOURS of history before
        # the filter activates. Without this, a fresh bot start would always
        # pause itself ("only 5 mints in window, must be a dead hour").
        oldest = self._timestamps[0]
        history_hours = (now - oldest) / HOUR_SECONDS
        if history_hours < REGIME_MIN_HOURS:
            return False, {
                "reason":          "bootstrap",
                "history_hours":   round(history_hours, 2),
                "min_required":    REGIME_MIN_HOURS,
            }

        # Trailing 60-min count
        last_hour_cutoff = now - HOUR_SECONDS
        recent_count = sum(1 for t in self._timestamps if t >= last_hour_cutoff)

        # Hourly histogram across the 24h window for the median baseline.
        # Bucket by integer hour offset from `now`. Hours older than the
        # bootstrap floor are still counted in the baseline — that's the
        # *typical* hour, not the *recent* hour.
        hour_buckets: dict[int, int] = {}
        for t in self._timestamps:
            bucket = int((now - t) // HOUR_SECONDS)  # 0=current hour, 1=last hour, ...
            if 0 <= bucket < 24:
                hour_buckets[bucket] = hour_buckets.get(bucket, 0) + 1
        # Use only completed past hours (1..23) for the median — the current
        # hour (bucket 0) is the one we're comparing against and shouldn't
        # be both the input and the baseline.
        past_hour_counts = [hour_buckets.get(b, 0) for b in range(1, 24)]
        if not past_hour_counts:
            return False, {"reason": "no_baseline"}
        baseline = statistics.median(past_hour_counts)
        if baseline <= 0:
            return False, {"reason": "zero_baseline"}

        threshold = baseline * REGIME_PAUSE_RATIO
        should_pause = recent_count < threshold

        return should_pause, {
            "recent_60min":   recent_count,
            "baseline_median": baseline,
            "threshold":      round(threshold, 1),
            "ratio":          REGIME_PAUSE_RATIO,
            "history_hours":  round(history_hours, 2),
        }

    def should_pause(self) -> bool:
        now = time.time()
        # Refresh cached decision at most every 60s — cheaper than
        # recomputing the median on every signal.
        if (now - self._last_check_ts) > 60:
            paused, metrics = self._compute()
            # State transition logging
            if paused != self._last_pause:
                if paused:
                    logger.warning(
                        f"[REGIME] Pausing new buys — recent={metrics.get('recent_60min')} "
                        f"vs median={metrics.get('baseline_median')} (×{REGIME_PAUSE_RATIO})"
                    )
                else:
                    logger.info(
                        f"[REGIME] Resuming new buys — recent={metrics.get('recent_60min')} "
                        f"vs median={metrics.get('baseline_median')}"
                    )
            self._last_check_ts = now
            self._last_pause = paused
            self._last_metrics = metrics
        return self._last_pause

    def attach(self, monitor) -> None:
        """Register on the shared PumpFunMonitor. Single create callback."""
        monitor.subscribe_create(self.record_new_mint)
        logger.info("[REGIME] Attached to shared PumpPortal WS")

    def metrics(self) -> dict:
        """Read-only metrics snapshot for the dashboard or debugging."""
        return dict(self._last_metrics)


# Module-level singleton, imported by main.py and signal_scorer
regime_filter = RegimeFilter()
