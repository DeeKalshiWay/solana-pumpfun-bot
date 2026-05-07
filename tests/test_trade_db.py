"""Tests for logger/trade_db.py against an in-memory sqlite db."""
from collections.abc import Iterator

import pytest

from logger.trade_db import TradeDB


@pytest.fixture
def db() -> Iterator[TradeDB]:
    d = TradeDB(":memory:")
    yield d
    d.close()


def _trade(**overrides):
    base = {
        "mint": "MintAAA",
        "symbol": "TICK",
        "creator": "creatorX",
        "score": 55,
        "sol_invested": 0.05,
        "sol_received": 0.10,
        "pnl_sol": 0.05,
        "pnl_pct": 100.0,
        "entry_time": 1000.0,
        "exit_time": 1060.0,
        "hold_minutes": 1.0,
        "reason": "take_profit_75pct",
    }
    base.update(overrides)
    return base


class TestSchema:
    def test_empty_db_has_zero_count(self, db):
        assert db.count() == 0

    def test_load_all_on_empty_returns_empty_list(self, db):
        assert db.load_all() == []


class TestInsert:
    def test_single_insert_round_trips(self, db):
        rid = db.insert(_trade())
        assert rid > 0
        assert db.count() == 1
        rows = db.load_all()
        assert len(rows) == 1
        assert rows[0]["mint"] == "MintAAA"
        assert rows[0]["pnl_pct"] == 100.0

    def test_unknown_field_survives_via_raw(self, db):
        # New field added later — should round-trip through `raw`
        # without requiring a schema migration.
        db.insert(_trade(future_field="new_value", nested={"k": 1}))
        rows = db.load_all()
        assert rows[0]["future_field"] == "new_value"
        assert rows[0]["nested"] == {"k": 1}

    def test_minimal_record(self, db):
        # Only mint required (NOT NULL); everything else nullable.
        rid = db.insert({"mint": "OnlyMint"})
        assert rid > 0
        rows = db.load_all()
        assert rows[0]["mint"] == "OnlyMint"

    def test_bulk_insert_count_matches(self, db):
        records = [_trade(mint=f"Mint{i}", pnl_sol=float(i)) for i in range(10)]
        n = db.insert_many(records)
        assert n == 10
        assert db.count() == 10

    def test_bulk_insert_empty_is_safe(self, db):
        assert db.insert_many([]) == 0


class TestQueries:
    def test_recent_returns_newest_first(self, db):
        for i in range(5):
            db.insert(_trade(mint=f"M{i}", exit_time=1000.0 + i))
        recent = db.recent(3)
        assert len(recent) == 3
        # Newest first
        assert recent[0]["mint"] == "M4"
        assert recent[2]["mint"] == "M2"

    def test_load_all_returns_oldest_first(self, db):
        for i in range(3):
            db.insert(_trade(mint=f"M{i}"))
        rows = db.load_all()
        assert [r["mint"] for r in rows] == ["M0", "M1", "M2"]

    def test_by_mint_filters(self, db):
        db.insert(_trade(mint="ALPHA"))
        db.insert(_trade(mint="BETA"))
        db.insert(_trade(mint="ALPHA", pnl_sol=0.01))
        alpha = db.by_mint("ALPHA")
        assert len(alpha) == 2
        assert all(r["mint"] == "ALPHA" for r in alpha)

    def test_aggregate_by_reason(self, db):
        db.insert(_trade(reason="stop_loss",   pnl_sol=-0.10))
        db.insert(_trade(reason="stop_loss",   pnl_sol=-0.20))
        db.insert(_trade(reason="take_profit", pnl_sol=0.50))
        agg = {row["reason"]: row for row in db.aggregate_by_reason()}
        assert agg["stop_loss"]["n"] == 2
        assert agg["stop_loss"]["wins"] == 0
        assert agg["stop_loss"]["total_pnl_sol"] == pytest.approx(-0.30)
        assert agg["take_profit"]["wins"] == 1


class TestConcurrency:
    def test_many_inserts_dont_lose_rows(self, db):
        # Smoke test: lock should serialize concurrent writes correctly.
        # Async tasks don't actually preempt mid-call here, but this
        # at least proves the autocommit + lock combo doesn't drop rows.
        for i in range(100):
            db.insert(_trade(mint=f"M{i}"))
        assert db.count() == 100
