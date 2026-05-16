"""Regression test for asymmetric slippage config.

Live observation 2026-05-16: a single global SLIPPAGE_BPS=1500 (15%) was
too tight on emergency sells. pump.fun's bonding-curve sell rejects with
`Custom: 6001` when curve moves more than 15% against us between tx
send and confirm. Five live positions got stranded — every force-sell
reverted on chain.

Fix: split into BUY_SLIPPAGE_BPS (kept tight, default 1500) and
SELL_SLIPPAGE_BPS (loosened, default 4000). Tight buys avoid overpaying
into pumps; wide sells guarantee emergency exits complete.

These tests pin the contract:
  - Both knobs exist as separate config values
  - SELL is wider than BUY by default
  - Backward-compat: if only SLIPPAGE_BPS is set in env, BUY follows it,
    SELL uses its own default
"""
import importlib


class TestAsymmetricSlippageConfig:
    def test_buy_slippage_exists(self):
        from config import BUY_SLIPPAGE_BPS
        assert isinstance(BUY_SLIPPAGE_BPS, int)
        assert BUY_SLIPPAGE_BPS >= 0

    def test_sell_slippage_exists(self):
        from config import SELL_SLIPPAGE_BPS
        assert isinstance(SELL_SLIPPAGE_BPS, int)
        assert SELL_SLIPPAGE_BPS >= 0

    def test_sell_wider_than_buy_by_default(self):
        """The whole point of the split is that emergency sells need more
        room than entries. If sell ever becomes tighter than buy, that's
        a config regression."""
        from config import BUY_SLIPPAGE_BPS, SELL_SLIPPAGE_BPS
        assert SELL_SLIPPAGE_BPS > BUY_SLIPPAGE_BPS, (
            f"SELL_SLIPPAGE_BPS ({SELL_SLIPPAGE_BPS}) must be > "
            f"BUY_SLIPPAGE_BPS ({BUY_SLIPPAGE_BPS}) — emergency sells need "
            f"more room than entries"
        )

    def test_legacy_slippage_bps_still_exists(self):
        """Backward compat: existing references to SLIPPAGE_BPS shouldn't
        break. It's kept as a fallback / informational value."""
        from config import SLIPPAGE_BPS
        assert isinstance(SLIPPAGE_BPS, int)


class TestEnvOverrides:
    """Env var overrides honor the same precedence as the rest of the
    config system."""

    def test_buy_slippage_env_override(self, monkeypatch):
        monkeypatch.setenv("BUY_SLIPPAGE_BPS", "777")
        import config
        importlib.reload(config)
        assert config.BUY_SLIPPAGE_BPS == 777

    def test_sell_slippage_env_override(self, monkeypatch):
        monkeypatch.setenv("SELL_SLIPPAGE_BPS", "9000")
        import config
        importlib.reload(config)
        assert config.SELL_SLIPPAGE_BPS == 9000

    def test_buy_falls_back_to_global_slippage(self, monkeypatch):
        """If BUY_SLIPPAGE_BPS is not set but SLIPPAGE_BPS is, buy
        slippage follows the global setting — preserves the old single-knob
        behavior for users who haven't migrated to asymmetric."""
        monkeypatch.delenv("BUY_SLIPPAGE_BPS", raising=False)
        monkeypatch.setenv("SLIPPAGE_BPS", "2222")
        import config
        importlib.reload(config)
        assert config.BUY_SLIPPAGE_BPS == 2222
