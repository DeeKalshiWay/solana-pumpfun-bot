"""Tests for detector.whale_tracker.

The tracker subscribes to PumpPortal trade events via the shared
monitor's pub/sub API. These tests build a WhaleTracker against an
isolated state file and drive trade events directly through _on_trade,
since the real WS is Cloudflare-blocked from CI/sandbox.
"""
import json
import time

import pytest

import detector.whale_tracker as wt_mod
from detector.whale_tracker import (
    WHALE_MIN_AVG_TRADE_SOL,
    WHALE_MIN_LIFETIME_SOL,
    WHALE_WINDOW_S,
    WhaleTracker,
)


@pytest.fixture
def wt(tmp_path, monkeypatch):
    """Isolated tracker with its own state file."""
    monkeypatch.setattr(wt_mod, "WHALE_STATE_FILE", str(tmp_path / "whales.json"))
    return WhaleTracker()


def _trade(addr: str, mint: str, sol: float, kind: str = "buy") -> dict:
    return {
        "txType":            kind,
        "traderPublicKey":   addr,
        "mint":              mint,
        "solAmount":         sol,
    }


class TestClassification:
    def test_under_threshold_is_not_whale(self, wt):
        # Single 0.1 SOL buy → 0.1 lifetime, avg = 0.1 → both below default
        # thresholds (10 SOL lifetime, 1.0 SOL avg).
        wt._on_create({"mint": "M1"})
        wt._on_trade(_trade("AddrSmall", "M1", 0.1))
        assert wt.is_whale("AddrSmall") is False

    def test_high_lifetime_volume_is_whale(self, wt):
        """One wallet doing many small trades that sum to ≥ WHALE_MIN_LIFETIME_SOL."""
        wt._on_create({"mint": "M1"})
        for _ in range(50):
            wt._on_trade(_trade("AddrBigVol", "M1", 0.25))   # 50 × 0.25 = 12.5 SOL
        assert wt.is_whale("AddrBigVol") is True

    def test_high_avg_trade_size_is_whale(self, wt):
        """One trade above WHALE_MIN_AVG_TRADE_SOL flips the classification
        even if lifetime is below the volume threshold."""
        wt._on_create({"mint": "M1"})
        wt._on_trade(_trade("AddrBigTicket", "M1", WHALE_MIN_AVG_TRADE_SOL + 0.1))
        assert wt.is_whale("AddrBigTicket") is True

    def test_sell_does_not_promote_to_whale(self, wt):
        """Only BUY volume counts toward whale classification. A whale
        is "real money is buying"; sells are a counter-signal."""
        wt._on_create({"mint": "M1"})
        wt._on_trade(_trade("AddrSells", "M1", WHALE_MIN_LIFETIME_SOL * 2, kind="sell"))
        assert wt.is_whale("AddrSells") is False


class TestWindowedBuyers:
    def test_buyer_inside_window_is_counted(self, wt):
        wt._on_create({"mint": "M1"})
        # Buy immediately — well inside WHALE_WINDOW_S
        wt._on_trade(_trade("AddrW", "M1", WHALE_MIN_AVG_TRADE_SOL + 0.5))
        assert "AddrW" in wt.whale_buyers_in_window("M1")

    def test_buyer_outside_window_is_excluded(self, wt, monkeypatch):
        wt._on_create({"mint": "M1"})
        # Backdate the mint creation so the buy lands past the window.
        wt._mint_created["M1"] = time.time() - (WHALE_WINDOW_S + 5)
        wt._on_trade(_trade("AddrLate", "M1", WHALE_MIN_AVG_TRADE_SOL + 0.5))
        assert wt.whale_buyers_in_window("M1") == []

    def test_non_whale_buyer_in_window_excluded(self, wt):
        wt._on_create({"mint": "M1"})
        wt._on_trade(_trade("AddrPleb", "M1", 0.05))   # under thresholds
        assert wt.whale_buyers_in_window("M1") == []

    def test_dedup_same_whale_buying_twice(self, wt):
        wt._on_create({"mint": "M1"})
        big = WHALE_MIN_AVG_TRADE_SOL + 1.0
        wt._on_trade(_trade("AddrW", "M1", big))
        wt._on_trade(_trade("AddrW", "M1", big))    # second buy from same whale
        assert wt.whale_buyers_in_window("M1") == ["AddrW"]

    def test_no_create_event_means_no_window(self, wt):
        """Trades on a mint we never saw the create event for don't
        contribute to whale_buyers_in_window — defensive against
        racey WS event ordering."""
        big = WHALE_MIN_AVG_TRADE_SOL + 1.0
        wt._on_trade(_trade("AddrW", "Mghost", big))
        assert wt.whale_buyers_in_window("Mghost") == []


class TestPersistence:
    def test_state_round_trips(self, tmp_path, monkeypatch):
        path = tmp_path / "whales.json"
        monkeypatch.setattr(wt_mod, "WHALE_STATE_FILE", str(path))

        wt1 = WhaleTracker()
        wt1._on_create({"mint": "M1"})
        for _ in range(50):
            wt1._on_trade(_trade("AddrW", "M1", 0.30))   # 15 SOL lifetime
        wt1._save()
        assert path.exists()

        # New instance loads from the same file.
        wt2 = WhaleTracker()
        assert wt2.is_whale("AddrW") is True

    def test_missing_file_starts_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wt_mod, "WHALE_STATE_FILE", str(tmp_path / "absent.json"))
        wt = WhaleTracker()
        assert wt._wallets == {}

    def test_corrupt_file_starts_empty(self, tmp_path, monkeypatch):
        path = tmp_path / "whales.json"
        path.write_text("{not json")
        monkeypatch.setattr(wt_mod, "WHALE_STATE_FILE", str(path))
        wt = WhaleTracker()
        assert wt._wallets == {}


class TestEviction:
    def test_lru_drops_oldest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wt_mod, "WHALE_STATE_FILE",      str(tmp_path / "w.json"))
        monkeypatch.setattr(wt_mod, "MAX_TRACKED_WALLETS",    5)
        wt = WhaleTracker()
        wt._on_create({"mint": "M1"})
        # 7 distinct wallets — LRU should leave the 5 most recent
        for i in range(7):
            wt._on_trade(_trade(f"Addr{i}", "M1", 0.01))
        assert len(wt._wallets) == 5
        # The first two should be gone
        assert "Addr0" not in wt._wallets
        assert "Addr1" not in wt._wallets
        assert "Addr6" in wt._wallets


class TestStats:
    def test_stats_shape(self, wt):
        wt._on_create({"mint": "M1"})
        big = WHALE_MIN_AVG_TRADE_SOL + 1.0
        wt._on_trade(_trade("AddrW", "M1", big))
        s = wt.stats()
        assert s["tracked_wallets"] >= 1
        assert s["whale_count"] >= 1
        assert isinstance(s["top_whales"], list)
        assert s["top_whales"][0]["buys_sol"] > 0


class TestVolumeMetric:
    def test_whale_buy_volume_aggregates(self, wt):
        wt._on_create({"mint": "M1"})
        # Two whales buy in the window
        wt._on_trade(_trade("AddrA", "M1", 2.0))
        wt._on_trade(_trade("AddrB", "M1", 3.0))
        vol = wt.whale_buy_volume("M1")
        # Each whale contributes its avg trade size (only 1 trade each so avg = trade size)
        assert vol == pytest.approx(5.0)

    def test_whale_buy_volume_empty_for_unknown_mint(self, wt):
        assert wt.whale_buy_volume("Mnone") == 0
