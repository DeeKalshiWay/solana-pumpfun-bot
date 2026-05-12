"""Tests for analytics.go_live_gate.

Each criterion is a pure function over (trades, thresholds) — no I/O.
These pin the pass/fail/insufficient verdicts so a future refactor
doesn't quietly turn a NO-GO into a GO.
"""
from analytics.go_live_gate import (
    _check_ev_per_trade,
    _check_payoff_breakeven,
    _check_symbol_concentration,
)


def _t(pnl_sol: float, symbol: str = "X") -> dict:
    return {"pnl_sol": pnl_sol, "symbol": symbol}


class TestEVPerTrade:
    def test_insufficient_data_below_min(self):
        v = _check_ev_per_trade([_t(1)] * 5, min_trades=200)
        assert v.passed is None     # not False — we want fail-OPEN here
        assert "5 trades" in v.detail

    def test_fails_when_ev_negative(self):
        trades = [_t(-0.01)] * 200
        v = _check_ev_per_trade(trades, min_trades=200)
        assert v.passed is False
        assert "EV -0" in v.detail

    def test_passes_when_ev_positive_above_min(self):
        trades = [_t(0.01)] * 200
        v = _check_ev_per_trade(trades, min_trades=200)
        assert v.passed is True

    def test_empty_is_insufficient_not_fail(self):
        v = _check_ev_per_trade([], min_trades=200)
        assert v.passed is None


class TestPayoffBreakeven:
    def test_insufficient_when_no_losses(self):
        v = _check_payoff_breakeven([_t(0.01), _t(0.02), _t(0.05)])
        assert v.passed is None

    def test_fails_on_asymmetric_loss_dominance(self):
        # 66% WR but losses are 4× the wins → EV negative
        trades = [_t(0.01)] * 4 + [_t(-0.04)] * 2
        v = _check_payoff_breakeven(trades)
        assert v.passed is False

    def test_passes_when_payoff_clears_breakeven(self):
        # 60% WR, +0.02 win vs −0.01 loss → EV positive
        trades = [_t(0.02)] * 6 + [_t(-0.01)] * 4
        v = _check_payoff_breakeven(trades)
        assert v.passed is True


class TestSymbolConcentration:
    def test_fails_when_one_symbol_dominates(self):
        trades = [_t(0.10, "BIG"), _t(0.01, "A"), _t(0.01, "B"), _t(-0.01, "C")]
        v = _check_symbol_concentration(trades, max_pct=0.25)
        assert v.passed is False
        assert "BIG" in v.detail

    def test_passes_when_diversified(self):
        trades = [_t(0.02, c) for c in "ABCDEFGHIJ"]
        v = _check_symbol_concentration(trades, max_pct=0.25)
        assert v.passed is True

    def test_negative_concentration_counts_against_cap(self):
        """A symbol that LOST a lot also counts as concentration — we
        don't want one ticker to be most of the variance either direction."""
        trades = [_t(-0.10, "RUG"), _t(0.01, "A"), _t(0.01, "B")]
        v = _check_symbol_concentration(trades, max_pct=0.25)
        assert v.passed is False

    def test_zero_pnl_is_insufficient(self):
        v = _check_symbol_concentration([_t(0, "A"), _t(0, "B")], max_pct=0.25)
        assert v.passed is None
