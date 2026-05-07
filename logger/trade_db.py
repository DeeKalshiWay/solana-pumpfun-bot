"""
logger/trade_db.py — sqlite3 persistence for closed trades.

Why this exists
---------------
Closed trades have lived in `logs/closed_trades.jsonl` since day one.
That worked while the file was small, but `_load_closed_trades` already
scans the whole file at every startup and the analyzer/score_bins
pipeline does the same. At ~3,000 trades the cost is invisible; at
30,000 it isn't, and there's no good way to query "trades by ticker"
or "last 24h winners" without reading and parsing every row.

Design
------
- Explicit columns for everything queryable: mint, symbol, creator,
  score, PnL, timestamps, exit reason. These get indexes.
- A `raw` TEXT column holds the full original JSON so newly-added
  fields don't require a schema migration before they can persist.
- Append-only writer: same crash-safety story as the JSONL — one
  INSERT per trade, no in-place rewrites.
- Coexists with the JSONL during transition. The JSONL is still
  written by risk/manager.py; this module is a parallel sink the
  readers prefer.

Testing
-------
`TradeDB(":memory:")` gives an in-memory database — used by
tests/test_trade_db.py. No filesystem touch, no fixture cleanup.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = "logs/trades.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS closed_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mint            TEXT    NOT NULL,
    symbol          TEXT,
    creator         TEXT,
    score           INTEGER,
    sol_invested    REAL,
    sol_received    REAL,
    pnl_sol         REAL,
    pnl_pct         REAL,
    entry_time      REAL,
    exit_time       REAL,
    hold_minutes    REAL,
    reason          TEXT,
    raw             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_closed_trades_exit_time ON closed_trades(exit_time);
CREATE INDEX IF NOT EXISTS ix_closed_trades_mint     ON closed_trades(mint);
CREATE INDEX IF NOT EXISTS ix_closed_trades_creator  ON closed_trades(creator);
CREATE INDEX IF NOT EXISTS ix_closed_trades_reason   ON closed_trades(reason);
"""


class TradeDB:
    """Thread-safe sqlite3 wrapper for closed trades. One connection per
    instance, guarded by a lock — sqlite3 connections are not
    thread-safe by default and the bot has multiple async tasks that
    can close trades concurrently."""

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Writers ───────────────────────────────────────────────────────
    def insert(self, record: dict[str, Any]) -> int:
        """Insert one closed-trade record. Returns the new row id."""
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO closed_trades (
                    mint, symbol, creator, score,
                    sol_invested, sol_received, pnl_sol, pnl_pct,
                    entry_time, exit_time, hold_minutes, reason, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("mint", ""),
                    record.get("symbol"),
                    record.get("creator"),
                    record.get("score"),
                    record.get("sol_invested"),
                    record.get("sol_received"),
                    record.get("pnl_sol"),
                    record.get("pnl_pct"),
                    record.get("entry_time"),
                    record.get("exit_time"),
                    record.get("hold_minutes"),
                    record.get("reason"),
                    json.dumps(record),
                ),
            )
            return int(cur.lastrowid or 0)

    def insert_many(self, records: Iterable[dict[str, Any]]) -> int:
        """Bulk insert. Returns the count actually written."""
        rows = [
            (
                r.get("mint", ""),
                r.get("symbol"),
                r.get("creator"),
                r.get("score"),
                r.get("sol_invested"),
                r.get("sol_received"),
                r.get("pnl_sol"),
                r.get("pnl_pct"),
                r.get("entry_time"),
                r.get("exit_time"),
                r.get("hold_minutes"),
                r.get("reason"),
                json.dumps(r),
            )
            for r in records
        ]
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO closed_trades (
                    mint, symbol, creator, score,
                    sol_invested, sol_received, pnl_sol, pnl_pct,
                    entry_time, exit_time, hold_minutes, reason, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    # ── Readers ───────────────────────────────────────────────────────
    def count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM closed_trades")
            return int(cur.fetchone()[0])

    def load_all(self) -> list[dict[str, Any]]:
        """Return every trade as a list of dicts, oldest first.
        The dict shape matches the original JSONL — readers that
        consumed JSONL keep working unchanged."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT raw FROM closed_trades ORDER BY id ASC"
            )
            return [json.loads(row["raw"]) for row in cur.fetchall()]

    def recent(self, n: int) -> list[dict[str, Any]]:
        """N most recent trades, newest first."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT raw FROM closed_trades ORDER BY id DESC LIMIT ?",
                (n,),
            )
            return [json.loads(row["raw"]) for row in cur.fetchall()]

    def by_mint(self, mint: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT raw FROM closed_trades WHERE mint = ? ORDER BY id ASC",
                (mint,),
            )
            return [json.loads(row["raw"]) for row in cur.fetchall()]

    def aggregate_by_reason(self) -> list[dict[str, Any]]:
        """Win count, loss count, total PnL grouped by exit reason."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT
                    reason,
                    COUNT(*)                                AS n,
                    SUM(CASE WHEN pnl_sol > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(pnl_sol)                            AS total_pnl_sol,
                    AVG(pnl_pct)                            AS avg_pnl_pct
                FROM closed_trades
                GROUP BY reason
                ORDER BY total_pnl_sol DESC
                """
            )
            return [dict(row) for row in cur.fetchall()]


# ── Module-level singleton (lazy) ─────────────────────────────────────
_singleton: TradeDB | None = None
_singleton_lock = threading.Lock()


def get_trade_db(path: str = DEFAULT_DB_PATH) -> TradeDB:
    """Lazy module-level singleton. The bot needs exactly one db
    handle process-wide; tests construct their own instances directly
    against ':memory:'."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = TradeDB(path)
        return _singleton
