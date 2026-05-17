"""Regression test for the momentum_stall + TP1 gate.

Live observation 2026-05-17: a token pumped to +5.1%, stalled flat,
momentum_stall fired, sell came back at −1.5% net after buy+sell
slippage and fees. The +5% paper gain wasn't enough to cover round-trip
costs. Repeated occurrences mean the rule was reliably booking small
losers on top of true rugs.

Fix: gate momentum_stall on len(pos.tp_levels_hit) > 0 — i.e., never
stall-exit until at least TP1 has fired. Once we've banked some,
remaining size is house money and stalling out is fine.

These tests pin the contract:
  - No TPs hit + flat in profit → momentum_stall MUST NOT fire
  - TP1 hit + flat in profit    → momentum_stall MUST fire
  - Other exits (stop loss, early rug) unaffected by the gate
"""
import time
from unittest.mock import AsyncMock

import pytest

from risk.manager import Position, RiskManager


class _FakeWallet:
    pubkey = "FakeWalletPubkey1111111111111111111111111111"
    async def get_sol_balance(self) -> float:
        return 1.0


class _FakeExecutor:
    async def sell(self, mint, amount, reason="exit", prebuilt_tx=None, price_history=None):
        return {"success": True, "signature": "SIG", "sol_received": 0, "reason": reason}
    async def prebuild_sell_tx(self, mint):
        return None


@pytest.fixture
def rm(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "1")
    import logger.trade_db as tdb
    tdb._singleton = None
    tdb.get_trade_db(str(tmp_path / "trades.db"))
    rm_obj = RiskManager(_FakeWallet(), _FakeExecutor())
    rm_obj._append_closed_trade = lambda rec: None
    yield rm_obj
    tdb._singleton = None


def _stalled_pos(profit_pct: float, tp_hits: list) -> Position:
    """Build a position currently at +profit_pct, flat for 90s
    (longer than the default 60s stall window)."""
    entry_price = 1e-7
    current = entry_price * (1 + profit_pct / 100)
    pos = Position(
        mint="MintAddr",
        symbol="TICK",
        creator="creator1",
        entry_price_sol=entry_price,
        entry_time=time.time() - 180,
        sol_invested=0.05,
        tokens_held=10_000_000,
        current_price=current,
        highest_price=current,
        score=72,
    )
    pos.tp_levels_hit = list(tp_hits)
    # 6 samples over the last 90s, all flat at +profit_pct (spread = 0)
    now = time.time()
    pos.price_history = [(now - i * 15, profit_pct) for i in range(6)]
    return pos


class TestMomentumStallGate:
    @pytest.mark.asyncio
    async def test_no_tp_hit_blocks_stall_exit(self, rm):
        """+5% flat, no TPs hit → must NOT trigger momentum_stall.
        This is the live bug: small pumps that stall before TP1 were
        getting booked as net losers after slippage."""
        pos = _stalled_pos(profit_pct=5.5, tp_hits=[])
        rm.positions["MintAddr"] = pos
        rm._force_sell = AsyncMock()
        await rm._check_position("MintAddr")
        # If momentum_stall fired, _force_sell would have been called
        # with that reason. Stop loss / early rug should not fire on
        # a +5% position, so the call list should be empty.
        called_reasons = [c.args[1] for c in rm._force_sell.call_args_list]
        assert "momentum_stall" not in called_reasons, (
            f"momentum_stall fired before TP1 — that's the live bug. "
            f"reasons fired: {called_reasons}"
        )

    @pytest.mark.asyncio
    async def test_tp1_hit_allows_stall_exit(self, rm):
        """After TP1 (or any TP) has banked, remaining size is house
        money; momentum_stall should fire normally to lock the rest."""
        pos = _stalled_pos(profit_pct=15.0, tp_hits=["tp_12.5"])
        rm.positions["MintAddr"] = pos
        rm._force_sell = AsyncMock()
        await rm._check_position("MintAddr")
        called_reasons = [c.args[1] for c in rm._force_sell.call_args_list]
        assert "momentum_stall" in called_reasons, (
            f"momentum_stall must still fire post-TP1; "
            f"reasons fired: {called_reasons}"
        )

    @pytest.mark.asyncio
    async def test_stop_loss_unaffected_by_gate(self, rm):
        """The gate is specific to momentum_stall. A real stop-loss must
        still fire regardless of whether any TPs have hit."""
        pos = _stalled_pos(profit_pct=-20.0, tp_hits=[])
        # Push entry_time back so early_rug window has passed and we hit
        # the real stop-loss path, not the early-rug path.
        pos.entry_time = time.time() - 3600
        rm.positions["MintAddr"] = pos
        rm._force_sell = AsyncMock()
        await rm._check_position("MintAddr")
        called_reasons = [c.args[1] for c in rm._force_sell.call_args_list]
        assert any("stop_loss" in r for r in called_reasons), (
            f"stop-loss must fire on a -20% position even with no TPs hit; "
            f"reasons fired: {called_reasons}"
        )
