"""
tools/synthetic_price_mover.py

Async task that walks the bot's open positions through realistic
memecoin price trajectories so synthetic tokens actually move,
allowing the bot's exit logic (TP ladder, trailing stops, stop-loss,
no-movement, time-exit) to fire on something other than friction-loss.

Without this, synthetic mints have `current_price == entry_price`
forever (no on-chain price exists) so every position exits via
no_movement at −100% of size after stampede friction.

DISTRIBUTION (calibrated against the rates documented in
analytics/holdout_validation.md):

  RUG       36% — price falls 50–95% over 30–120s
  FLAT      40% — drifts ±10% randomly
  PUMP      21% — climbs 25–200% over 60–180s
  MOON       3% — climbs 5×–20× over 180–300s (the heavy tail)

These rates produce per-trade EV near zero with the bot's default TP
ladder + −15% stop, matching what the live audit measured. Use this
to stress-test the exit logic at scale without paying real friction
on a real chain.

⚠️  DEV TOOL ONLY. Gated on SYNTHETIC_PRICE_MOVES in main.py. Auto-on
when SYNTHETIC_INJECT is also set (you basically never want one
without the other). NEVER active in LIVE mode.

Implementation notes
--------------------
- Each mint gets a "fate" assigned on first sight (random.choices with
  the rates above) and a target price + duration.
- The walker steps every PRICE_TICK_S, moving current_price linearly
  toward target until duration elapses or the position closes.
- Update goes through risk_mgr.update_price() (which also calls
  pos.price_history.append) so the risk-monitor's stall detector,
  trailing stop, and TP logic all see fresh data.
- Also calls executor.update_price() so PaperExecutor.sell sees the
  same price the risk manager does.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

from loguru import logger

# How often to step every open synthetic position toward its fate.
PRICE_TICK_S = 2.0

# Synthetic-mint sniff — recognize mints created by tools/synthetic_injector.py
SYNTHETIC_MINT_PREFIX = "SYN_"


@dataclass
class _Fate:
    """A token's destiny. Linear walk from entry → target_mult over
    duration_s seconds. Walker stops touching the position once
    elapsed ≥ duration_s (price stays at target for any subsequent
    ticks until the bot exits)."""
    kind:        str             # "rug" | "flat" | "pump" | "moon"
    target_mult: float           # multiplier vs entry_price at end of duration
    duration_s:  float
    start_ts:    float           # epoch sec when first tick fired


# Weighted catalog. Each entry: (kind, weight, mult_range, duration_range_s)
_FATE_CATALOG = [
    # 36% — rugs
    ("rug",   36,  (0.05, 0.50),   (30,  120)),
    # 40% — flat (oscillates around 1.0 ±10%, the walker handles this specially)
    ("flat",  40,  (0.90, 1.10),   (60,  240)),
    # 21% — modest pumps
    ("pump",  21,  (1.25, 3.00),   (60,  180)),
    # 3% — moonshots (heavy tail; rare but big)
    ("moon",   3,  (5.0, 20.0),    (180, 300)),
]


def _draw_fate(rng: random.Random) -> _Fate:
    """Sample a fate from the catalog. Pure: given the same RNG state,
    returns the same fate — used by tests to verify the distribution."""
    weights = [w for _, w, _, _ in _FATE_CATALOG]
    kinds   = [k for k, _, _, _ in _FATE_CATALOG]
    kind = rng.choices(kinds, weights=weights, k=1)[0]
    _, _, mult_range, dur_range = next(c for c in _FATE_CATALOG if c[0] == kind)
    mult     = rng.uniform(*mult_range)
    duration = rng.uniform(*dur_range)
    return _Fate(
        kind        = kind,
        target_mult = mult,
        duration_s  = duration,
        start_ts    = time.time(),
    )


def _interpolate_price(entry: float, fate: _Fate, now: float, rng: random.Random) -> float:
    """Return the current synthetic price given how much time has passed
    since fate assignment. Linear for rug/pump/moon; random walk within
    band for flat (the realistic memecoin behavior where most tokens
    just drift sideways)."""
    elapsed = max(0.0, now - fate.start_ts)
    progress = min(1.0, elapsed / max(fate.duration_s, 1e-9))

    if fate.kind == "flat":
        # Independent random walk within [0.90, 1.10] of entry, with
        # mild momentum so it doesn't look like white noise. Bounded.
        # Caller seeds the RNG per-mint so the walk is deterministic
        # for tests.
        drift  = rng.uniform(-0.03, 0.03)
        center = 1.0
        bounded = max(0.85, min(1.15, center + drift * (1 + progress)))
        return entry * bounded

    # rug / pump / moon: linear walk from 1.0 → target_mult
    mult = 1.0 + (fate.target_mult - 1.0) * progress
    return entry * mult


class SyntheticPriceMover:
    """Background task that ticks every open synthetic position toward
    its fate. Stateless across restarts — fates reassign when the bot
    boots fresh. That's fine: the point is realistic-shape simulation,
    not deterministic replay across runs."""

    def __init__(self, risk_mgr, executor, seed: int | None = None):
        self.risk_mgr = risk_mgr
        self.executor = executor
        # Per-mint state: (entry_price, fate, rng_for_flat_walk)
        self._tracked: dict[str, tuple[float, _Fate, random.Random]] = {}
        # Master RNG; reseeded per run unless caller pins it (tests).
        self._rng_master = random.Random(seed if seed is not None else int(time.time()))

    def _ensure_fate(self, mint: str, entry_price: float) -> tuple[_Fate, random.Random]:
        """Assign a fate the first time we see a mint. Per-mint RNG is
        seeded from the master + the mint string so two mints with the
        same fate kind don't produce identical walks."""
        existing = self._tracked.get(mint)
        if existing is not None:
            return existing[1], existing[2]
        fate    = _draw_fate(self._rng_master)
        per_rng = random.Random(self._rng_master.random() + hash(mint) % (1 << 32))
        self._tracked[mint] = (entry_price, fate, per_rng)
        logger.info(
            f"[SYNTHETIC-PRICE] {mint[:10]} fate={fate.kind} "
            f"target={fate.target_mult:.2f}× duration={fate.duration_s:.0f}s"
        )
        return fate, per_rng

    async def run(self) -> None:
        """Main loop: every PRICE_TICK_S seconds, walk each open
        synthetic position toward its fate."""
        logger.warning(
            "[SYNTHETIC-PRICE] Mover active — driving open synthetic positions "
            "through realistic memecoin distribution. NEVER enable in live."
        )
        while True:
            try:
                now = time.time()
                # Snapshot keys so concurrent close_position doesn't error us.
                for mint in list(self.risk_mgr.positions.keys()):
                    if not mint.startswith(SYNTHETIC_MINT_PREFIX):
                        continue   # only touch synthetic mints
                    pos = self.risk_mgr.positions.get(mint)
                    if pos is None or pos.entry_price_sol <= 0:
                        continue
                    fate, rng = self._ensure_fate(mint, pos.entry_price_sol)
                    new_price = _interpolate_price(
                        pos.entry_price_sol, fate, now, rng,
                    )
                    # Drive both the risk_manager's view (TP/stop/trail
                    # logic reads pos.current_price) AND the paper
                    # executor's price cache (sell() reads it for fills).
                    self.risk_mgr.update_price(mint, new_price)
                    if hasattr(self.executor, "update_price"):
                        self.executor.update_price(mint, new_price)
            except Exception as e:
                logger.debug(f"[SYNTHETIC-PRICE] tick error: {e}")
            await asyncio.sleep(PRICE_TICK_S)
