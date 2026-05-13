"""Regression test for RiskManager.update_price sanity guard.

Live observation: a 0.05 SOL position recorded +30,000% PnL on a single
tick because the fallback price feed (Jupiter / DexScreener) quoted a
300× price for a metadata-less mint that migrated to Raydium within
seconds. The wallet math was honest; the input was junk.

A pump.fun bonding curve mathematically cannot move >3× before
migrating at 85 SOL, and even post-migration a single-tick >5× spike
is almost always corrupted. This guard rejects spikes >5× within 5s
of the prior update.

We only filter UP-spikes; legitimate rugs CAN drop >5× down in one tick.
"""
import time

import pytest

from risk.manager import Position, RiskManager


class _FakeWallet:
    pubkey = "FakeWalletPubkey1111111111111111111111111111"
    def __init__(self): pass
    async def get_sol_balance(self) -> float: return 1.0


class _FakeExec:
    pass


def _pos(price: float = 1e-9) -> Position:
    return Position(
        mint              = "M",
        symbol            = "TEST",
        creator           = "C",
        entry_price_sol   = price,
        entry_time        = time.time() - 60,
        sol_invested      = 0.05,
        tokens_held       = 1_000_000,
        current_price     = price,
        highest_price     = price,
        price_updated_at  = time.time() - 1.0,
    )


@pytest.fixture
def rm(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    return RiskManager(_FakeWallet(), _FakeExec())


class TestPriceSanityGuard:
    def test_normal_3x_move_accepted(self, rm):
        """Bonding-curve max ~3x. Realistic and must pass."""
        pos = _pos(price=1e-9)
        rm.positions["M"] = pos
        rm.update_price("M", 3e-9)
        assert pos.current_price == pytest.approx(3e-9)
        assert pos.highest_price == pytest.approx(3e-9)

    def test_4x_move_accepted_under_cap(self, rm):
        """4x is under the 5x cap — accept."""
        pos = _pos(price=1e-9)
        rm.positions["M"] = pos
        rm.update_price("M", 4e-9)
        assert pos.current_price == pytest.approx(4e-9)

    def test_300x_spike_rejected(self, rm):
        """The actual bug: 300x in one tick — rejected."""
        pos = _pos(price=1e-9)
        rm.positions["M"] = pos
        rm.update_price("M", 3e-7)   # 300x
        assert pos.current_price == pytest.approx(1e-9)   # unchanged
        assert pos.highest_price == pytest.approx(1e-9)   # unchanged

    def test_10x_spike_within_window_rejected(self, rm):
        """10x within 5s is over the cap — rejected."""
        pos = _pos(price=1e-9)
        pos.price_updated_at = time.time() - 1.0
        rm.positions["M"] = pos
        rm.update_price("M", 1e-8)   # 10x
        assert pos.current_price == pytest.approx(1e-9)

    def test_10x_after_window_accepted(self, rm):
        """If 5s+ has elapsed since last update, 10x is plausible
        (e.g. Raydium pool moved hard while we weren't polling)."""
        pos = _pos(price=1e-9)
        pos.price_updated_at = time.time() - 10.0   # 10s ago
        rm.positions["M"] = pos
        rm.update_price("M", 1e-8)   # 10x
        assert pos.current_price == pytest.approx(1e-8)

    def test_dump_not_filtered(self, rm):
        """Legit rug — price drops 99% in one tick. Must NOT filter
        (the guard is one-sided, only blocks UP-spikes)."""
        pos = _pos(price=1e-9)
        rm.positions["M"] = pos
        rm.update_price("M", 1e-11)   # -99%
        assert pos.current_price == pytest.approx(1e-11)

    def test_first_update_always_accepted(self, rm):
        """When current_price is 0 (defensive), any first update is
        accepted — there's no baseline to compare against."""
        pos = _pos(price=0)
        pos.price_updated_at = 0
        rm.positions["M"] = pos
        rm.update_price("M", 1e-9)
        assert pos.current_price == pytest.approx(1e-9)

    def test_no_pos_no_crash(self, rm):
        """update_price for an unknown mint is a no-op."""
        rm.update_price("UNKNOWN_MINT", 1e-9)
        # didn't raise; nothing else to assert.

    def test_price_history_only_appended_on_accept(self, rm):
        """Rejected updates must not pollute price_history (otherwise
        downstream PnL / momentum readers see fake highs)."""
        pos = _pos(price=1e-9)
        rm.positions["M"] = pos
        before = len(pos.price_history)
        rm.update_price("M", 1e-7)   # 100x — rejected
        assert len(pos.price_history) == before
        rm.update_price("M", 2e-9)   # 2x — accepted
        assert len(pos.price_history) == before + 1
