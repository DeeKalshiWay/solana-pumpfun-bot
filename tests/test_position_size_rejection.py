"""Tests for RiskManager.calculate_position_size rejection reasons.

The bot used to log every sizing rejection as the generic "no_capacity"
on the dashboard — masking which guard actually fired (loss-streak
pause vs symbol cap vs daily-loss-limit vs hard exposure cap, etc.).
The sizer now returns (size, reject_reason). These tests pin the
contract: each guard surfaces the right stable token.

Stable tokens (alphabetical):
  emergency_stop, max_exposure, max_positions,
  paused_<sub>, size_below_min, symbol_cap
Success: ("", non-zero size).
"""
import time

import pytest

from config import (
    LOSS_STREAK_LIMIT,
    LOSS_STREAK_PAUSE_MIN,
    MAX_OPEN_POSITIONS,
    MAX_SYMBOL_LIFETIME_DEPLOY_PCT,
    MAX_TOTAL_EXPOSURE_SOL,
)
from risk.manager import Position, RiskManager


class _FakeWallet:
    pubkey = "FakeWalletPubkey1111111111111111111111111111"

    def __init__(self, balance: float = 2.5):
        self._balance = balance

    async def get_sol_balance(self) -> float:
        return self._balance


class _FakeExec:
    pass


def _new_position(mint: str, sol: float) -> Position:
    """Synthetic position for filling up positions / exposure."""
    return Position(
        mint              = mint,
        symbol            = mint[:6],
        creator           = "C" + mint[1:],
        entry_price_sol   = 1e-9,
        entry_time        = time.time(),
        sol_invested      = sol,
        tokens_held       = 1_000_000,
    )


@pytest.fixture
def rm(tmp_path, monkeypatch):
    """RiskManager with an isolated logs dir so symbol_deployed.json
    from prior runs doesn't bleed in."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    rm = RiskManager(_FakeWallet(), _FakeExec())
    rm.starting_sol_balance = 2.5
    return rm


class TestRejectReasons:
    @pytest.mark.asyncio
    async def test_emergency_stop(self, rm):
        rm.emergency_stop_active = True
        size, reason = await rm.calculate_position_size(80, symbol="T")
        assert size == 0.0
        assert reason == "emergency_stop"

    @pytest.mark.asyncio
    async def test_loss_streak_pause(self, rm):
        # Park the pause 30s in the future — _is_paused should see it.
        rm.loss_streak_pause_until = time.time() + 30
        size, reason = await rm.calculate_position_size(80, symbol="T")
        assert size == 0.0
        assert reason.startswith("paused_loss_streak_pause"), reason

    @pytest.mark.asyncio
    async def test_max_positions(self, rm):
        for i in range(MAX_OPEN_POSITIONS):
            mint = f"FILL{i:040d}"
            rm.positions[mint] = _new_position(mint, 0.001)
        size, reason = await rm.calculate_position_size(80, symbol="T")
        assert size == 0.0
        assert reason == "max_positions"

    @pytest.mark.asyncio
    async def test_symbol_cap(self, rm):
        # Deploy past the per-symbol cap. Cap is MAX_SYMBOL_LIFETIME_DEPLOY_PCT
        # of starting balance (2.5 SOL) → 0.25 SOL by default. Park 1 SOL.
        cap = rm._symbol_cap_sol()
        rm._symbol_deployed["HOTSYM"] = cap + 1.0
        size, reason = await rm.calculate_position_size(80, symbol="HOTSYM")
        assert size == 0.0
        assert reason == "symbol_cap"

    @pytest.mark.asyncio
    async def test_max_exposure(self, rm):
        # Fill exposure with positions whose combined sol_invested >=
        # MAX_TOTAL_EXPOSURE_SOL. MAX_OPEN_POSITIONS must be high enough
        # to fit them, otherwise that guard fires first. Use one big position.
        big = _new_position("BIG" + "X" * 41, MAX_TOTAL_EXPOSURE_SOL)
        rm.positions[big.mint] = big
        size, reason = await rm.calculate_position_size(80, symbol="T")
        assert size == 0.0
        # max_positions can fire first if MAX_OPEN_POSITIONS==1. Either is
        # a "no more capacity" answer the dashboard would group together,
        # so accept both as valid for this branch coverage.
        assert reason in ("max_exposure", "max_positions"), reason

    @pytest.mark.asyncio
    async def test_size_below_min(self, rm):
        # Wallet balance so tiny that base_size (sol_balance * MAX_POSITION_PCT)
        # falls under min_viable (0.003). 0.0001 SOL × 5% = 5e-6 — far below.
        # Skip the daily-loss circuit (which would trip first if a 2.5 SOL
        # baseline were inherited) by pinning baseline=balance and the
        # baseline date to today so _is_paused short-circuits as False.
        import datetime
        rm.wallet = _FakeWallet(balance=0.0001)
        rm.day_baseline_balance = 0.0001
        rm.day_baseline_date    = datetime.datetime.now(datetime.UTC).date()
        size, reason = await rm.calculate_position_size(80, symbol="T")
        assert size == 0.0
        assert reason == "size_below_min"

    @pytest.mark.asyncio
    async def test_success_returns_empty_reason(self, rm):
        size, reason = await rm.calculate_position_size(80, symbol="FRESHSYM")
        assert size > 0
        assert reason == ""


class TestRejectReasonTokensAreStable:
    """Pin the exact reason strings — the dashboard groups on these, so
    drifting them silently would break the histogram."""

    EXPECTED = {
        "emergency_stop",
        "max_positions",
        "symbol_cap",
        "max_exposure",
        "size_below_min",
    }

    @pytest.mark.asyncio
    async def test_no_typos(self, rm):
        # Smoke each path lightly to exercise the literal strings without
        # binding to wall-clock state. _is_paused returns False on a fresh
        # RM so we touch only the deterministic guards.
        observed = set()

        # emergency_stop
        rm.emergency_stop_active = True
        _, r = await rm.calculate_position_size(50, symbol="T")
        observed.add(r); rm.emergency_stop_active = False

        # max_positions
        for i in range(MAX_OPEN_POSITIONS):
            mint = f"P{i:043d}"
            rm.positions[mint] = _new_position(mint, 0.001)
        _, r = await rm.calculate_position_size(50, symbol="T")
        observed.add(r); rm.positions.clear()

        # symbol_cap
        rm._symbol_deployed["BAD"] = rm._symbol_cap_sol() + 1
        _, r = await rm.calculate_position_size(50, symbol="BAD")
        observed.add(r); rm._symbol_deployed.clear()

        # max_exposure (uses fewer positions, may overlap with max_positions
        # depending on MAX_OPEN_POSITIONS — still hits exposure tag here)
        big = _new_position("EXP" + "X" * 41, MAX_TOTAL_EXPOSURE_SOL + 1)
        rm.positions[big.mint] = big
        _, r = await rm.calculate_position_size(50, symbol="T")
        observed.add(r); rm.positions.clear()

        # size_below_min — clamp baseline so daily-loss doesn't pre-empt.
        import datetime
        rm.wallet = _FakeWallet(balance=0.0001)
        rm.day_baseline_balance = 0.0001
        rm.day_baseline_date    = datetime.datetime.now(datetime.UTC).date()
        _, r = await rm.calculate_position_size(50, symbol="T")
        observed.add(r)

        # Every observed reason must be one of the documented tokens.
        # paused_* is checked separately above; it's the only dynamic one.
        observed.discard("paused_loss_streak_pause_0min")
        assert observed.issubset(self.EXPECTED), (
            f"unexpected reason(s): {observed - self.EXPECTED}"
        )
