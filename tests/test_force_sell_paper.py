"""Regression test for _force_sell paper-mode sol_received.

The bug: _force_sell unconditionally overrode `result["sol_received"]`
with on-chain tx-receipt parsing, then fell back to
`sol_after - sol_before` after an 8s sleep. The override was added so
the live PumpPortal close path could derive sol_received (PumpPortal
doesn't return it), but it stomped on paper mode too.

In paper:
  - signature is "PAPER_SELL_*" (fake) → _sol_delta_from_tx returns 0
  - sleep(8) fallback measures sol_after - sol_before, but with 3
    concurrent open positions firing buys during the wait, the
    measured delta gets clobbered by parallel wallet deductions
  - record_db ended up with sol_received=0 despite the paper executor
    having correctly credited the wallet

Fix: _force_sell now short-circuits when result["venue"] == "paper"
and trusts the executor's returned sol_received as-is. This test
pins the contract.
"""
import time

import pytest

from risk.manager import Position, RiskManager


class _FakeWallet:
    pubkey = "FakeWalletPubkey1111111111111111111111111111"

    def __init__(self, balance: float = 1.0):
        self._balance = balance

    async def get_sol_balance(self) -> float:
        return self._balance

    def deduct(self, amount: float):
        self._balance -= amount

    def credit(self, amount: float):
        self._balance += amount


class _FakePaperExec:
    """Mirrors PaperExecutor.sell return contract — venue="paper" and a
    populated sol_received. The sleep(8) trap is irrelevant here because
    the fix bypasses it entirely on the paper branch."""

    def __init__(self, fixed_sol_received: float, wallet):
        self.fixed = fixed_sol_received
        self.wallet = wallet

    async def sell(self, mint, amount, reason="exit", prebuilt_tx=None, price_history=None):
        # Mimic what PaperExecutor does: credit the wallet synchronously.
        self.wallet.credit(self.fixed)
        return {
            "success":      True,
            "signature":    f"PAPER_SELL_{mint[:8]}_{int(time.time())}",
            "type":         "sell",
            "mint":         mint,
            "reason":       reason,
            "sol_received": self.fixed,
            "venue":        "paper",
            "timestamp":    time.time(),
        }

    async def prebuild_sell_tx(self, mint):
        return None


class _FakeLiveExec(_FakePaperExec):
    """Live-side analogue: venue="pumpportal", sol_received present in
    result but conceptually the live close path would still derive it
    from the on-chain receipt. We want the fix to NOT short-circuit on
    this venue; that branch must still run _sol_delta_from_tx."""

    async def sell(self, mint, amount, reason="exit", prebuilt_tx=None, price_history=None):
        self.wallet.credit(self.fixed)
        return {
            "success":      True,
            "signature":    f"LIVE_{mint[:8]}_{int(time.time())}",
            "sol_received": self.fixed,
            "venue":        "pumpportal",
            "timestamp":    time.time(),
        }


@pytest.fixture
def rm_paper(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "1")
    db_file = tmp_path / "trades.db"
    import logger.trade_db as tdb
    tdb._singleton = None
    isolated_db = tdb.get_trade_db(str(db_file))

    wallet = _FakeWallet(balance=2.5)
    executor = _FakePaperExec(fixed_sol_received=0.0418, wallet=wallet)
    rm_obj = RiskManager(wallet, executor)
    monkeypatch.setattr(rm_obj, "_append_closed_trade", lambda rec: isolated_db.insert(rec))
    monkeypatch.setattr(rm_obj, "_save_open_positions", lambda: None)

    yield rm_obj, executor, isolated_db, wallet
    tdb._singleton = None


def _make_pos(mint: str = "MintForceSell", sol_invested: float = 0.05) -> Position:
    return Position(
        mint              = mint,
        symbol            = "TICK",
        creator           = "creator1",
        entry_price_sol   = sol_invested / 1_000_000,
        entry_time        = time.time() - 60,
        sol_invested      = sol_invested,
        tokens_held       = 1_000_000,
        current_price     = sol_invested / 1_000_000,
    )


class TestForceSellPaperSolReceived:
    @pytest.mark.asyncio
    async def test_paper_force_sell_records_executor_sol_received(self, rm_paper):
        rm_obj, _, db, _ = rm_paper
        pos = _make_pos()
        rm_obj.positions[pos.mint] = pos

        await rm_obj._force_sell(pos.mint, reason="no_movement")

        rows = db.load_all()
        assert len(rows) == 1
        rec = rows[0]
        # The executor returned 0.0418 — the trade-db row must reflect
        # that, NOT the 0 the old override would have produced for paper.
        assert rec["sol_received"] == pytest.approx(0.0418)
        # pnl_sol = sol_received - sol_invested = 0.0418 - 0.05 = -0.0082
        assert rec["pnl_sol"] == pytest.approx(0.0418 - 0.05)

    @pytest.mark.asyncio
    async def test_paper_force_sell_zero_received_still_recorded(self, rm_paper, monkeypatch):
        """Even when the paper executor honestly reports 0 (early-fail,
        no liquidity), the override must NOT silently invent a non-zero
        from a stale wallet delta. Trust the executor's value."""
        rm_obj, _, db, wallet = rm_paper
        # Reconfigure the executor to return 0 sol_received deterministically.
        rm_obj.executor.fixed = 0.0
        pos = _make_pos()
        rm_obj.positions[pos.mint] = pos

        await rm_obj._force_sell(pos.mint, reason="no_movement")

        rows = db.load_all()
        assert len(rows) == 1
        assert rows[0]["sol_received"] == 0.0


class TestForceSellLiveStillUsesTxReceipt:
    """Sanity: non-paper venue must NOT short-circuit. The fix should be
    surgically scoped to paper. We can't easily exercise _sol_delta_from_tx
    here, but we can confirm the venue branch is reached by mocking it out
    and asserting it was called."""

    @pytest.mark.asyncio
    async def test_live_path_calls_sol_delta(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_TRADING", "0")
        db_file = tmp_path / "trades.db"
        import logger.trade_db as tdb
        tdb._singleton = None
        isolated_db = tdb.get_trade_db(str(db_file))

        wallet = _FakeWallet(balance=2.5)
        executor = _FakeLiveExec(fixed_sol_received=0.07, wallet=wallet)
        rm_obj = RiskManager(wallet, executor)
        monkeypatch.setattr(rm_obj, "_append_closed_trade", lambda rec: isolated_db.insert(rec))
        monkeypatch.setattr(rm_obj, "_save_open_positions", lambda: None)

        calls = []
        async def fake_delta(sig):
            calls.append(sig)
            return 0.07
        monkeypatch.setattr(rm_obj, "_sol_delta_from_tx", fake_delta)

        pos = _make_pos()
        rm_obj.positions[pos.mint] = pos
        await rm_obj._force_sell(pos.mint, reason="no_movement")

        # Live path called the on-chain delta — paper short-circuit did
        # NOT fire on a pumpportal venue.
        assert len(calls) == 1
        rows = isolated_db.load_all()
        assert rows[0]["sol_received"] == pytest.approx(0.07)
        tdb._singleton = None
