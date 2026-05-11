"""Regression tests for the TP-partial-close path.

Before this fix, take-profit partial sells in risk_manager were only
locally accounted (pos.sol_invested *= 0.85) and never written to
trades.db. The auto_tuner read win-rate from closed_trades and only saw
the FINAL exit per position, systematically undercounting wins from
positions that hit TPs before stopping out. These tests pin the new
contract: every successful TP partial writes its own trade record.
"""
import time
from unittest.mock import AsyncMock, patch

import pytest

from logger.trade_db import TradeDB
from risk.manager import Position, RiskManager


class _FakeWallet:
    """Minimal wallet stub — RiskManager only needs get_sol_balance + pubkey."""
    pubkey = "FakeWalletPubkey1111111111111111111111111111"
    def __init__(self, balance: float = 1.0):
        self._balance = balance
    async def get_sol_balance(self) -> float:
        return self._balance
    async def _rpc(self, method, params):   # unused here; defensive stub
        return {"result": None}


class _FakeExecutor:
    """Minimal executor stub. Sell returns a deterministic sol_received
    so we don't need a real RPC."""
    def __init__(self, fixed_sol_received: float):
        self.fixed = fixed_sol_received
    async def sell(self, mint, amount, reason="exit", prebuilt_tx=None, price_history=None):
        return {
            "success":      True,
            "signature":    "SIG_" + str(time.time_ns()),
            "type":         "sell",
            "mint":         mint,
            "reason":       reason,
            "sol_received": self.fixed,
            "venue":        "pumpportal",
            "timestamp":    time.time(),
        }
    async def prebuild_sell_tx(self, mint):
        return None


@pytest.fixture
def rm(tmp_path, monkeypatch):
    # Isolate the trade DB to a temp path. `DEFAULT_DB_PATH` is captured
    # as a default arg in `get_trade_db`, so monkeypatching the module
    # attribute alone doesn't help — pass the path explicitly to seed
    # the singleton before anything else touches it.
    monkeypatch.setenv("PAPER_TRADING", "1")
    db_file = tmp_path / "trades.db"

    import logger.trade_db as tdb
    tdb._singleton = None
    isolated_db = tdb.get_trade_db(str(db_file))   # seeds singleton with the temp path

    wallet   = _FakeWallet(balance=1.0)
    executor = _FakeExecutor(fixed_sol_received=0.0)
    rm_obj   = RiskManager(wallet, executor)
    # Re-point _append_closed_trade at the isolated DB so the partial
    # records land somewhere we can assert on.
    monkeypatch.setattr(rm_obj, "_append_closed_trade", lambda rec: isolated_db.insert(rec))

    yield rm_obj, executor, isolated_db

    # Reset the singleton so other tests get a clean slate.
    tdb._singleton = None


def _make_pos(symbol: str = "TICK", sol_invested: float = 0.05, tokens_held: int = 10_000_000) -> Position:
    return Position(
        mint="MintAddr",
        symbol=symbol,
        creator="creator1",
        entry_price_sol=sol_invested / max(tokens_held, 1),
        entry_time=time.time() - 60,
        sol_invested=sol_invested,
        tokens_held=tokens_held,
        current_price=(sol_invested / max(tokens_held, 1)) * 2.0,  # +100% mark-to-market
        score=72,
    )


class TestPartialCloseRecording:
    def test_single_partial_writes_one_record(self, rm):
        rm_obj, _, db = rm
        pos = _make_pos(sol_invested=0.05, tokens_held=10_000_000)
        sell_result = {"sol_received": 0.02, "reason": "take_profit_75pct"}

        rm_obj._record_partial_close("MintAddr", pos, sell_fraction=0.15,
                                     sell_result=sell_result, level_id="tp_75")

        rows = db.load_all()
        assert len(rows) == 1
        rec = rows[0]
        # cost basis is the PRE-decrement sol_invested × sell_fraction
        assert rec["sol_invested"] == pytest.approx(0.05 * 0.15)
        assert rec["sol_received"] == pytest.approx(0.02)
        assert rec["pnl_sol"]      == pytest.approx(0.02 - 0.0075)
        assert rec["partial"] is True
        assert rec["reason"] == "take_profit_75pct"

    def test_partial_winning_increases_closed_trades_and_win_rate(self, rm):
        rm_obj, _, _ = rm
        pos = _make_pos()
        # Three TPs at increasing sell_fractions, each one a win
        for i, frac in enumerate([0.15, 0.25, 0.30]):
            rm_obj._record_partial_close(
                "MintAddr", pos, frac,
                {"sol_received": pos.sol_invested * frac * 2.0, "reason": f"tp_{i}"},
                level_id=f"tp_{i}",
            )
        stats = rm_obj.get_stats()
        assert stats["closed_trades"] == 3
        # all three wins (sol_received > sol_invested)
        assert stats["win_rate"] == pytest.approx(1.0)
        # total PnL = sum of (received - invested) per leg
        assert stats["total_pnl_sol"] > 0

    def test_zero_sol_received_records_as_loss(self, rm):
        """If the executor still returns sol_received=0 (sol-delta resolve
        failed), the partial gets recorded as a loss equal to its cost
        basis. Conservative: an unknown is treated as worst-case."""
        rm_obj, _, db = rm
        pos = _make_pos(sol_invested=0.10)
        rm_obj._record_partial_close(
            "MintAddr", pos, sell_fraction=0.25,
            sell_result={"sol_received": 0, "reason": "tp_300"},
            level_id="tp_300",
        )
        rec = db.load_all()[0]
        assert rec["sol_received"] == 0
        assert rec["pnl_sol"] == pytest.approx(-0.025)


class TestPositionStateUnchanged:
    """The partial-close record must not pop the position or mutate it.
    Local accounting is the caller's responsibility (in the TP loop)."""

    def test_position_still_held_after_partial(self, rm):
        rm_obj, _, _ = rm
        pos = _make_pos()
        rm_obj.positions["MintAddr"] = pos
        original_tokens   = pos.tokens_held
        original_invested = pos.sol_invested

        rm_obj._record_partial_close(
            "MintAddr", pos, 0.15,
            {"sol_received": 0.02, "reason": "tp_75"},
            level_id="tp_75",
        )
        assert "MintAddr" in rm_obj.positions
        assert pos.tokens_held   == original_tokens     # untouched
        assert pos.sol_invested  == original_invested   # untouched
