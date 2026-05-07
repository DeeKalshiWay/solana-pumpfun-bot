"""Smoke tests for risk.manager.Position — pure-function coverage of the
PnL math that drives every TP/SL/trailing decision. If this math is wrong,
the bot is wrong; if these tests pass, the math is sane."""
import time

import pytest

from risk.manager import Position


def _pos(entry: float = 1.0, current: float = 1.0) -> Position:
    return Position(
        mint="MintAddr",
        symbol="TICK",
        creator="creator1",
        entry_price_sol=entry,
        entry_time=time.time(),
        sol_invested=0.1,
        tokens_held=1000,
        current_price=current,
    )


class TestPnlPct:
    def test_no_change_returns_zero(self):
        p = _pos(entry=1.0, current=1.0)
        assert p.pnl_pct == pytest.approx(0.0)

    def test_double_returns_100_pct(self):
        p = _pos(entry=1.0, current=2.0)
        assert p.pnl_pct == pytest.approx(100.0)

    def test_half_returns_minus_50_pct(self):
        p = _pos(entry=1.0, current=0.5)
        assert p.pnl_pct == pytest.approx(-50.0)

    def test_zero_entry_does_not_divide(self):
        # Defensive: degenerate state shouldn't crash the monitor loop.
        p = _pos(entry=0.0, current=1.0)
        assert p.pnl_pct == 0

    def test_moonshot_10x(self):
        p = _pos(entry=0.001, current=0.01)
        assert p.pnl_pct == pytest.approx(900.0)

    def test_total_loss(self):
        p = _pos(entry=1.0, current=0.0)
        assert p.pnl_pct == pytest.approx(-100.0)


class TestAgeMinutes:
    def test_fresh_position_is_near_zero(self):
        p = _pos()
        assert 0.0 <= p.age_minutes < 0.1

    def test_old_position_is_positive(self):
        p = _pos()
        p.entry_time -= 600  # 10 min ago
        assert p.age_minutes == pytest.approx(10.0, rel=0.01)
