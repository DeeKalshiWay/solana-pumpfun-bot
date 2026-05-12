"""Tests for the JITO_REGION → regional Jito URL helper in config.py.

These pin the contract that the regional URL prepends to RPC_URLS when
JITO_REGION is set, and stays out of the way when unset / unknown.

The helper sits inside config.py (module-level), so we test the pure
function plus the integration via env-var monkeypatching + module reload.
"""
import importlib

import pytest


def _reload_config():
    """Re-import config so module-level globals (RPC_URLS) re-evaluate
    against whatever env vars the test set."""
    import config
    return importlib.reload(config)


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """Strip any pre-existing config env vars from the test environment
    so a stray JITO_REGION on the dev's shell doesn't poison the test."""
    for var in ("JITO_REGION", "EXTRA_RPC_URLS", "RPC_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    yield


class TestJitoUrlBuilder:
    def test_known_region_returns_pinned_url(self):
        from config import _jito_url_for_region
        url = _jito_url_for_region("ny")
        assert url == "https://ny.mainnet.block-engine.jito.wtf/api/v1/transactions"

    def test_case_insensitive(self):
        from config import _jito_url_for_region
        assert _jito_url_for_region("NY") == _jito_url_for_region("ny")
        assert _jito_url_for_region("  Frankfurt ") == _jito_url_for_region("frankfurt")

    def test_unknown_region_returns_none(self):
        from config import _jito_url_for_region
        assert _jito_url_for_region("mars") is None

    def test_empty_returns_none(self):
        from config import _jito_url_for_region
        assert _jito_url_for_region("") is None
        assert _jito_url_for_region(None) is None

    @pytest.mark.parametrize("region", [
        "ny", "slc", "frankfurt", "amsterdam",
        "dublin", "london", "tokyo", "singapore",
    ])
    def test_every_documented_region_resolves(self, region):
        from config import _jito_url_for_region
        url = _jito_url_for_region(region)
        assert url is not None
        assert region + ".mainnet.block-engine.jito.wtf" in url
        assert url.endswith("/api/v1/transactions")


class TestRpcUrlsIntegration:
    def test_jito_region_prepends_to_rpc_urls(self, monkeypatch):
        monkeypatch.setenv("JITO_REGION", "ny")
        cfg = _reload_config()
        assert cfg.RPC_URLS[0] == (
            "https://ny.mainnet.block-engine.jito.wtf/api/v1/transactions"
        )
        # Base RPC_URL still in the list.
        assert "https://api.mainnet-beta.solana.com" in cfg.RPC_URLS

    def test_no_jito_region_leaves_rpc_urls_alone(self, monkeypatch):
        # JITO_REGION already stripped by autouse fixture.
        cfg = _reload_config()
        for u in cfg.RPC_URLS:
            assert "block-engine.jito.wtf" not in u

    def test_unknown_jito_region_does_not_crash(self, monkeypatch):
        monkeypatch.setenv("JITO_REGION", "nowhere")
        cfg = _reload_config()
        for u in cfg.RPC_URLS:
            assert "block-engine.jito.wtf" not in u

    def test_dedup_against_extra_rpc_urls(self, monkeypatch):
        """If the operator manually set the NY URL in EXTRA_RPC_URLS AND
        also set JITO_REGION=ny, we should only see it once."""
        monkeypatch.setenv("JITO_REGION", "ny")
        monkeypatch.setenv(
            "EXTRA_RPC_URLS",
            "https://ny.mainnet.block-engine.jito.wtf/api/v1/transactions",
        )
        cfg = _reload_config()
        ny = "https://ny.mainnet.block-engine.jito.wtf/api/v1/transactions"
        assert cfg.RPC_URLS.count(ny) == 1


class TestGenericJitoWarning:
    """If the user has the generic catch-all URL in their EXTRA_RPC_URLS,
    config-load emits a warning telling them to pick a region. Verifies
    we don't break their setup — just nudge."""

    def test_generic_url_emits_warning(self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv(
            "EXTRA_RPC_URLS",
            "https://mainnet.block-engine.jito.wtf/api/v1/transactions",
        )
        with caplog.at_level(logging.WARNING):
            cfg = _reload_config()
        assert any(
            "Generic Jito URL detected" in r.message for r in caplog.records
        )
        # URL stays in the list — we don't strip it, just warn.
        assert any("mainnet.block-engine.jito.wtf" in u for u in cfg.RPC_URLS)

    def test_regional_url_does_not_warn(self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv(
            "EXTRA_RPC_URLS",
            "https://ny.mainnet.block-engine.jito.wtf/api/v1/transactions",
        )
        with caplog.at_level(logging.WARNING):
            _reload_config()
        assert not any(
            "Generic Jito URL detected" in r.message for r in caplog.records
        )
