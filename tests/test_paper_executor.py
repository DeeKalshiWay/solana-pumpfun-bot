"""Regression tests for trader.paper_executor.

The bug being pinned: risk_manager passes "15%" / "50%" / "100%" strings
to executor.sell() — the live PumpPortal HTTP API accepts those, and the
risk manager intentionally uses them so the same call works in both
modes. Paper used to silently zero out the percent string and early-
return sol_received=0, so every TP partial got recorded as a 100% loss
of the cost basis regardless of the actual price move. Dashboard showed
PnL% +1000% with PnL SOL strongly negative. These tests pin the new
behavior:

  - "100%" resolves to the full tracked holdings
  - "50%" resolves to half, twice resolves the full position
  - On a +10x price move, a 100% sell returns ~10x the SOL invested
    (minus modeled friction)
  - Token bookkeeping decrements correctly after partial fills
  - Pure-int amounts still work (legacy callers / tests)
"""
import asyncio
import os
import random

import pytest

# Disable tx-fail randomness for deterministic assertions. The flag is
# baked into module-level constants at import time, so we set the env
# var before importing.
os.environ.setdefault("REALISTIC_PAPER_SIM", "0")

from trader.paper_executor import PaperExecutor  # noqa: E402


class _FakeWallet:
    def __init__(self, balance: float = 10.0):
        self.balance = balance
    def deduct(self, amount: float):
        self.balance -= amount
    def credit(self, amount: float):
        self.balance += amount


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _token(mint: str = "MINT_TEST", v_sol: float = 30.0, v_tokens: float = 1_000_000.0):
    """Build a token dict matching what risk_manager passes to executor.buy."""
    return {
        "symbol":              "TEST",
        "v_sol_in_bonding":    v_sol,
        "v_tokens_in_bonding": v_tokens,
        "market_cap_sol":      v_sol,
    }


@pytest.fixture(autouse=True)
def _seed_rng():
    """Pin RNG so legacy-mode (no tx-fail) tests are 100% deterministic."""
    random.seed(0)


class TestResolveTokenAmount:
    def test_integer_passthrough(self):
        ex = PaperExecutor(_FakeWallet())
        assert ex._resolve_token_amount("X", 12345) == 12345

    def test_int_string_passthrough(self):
        ex = PaperExecutor(_FakeWallet())
        # Tracked holdings irrelevant for pure int strings.
        assert ex._resolve_token_amount("X", "12345") == 12345

    def test_percent_with_no_holdings_returns_zero(self):
        ex = PaperExecutor(_FakeWallet())
        assert ex._resolve_token_amount("UNKNOWN", "100%") == 0

    def test_percent_100_full_holdings(self):
        ex = PaperExecutor(_FakeWallet())
        ex._tokens_held["M"] = 1_000_000
        assert ex._resolve_token_amount("M", "100%") == 1_000_000

    def test_percent_50_half_holdings(self):
        ex = PaperExecutor(_FakeWallet())
        ex._tokens_held["M"] = 1_000_000
        assert ex._resolve_token_amount("M", "50%") == 500_000

    def test_percent_15_resolves(self):
        ex = PaperExecutor(_FakeWallet())
        ex._tokens_held["M"] = 1_000_000
        assert ex._resolve_token_amount("M", "15%") == 150_000

    def test_percent_with_whitespace(self):
        ex = PaperExecutor(_FakeWallet())
        ex._tokens_held["M"] = 1_000_000
        assert ex._resolve_token_amount("M", "  100%  ") == 1_000_000

    def test_garbage_string_returns_zero(self):
        ex = PaperExecutor(_FakeWallet())
        ex._tokens_held["M"] = 1_000_000
        assert ex._resolve_token_amount("M", "not a number") == 0

    def test_negative_resolves_to_zero(self):
        ex = PaperExecutor(_FakeWallet())
        assert ex._resolve_token_amount("M", -5) == 0


class TestBuyTracksTokens:
    def test_buy_populates_tokens_held(self):
        ex = PaperExecutor(_FakeWallet(balance=10.0))
        result = _run(ex.buy("MINT_A", 0.1, _token()))
        assert result["success"]
        assert ex._tokens_held["MINT_A"] == result["tokens_expected"]
        assert ex._tokens_held["MINT_A"] > 0


class TestPercentSellResolvesToRealSolReceived:
    """The headline bug: a price-moving position sold with a percent string
    must return non-zero sol_received."""

    def test_100pct_sell_at_entry_price_returns_invested(self):
        wallet = _FakeWallet(balance=10.0)
        ex = PaperExecutor(wallet)
        _run(ex.buy("MINT_A", 0.1, _token()))
        # Price unchanged — sol_received should be ~0.1 SOL (minus friction).
        result = _run(ex.sell("MINT_A", "100%", reason="exit"))
        assert result["success"]
        # Legacy-mode friction is 1.5% slippage; sol_received should land
        # in a sensible range > 0.05 SOL of the 0.1 invested. The exact
        # number is asserted below in the price-moving test.
        assert result["sol_received"] > 0
        assert ex._tokens_held.get("MINT_A", 0) == 0

    def test_100pct_sell_after_10x_returns_roughly_10x(self):
        wallet = _FakeWallet(balance=10.0)
        ex = PaperExecutor(wallet)
        buy = _run(ex.buy("MINT_B", 0.1, _token()))
        entry_price = ex._prices["MINT_B"]
        # Walk price to 10x via the synthetic-mover's contract: update_price.
        ex.update_price("MINT_B", entry_price * 10)
        result = _run(ex.sell("MINT_B", "100%", reason="take_profit_900pct"))
        assert result["success"]
        # 0.1 SOL × 10x = 1.0 SOL gross; legacy mode 1.5% slip → ~0.985 SOL.
        # Allow wide bounds so the assertion survives small math changes.
        assert 0.7 < result["sol_received"] < 1.05, result["sol_received"]
        # Bug pin: must be strongly positive, not zero (the old behavior).
        assert result["sol_received"] > buy["sol_spent"] * 5

    def test_50pct_then_100pct_drains_in_two_legs(self):
        wallet = _FakeWallet(balance=10.0)
        ex = PaperExecutor(wallet)
        buy = _run(ex.buy("MINT_C", 0.1, _token()))
        original = ex._tokens_held["MINT_C"]
        assert original == buy["tokens_expected"]

        leg1 = _run(ex.sell("MINT_C", "50%", reason="take_profit_50pct"))
        assert leg1["success"]
        assert leg1["sol_received"] > 0
        # After 50% sell, half the tokens remain.
        assert ex._tokens_held["MINT_C"] == original - (original // 2)

        leg2 = _run(ex.sell("MINT_C", "100%", reason="exit"))
        assert leg2["success"]
        assert leg2["sol_received"] > 0
        # Full drain — entry tracking cleared.
        assert "MINT_C" not in ex._tokens_held
