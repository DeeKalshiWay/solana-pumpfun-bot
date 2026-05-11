"""Regression tests for live-execution state recovery and circuit breakers.

These pin the contracts that were missing during the live wipeout:

  Bug C — emergency_stop_active / emergency_force_sell must persist
          across process restarts so a crash mid-emergency doesn't
          wipe the flag and let the bot resume trading at the
          depleted balance.

  Bug D — self.positions must persist across restarts so a crash
          doesn't lose track of in-flight positions (tokens stranded
          in the wallet with no bot bookkeeping).

  Bug E — force-sell must escalate after MAX_FORCE_SELL_ATTEMPTS
          and drop the zombie position from self.positions so the
          emergency loop can clear.

  Bug F — daily baseline must NOT reset at UTC midnight if equity
          is below 80% of the prior baseline; otherwise a drawdown
          straddling midnight rebases the daily-loss breaker.

  Bug G — stale prices (>STALE_PRICE_SEC old) must be excluded from
          the held-value calc; otherwise the drawdown trigger fires
          late during fast dumps.
"""
import datetime
import json
import time
from unittest.mock import patch

import pytest

import risk.manager as rm_mod
from risk.manager import (
    MAX_FORCE_SELL_ATTEMPTS,
    OPEN_POSITIONS_FILE,
    RISK_STATE_FILE,
    STALE_PRICE_SEC,
    Position,
    RiskManager,
)


class _FakeWallet:
    pubkey = "FakeWalletPubkey1111111111111111111111111111"
    def __init__(self, balance: float = 1.0):
        self._balance = balance
    async def get_sol_balance(self) -> float:
        return self._balance


class _FakeExecutor:
    def __init__(self, sell_succeeds: bool = True):
        self.sell_succeeds = sell_succeeds
        self.sell_calls = 0
    async def sell(self, mint, amount, reason="exit", prebuilt_tx=None, price_history=None):
        self.sell_calls += 1
        if not self.sell_succeeds:
            return {"success": False, "error": "pp_send_failed", "reason": reason}
        return {
            "success": True, "signature": "SIG_OK", "type": "sell",
            "mint": mint, "reason": reason, "sol_received": 0.05,
            "venue": "pumpportal", "timestamp": time.time(),
        }
    async def prebuild_sell_tx(self, mint):
        return None


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    """Redirect all on-disk state files into tmp_path so tests don't
    pollute each other or the real logs/ directory."""
    monkeypatch.setattr(rm_mod, "RISK_STATE_FILE",      str(tmp_path / "risk_state.json"))
    monkeypatch.setattr(rm_mod, "OPEN_POSITIONS_FILE",  str(tmp_path / "open_positions.json"))
    monkeypatch.setattr(rm_mod, "CLOSED_TRADES_FILE",   str(tmp_path / "closed_trades.jsonl"))
    monkeypatch.setattr(rm_mod, "SYMBOL_DEPLOYED_FILE", str(tmp_path / "symbol_deployed.json"))
    yield tmp_path


def _pos(mint="MintA", **overrides) -> Position:
    defaults = dict(
        mint=mint, symbol="TICK", creator="creator1",
        entry_price_sol=1e-9, entry_time=time.time(),
        sol_invested=0.05, tokens_held=10_000_000,
        current_price=1e-9, highest_price=1e-9,
        score=72,
    )
    defaults.update(overrides)
    return Position(**defaults)


# ── Bug C ────────────────────────────────────────────────────────────────────

class TestEmergencyStatePersistence:
    """emergency_stop_active and emergency_force_sell must round-trip
    through risk_state.json so a restart doesn't wipe them."""

    def test_save_then_load_preserves_flags(self, fresh_state):
        rm = RiskManager(_FakeWallet(), _FakeExecutor())
        rm.starting_sol_balance     = 0.7387
        rm.emergency_stop_active    = True
        rm.emergency_force_sell     = True
        rm._save_risk_state()

        # Confirm the file shape.
        with open(rm_mod.RISK_STATE_FILE) as f:
            data = json.load(f)
        assert data["emergency_stop_active"] is True
        assert data["emergency_force_sell"]  is True
        assert data["original_starting_sol"] == pytest.approx(0.7387)

        # Rehydrate: fresh RiskManager reads the same file.
        rm2 = RiskManager(_FakeWallet(), _FakeExecutor())
        rm2.starting_sol_balance = rm2._load_or_seed_starting_balance(0.0)
        assert rm2.emergency_stop_active is True
        assert rm2.emergency_force_sell  is True

    def test_fresh_install_defaults_to_false(self, fresh_state):
        """No risk_state.json on disk → both flags False."""
        rm = RiskManager(_FakeWallet(), _FakeExecutor())
        rm.starting_sol_balance = rm._load_or_seed_starting_balance(1.0)
        assert rm.emergency_stop_active is False
        assert rm.emergency_force_sell  is False


# ── Bug D ────────────────────────────────────────────────────────────────────

class TestOpenPositionsPersistence:
    def test_open_save_then_load_round_trip(self, fresh_state):
        rm = RiskManager(_FakeWallet(), _FakeExecutor())
        rm.positions["MintA"] = _pos("MintA", symbol="ALPHA",
                                     sol_invested=0.10, tokens_held=5_000_000)
        rm.positions["MintB"] = _pos("MintB", symbol="BETA",
                                     sol_invested=0.05, tokens_held=2_500_000)
        rm._save_open_positions()

        rm2 = RiskManager(_FakeWallet(), _FakeExecutor())
        rm2._load_open_positions()
        assert "MintA" in rm2.positions and "MintB" in rm2.positions
        assert rm2.positions["MintA"].symbol == "ALPHA"
        assert rm2.positions["MintA"].tokens_held == 5_000_000
        assert rm2.positions["MintB"].sol_invested == pytest.approx(0.05)

    def test_missing_file_is_no_op(self, fresh_state):
        rm = RiskManager(_FakeWallet(), _FakeExecutor())
        rm._load_open_positions()           # file doesn't exist
        assert rm.positions == {}           # no error, no state change

    def test_malformed_row_is_skipped(self, fresh_state):
        with open(rm_mod.OPEN_POSITIONS_FILE, "w") as f:
            json.dump({"positions": [
                {"mint": "MintGood", "symbol": "G", "creator": "x",
                 "entry_price_sol": 1, "entry_time": 0,
                 "sol_invested": 0.1, "tokens_held": 100},
                {"symbol": "NoMint"},           # missing mint → KeyError-caught
                {"mint": None},                 # mint=None → keyed as None, harmless
            ]}, f)
        rm = RiskManager(_FakeWallet(), _FakeExecutor())
        rm._load_open_positions()
        # Good one survives; the row missing 'mint' is dropped (no crash).
        assert "MintGood" in rm.positions
        assert "NoMint"   not in rm.positions

    def test_corrupt_json_file_is_no_op(self, fresh_state):
        with open(rm_mod.OPEN_POSITIONS_FILE, "w") as f:
            f.write("{not valid json")
        rm = RiskManager(_FakeWallet(), _FakeExecutor())
        rm._load_open_positions()
        assert rm.positions == {}


# ── Bug E ────────────────────────────────────────────────────────────────────

class TestForceSellEscalation:
    """A repeatedly-failing force-sell must abandon the zombie position
    after MAX_FORCE_SELL_ATTEMPTS so the emergency loop can clear."""

    @pytest.mark.asyncio
    async def test_position_dropped_after_max_attempts(self, fresh_state):
        executor = _FakeExecutor(sell_succeeds=False)
        rm = RiskManager(_FakeWallet(), executor)
        rm.starting_sol_balance = 1.0
        pos = _pos("MintZ")
        rm.positions["MintZ"] = pos

        # Drive force_sell up to (but not past) the threshold.
        for i in range(MAX_FORCE_SELL_ATTEMPTS - 1):
            await rm._force_sell("MintZ", "stop_loss")
            assert "MintZ" in rm.positions
            assert rm.positions["MintZ"].force_sell_attempts == i + 1

        # The Nth failure drops the position.
        await rm._force_sell("MintZ", "stop_loss")
        assert "MintZ" not in rm.positions
        assert executor.sell_calls == MAX_FORCE_SELL_ATTEMPTS


# ── Bug F ────────────────────────────────────────────────────────────────────

class TestDailyBaselineMidnight:
    """At UTC midnight, the daily baseline must NOT reset if equity
    is below 80% of the prior baseline."""

    @pytest.mark.asyncio
    async def test_midnight_does_not_reset_during_drawdown(self, fresh_state):
        rm = RiskManager(_FakeWallet(balance=0.5), _FakeExecutor())
        rm.starting_sol_balance     = 1.0
        rm.day_baseline_balance     = 1.0
        # Force the "date changed" branch by setting prior date to yesterday.
        rm.day_baseline_date        = datetime.date(2000, 1, 1)

        await rm._is_paused()   # invokes the baseline-reset block

        # Baseline should be preserved (0.5 is 50% of 1.0, below the 80% gate).
        assert rm.day_baseline_balance == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_midnight_resets_when_recovered(self, fresh_state):
        rm = RiskManager(_FakeWallet(balance=0.95), _FakeExecutor())
        rm.starting_sol_balance     = 1.0
        rm.day_baseline_balance     = 1.0
        rm.day_baseline_date        = datetime.date(2000, 1, 1)

        await rm._is_paused()
        # Equity 0.95 ≥ 80% of 1.0 → baseline rebases to current equity.
        assert rm.day_baseline_balance == pytest.approx(0.95)


# ── Bug G ────────────────────────────────────────────────────────────────────

class TestStalePriceEquityGuard:
    """held_value for the drawdown calc must exclude positions whose
    current_price hasn't been refreshed within STALE_PRICE_SEC."""

    def test_fresh_price_counts(self, fresh_state):
        rm = RiskManager(_FakeWallet(), _FakeExecutor())
        pos = _pos("MintFresh", current_price=1e-8, tokens_held=1_000_000)
        pos.price_updated_at = time.time()   # just now
        rm.positions["MintFresh"] = pos
        assert rm._held_value_for_drawdown() == pytest.approx(1e-8 * 1_000_000)

    def test_stale_price_is_excluded(self, fresh_state):
        rm = RiskManager(_FakeWallet(), _FakeExecutor())
        pos = _pos("MintStale", current_price=1e-8, tokens_held=1_000_000)
        pos.price_updated_at = time.time() - (STALE_PRICE_SEC + 2)
        rm.positions["MintStale"] = pos
        assert rm._held_value_for_drawdown() == 0

    def test_never_updated_price_is_excluded(self, fresh_state):
        rm = RiskManager(_FakeWallet(), _FakeExecutor())
        pos = _pos("MintBrandNew", current_price=1e-8, tokens_held=1_000_000)
        # price_updated_at left as the dataclass default (0)
        assert pos.price_updated_at == 0
        rm.positions["MintBrandNew"] = pos
        assert rm._held_value_for_drawdown() == 0

    def test_mixed_fresh_and_stale(self, fresh_state):
        rm = RiskManager(_FakeWallet(), _FakeExecutor())
        fresh = _pos("MintFresh", current_price=2e-8, tokens_held=1_000_000)
        fresh.price_updated_at = time.time()
        stale = _pos("MintStale", current_price=5e-8, tokens_held=1_000_000)
        stale.price_updated_at = time.time() - 60
        rm.positions["MintFresh"] = fresh
        rm.positions["MintStale"] = stale
        # Only the fresh one contributes.
        assert rm._held_value_for_drawdown() == pytest.approx(2e-8 * 1_000_000)
