"""Regression test for trader.executor.TradeExecutor sell kwargs.

Live bug observation: bot had 5 stuck positions on Windows because every
force-sell raised:

  TypeError: TradeExecutor.sell() got an unexpected keyword argument 'price_history'

PR #13 added `price_history` to PaperExecutor.sell and PumpPortalExecutor.sell
but the OUTER TradeExecutor router (executor.py) was never updated. risk_manager
calls the outer router. Every early_rug / no_movement / momentum_stall /
time_exit / trailing_stop force-sell died at the call site with no on-chain
action, leaving real positions stranded.

These tests pin the contract for the outer router:
  - sell() accepts `price_history` kwarg without TypeError
  - sell() accepts `prebuilt_tx` kwarg without TypeError
  - Both kwargs forward to PumpPortalExecutor (which also accepts them)
"""
import inspect
from unittest.mock import AsyncMock

import pytest

from trader.executor import TradeExecutor


class _FakeKeypair:
    """Minimal stand-in for solders.Keypair. TradeExecutor only needs a
    .pubkey() method for boot; sell() doesn't touch it."""
    def pubkey(self):
        return "FakeWalletPubkey1111111111111111111111111111"


@pytest.fixture
def executor():
    """Build a TradeExecutor with a mocked PumpPortalExecutor so we don't
    touch the chain. We only care about the signature shape and the
    forwarding behavior."""
    ex = TradeExecutor(_FakeKeypair())
    ex.pumpportal = AsyncMock()
    # PumpPortal returns a successful sell-shaped dict by default.
    ex.pumpportal.sell.return_value = {
        "success": True, "venue": "pumpportal", "signature": "SIG_OK",
        "sol_received": 0.05,
    }
    return ex


class TestSellSignatureAcceptsKwargs:
    """The TypeError that stranded live positions was a signature mismatch.
    Lock in the kwargs explicitly so a future signature change can't
    silently regress."""

    def test_price_history_in_signature(self):
        sig = inspect.signature(TradeExecutor.sell)
        assert "price_history" in sig.parameters, (
            "TradeExecutor.sell must accept price_history kwarg — "
            "risk_manager._force_sell passes it on every force-sell exit"
        )

    def test_prebuilt_tx_in_signature(self):
        sig = inspect.signature(TradeExecutor.sell)
        assert "prebuilt_tx" in sig.parameters, (
            "TradeExecutor.sell must accept prebuilt_tx kwarg — "
            "risk_manager._force_sell passes it for the prebuild fast-path"
        )


class TestSellForwardsKwargs:
    """Kwargs aren't just accepted; they're forwarded to PumpPortalExecutor
    where the paper-mode equivalent reads price_history for latency math."""

    @pytest.mark.asyncio
    async def test_pct_string_forwards_price_history(self, executor):
        await executor.sell("MintAddr", "100%", reason="time_exit",
                            price_history=[(1.0, 0.0, 2e-9), (2.0, 5.0, 2.1e-9)])
        executor.pumpportal.sell.assert_awaited_once()
        _, kwargs = executor.pumpportal.sell.call_args
        assert "price_history" in kwargs
        assert kwargs["price_history"] is not None

    @pytest.mark.asyncio
    async def test_prebuilt_tx_forwards_price_history(self, executor):
        await executor.sell("MintAddr", "100%", reason="trailing_stop",
                            prebuilt_tx=b"prebuilt_bytes",
                            price_history=[(1.0, 0.0, 2e-9)])
        executor.pumpportal.sell.assert_awaited_once()
        _, kwargs = executor.pumpportal.sell.call_args
        assert kwargs["prebuilt_tx"] == b"prebuilt_bytes"
        assert kwargs["price_history"] is not None

    @pytest.mark.asyncio
    async def test_no_kwargs_still_works(self, executor):
        """The bug only manifested when risk_manager passed the kwargs.
        Verify the no-kwargs path still works so legacy callers aren't
        broken by the signature change."""
        await executor.sell("MintAddr", "100%", reason="exit")
        executor.pumpportal.sell.assert_awaited_once()


class TestRiskManagerCanCallSell:
    """End-to-end signature check: the exact call risk_manager._force_sell
    makes must not raise TypeError."""

    @pytest.mark.asyncio
    async def test_force_sell_call_signature(self, executor):
        # Mirrors risk/manager.py:1018 — the line that was crashing.
        result = await executor.sell(
            "MintAddr", "100%",
            reason="time_exit",
            prebuilt_tx=None,
            price_history=[(1.0, 0.0, 2e-9)],
        )
        assert result["success"] is True
