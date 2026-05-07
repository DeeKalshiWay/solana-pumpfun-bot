"""
tools/migrate_jsonl_to_sqlite.py

One-shot migrator: reads logs/closed_trades.jsonl line-by-line and
bulk-inserts into logs/trades.db (sqlite3). Idempotent in the sense
that re-running on an already-populated db is harmless if the JSONL
hasn't grown — but it WILL duplicate rows if it has, so the intended
flow is:

  1. Bot stops writing to JSONL (or pause it).
  2. Run this once.
  3. Bot resumes; risk/manager.py dual-writes to JSONL + db.

Usage:
  python -m tools.migrate_jsonl_to_sqlite
  python -m tools.migrate_jsonl_to_sqlite --jsonl logs/closed_trades.jsonl --db logs/trades.db
  python -m tools.migrate_jsonl_to_sqlite --dry-run     # parse + count, no writes
  python -m tools.migrate_jsonl_to_sqlite --truncate    # wipe db before importing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as `python tools/migrate_jsonl_to_sqlite.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logger.trade_db import TradeDB  # noqa: E402


def parse_jsonl(path: str) -> tuple[list[dict], int]:
    """Return (records, bad_line_count). Skips blank and malformed lines
    rather than aborting — partial migrations are better than none."""
    records: list[dict] = []
    bad = 0
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                bad += 1
                print(f"  line {lineno}: bad json ({e})", file=sys.stderr)
    return records, bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", default="logs/closed_trades.jsonl")
    ap.add_argument("--db", default="logs/trades.db")
    ap.add_argument("--dry-run", action="store_true", help="parse + count, skip writes")
    ap.add_argument("--truncate", action="store_true", help="DELETE FROM closed_trades before importing")
    args = ap.parse_args()

    if not Path(args.jsonl).exists():
        print(f"ERROR: {args.jsonl} not found", file=sys.stderr)
        return 1

    print(f"==> Reading  {args.jsonl}")
    records, bad = parse_jsonl(args.jsonl)
    print(f"    {len(records)} valid records, {bad} bad lines skipped")

    if args.dry_run:
        if records:
            print(f"    First record keys: {sorted(records[0].keys())}")
            print(f"    Last record keys:  {sorted(records[-1].keys())}")
        print("==> Dry run; no writes.")
        return 0

    print(f"==> Opening  {args.db}")
    db = TradeDB(args.db)

    if args.truncate:
        before = db.count()
        with db._lock:  # noqa: SLF001 — internal access intentional in migrator
            db._conn.execute("DELETE FROM closed_trades")
        print(f"    Truncated {before} existing rows")

    print(f"==> Inserting {len(records)} rows")
    n = db.insert_many(records)
    after = db.count()
    print(f"==> Done. {n} inserted, db now holds {after} rows.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
