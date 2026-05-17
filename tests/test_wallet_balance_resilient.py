"""Regression tests for SolanaWallet.get_sol_balance() RPC resilience.

Live bug observation: a single bad RPC response (401 invalid key,
timeout, rate-limit, malformed body) made get_sol_balance return 0.
risk_manager then compared 0 vs starting_sol_balance and computed
−100% drawdown → emergency stop → bot "stopped buying" with full
wallet still on chain.

Fix: cache the last successful read and return it on subsequent
failures. Bot stays trading through transient blips.

These tests pin the behavior:
  - First successful call caches the value
  - Subsequent RPC error / network error / unexpected shape returns
    the cached value, NOT 0
  - First-ever call with broken RPC still returns 0 (graceful)
  - Successful read after a failure updates the cache
"""
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from trader.wallet import SolanaWallet


class _FakeKeypair:
    """Minimal solders.Keypair stand-in. SolanaWallet.__init__ calls
    _load_keypair() which needs PRIVATE_KEY env. We bypass init by
    constructing the object manually and setting the fields we need."""
    def pubkey(self):
        # solders.Pubkey.__str__ returns base58. We don't need a real one.
        class _PK:
            def __str__(self): return "FakeWalletPubkey1111111111111111111111111111"
        return _PK()


@pytest.fixture
def wallet():
    """Build a SolanaWallet with all init side effects stubbed out.
    We're testing get_sol_balance in isolation."""
    w = SolanaWallet.__new__(SolanaWallet)
    w.keypair = _FakeKeypair()
    w.pubkey  = w.keypair.pubkey()
    w.session = None
    w._last_known_balance = None
    w._last_balance_read_ts = 0.0
    return w


class TestBalanceCaching:
    @pytest.mark.asyncio
    async def test_first_successful_read_caches(self, wallet):
        wallet._rpc = AsyncMock(return_value={"result": {"value": 3_500_000_000}})
        bal = await wallet.get_sol_balance()
        assert bal == pytest.approx(3.5)
        assert wallet._last_known_balance == pytest.approx(3.5)

    @pytest.mark.asyncio
    async def test_rpc_error_response_returns_cache(self, wallet):
        """The 401 invalid api key case — returns a JSON body with `error`
        field but no `result`. Must NOT return 0; must return cached value."""
        # Seed cache
        wallet._last_known_balance = 3.5
        wallet._rpc = AsyncMock(return_value={"error": {"code": -32401, "message": "invalid api key"}})
        bal = await wallet.get_sol_balance()
        assert bal == pytest.approx(3.5), (
            "401 from Helius must return cached balance, not 0 — "
            "otherwise risk_manager computes a false −100% drawdown"
        )

    @pytest.mark.asyncio
    async def test_network_timeout_returns_cache(self, wallet):
        wallet._last_known_balance = 3.5
        async def boom(*_a, **_kw):
            import asyncio
            raise TimeoutError("simulated")
        wallet._rpc = boom
        bal = await wallet.get_sol_balance()
        assert bal == pytest.approx(3.5)

    @pytest.mark.asyncio
    async def test_client_error_returns_cache(self, wallet):
        wallet._last_known_balance = 3.5
        async def boom(*_a, **_kw):
            raise aiohttp.ClientError("connection refused")
        wallet._rpc = boom
        bal = await wallet.get_sol_balance()
        assert bal == pytest.approx(3.5)

    @pytest.mark.asyncio
    async def test_unexpected_shape_returns_cache(self, wallet):
        """RPC returns {} or {"result": "weird string"} — defensive case."""
        wallet._last_known_balance = 3.5
        wallet._rpc = AsyncMock(return_value={"result": "not a dict"})
        bal = await wallet.get_sol_balance()
        assert bal == pytest.approx(3.5)

    @pytest.mark.asyncio
    async def test_recovery_updates_cache(self, wallet):
        """RPC fails, then recovers. Cache should update to new value."""
        wallet._rpc = AsyncMock(return_value={"result": {"value": 3_500_000_000}})
        await wallet.get_sol_balance()
        # RPC fails
        wallet._rpc = AsyncMock(return_value={"error": {"message": "rate limit"}})
        bal_during_fail = await wallet.get_sol_balance()
        assert bal_during_fail == pytest.approx(3.5)
        # RPC recovers with new balance
        wallet._rpc = AsyncMock(return_value={"result": {"value": 4_000_000_000}})
        bal_after = await wallet.get_sol_balance()
        assert bal_after == pytest.approx(4.0)
        assert wallet._last_known_balance == pytest.approx(4.0)

    @pytest.mark.asyncio
    async def test_first_call_failure_returns_zero(self, wallet):
        """Edge case: bot just booted, no cache, RPC is dead. Return 0
        but log loudly. Caller should treat boot-time 0 specially —
        but the wallet itself can't crash, so 0 is the only fallback."""
        assert wallet._last_known_balance is None
        wallet._rpc = AsyncMock(return_value={"error": {"message": "401"}})
        bal = await wallet.get_sol_balance()
        assert bal == 0.0
