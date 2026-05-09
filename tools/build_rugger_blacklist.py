"""
tools/build_rugger_blacklist.py

Tag every creator that ever rugged us, persisted to logs/rugger_creators.json
which is loaded by config.CREATOR_BLACKLIST at bot startup.

Sources combined:
  1. trades.db — creators where any of OUR trades closed at <= -50% pnl_pct
  2. counterfactual.jsonl — creators where one of their REJECTED tokens
     dropped <= -50% in the 10-min window after we rejected it
  3. creators.json — creators with 2+ losses, 0 wins, net negative cumulative

Run with: `python -m tools.build_rugger_blacklist`
Re-run periodically (e.g. before each bot restart) so the list stays fresh.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# Allow `python tools/build_rugger_blacklist.py` from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RUG_THRESHOLD_PCT = -50.0
TRADES_DB        = "logs/trades.db"
COUNTERFACTUAL   = "logs/counterfactual.jsonl"
CREATORS_JSON    = "logs/creators.json"
OUTPUT           = "logs/rugger_creators.json"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    ruggers: set[str] = set()
    sources = {"trades_db": 0, "counterfactual": 0, "creators_json": 0}

    if os.path.exists(TRADES_DB):
        con = sqlite3.connect(TRADES_DB)
        for (creator, _) in con.execute(
            "SELECT creator, MIN(pnl_pct) FROM closed_trades "
            "GROUP BY creator HAVING MIN(pnl_pct) <= ?",
            (RUG_THRESHOLD_PCT,),
        ):
            if creator and creator.strip():
                if creator not in ruggers:
                    sources["trades_db"] += 1
                ruggers.add(creator.strip())
        con.close()

    if os.path.exists(COUNTERFACTUAL):
        with open(COUNTERFACTUAL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("mc_delta_pct", 0) <= RUG_THRESHOLD_PCT:
                    cr = (r.get("creator") or "").strip()
                    if cr:
                        if cr not in ruggers:
                            sources["counterfactual"] += 1
                        ruggers.add(cr)

    if os.path.exists(CREATORS_JSON):
        with open(CREATORS_JSON, encoding="utf-8") as f:
            creators_db = json.load(f)
        for cr, info in creators_db.items():
            if not isinstance(info, dict):
                continue
            losses = info.get("losses", 0) or 0
            wins   = info.get("wins", 0) or 0
            pnl    = info.get("total_pnl_sol", 0) or 0
            if losses >= 2 and wins == 0 and pnl < 0:
                if cr and cr not in ruggers:
                    sources["creators_json"] += 1
                if cr:
                    ruggers.add(cr.strip())

    out = {
        "generated_at": int(time.time()),
        "rug_threshold_pct": RUG_THRESHOLD_PCT,
        "count": len(ruggers),
        "sources": sources,
        "creators": sorted(ruggers),
    }
    Path(OUTPUT).parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print(f"Rugger creators tagged: {len(ruggers)}")
    print(f"  from trades.db (real closes <= {RUG_THRESHOLD_PCT}%): {sources['trades_db']}")
    print(f"  from counterfactual (rejected -> rugged):              {sources['counterfactual']}")
    print(f"  from creators.json (2+ losses, 0 wins, net negative):  {sources['creators_json']}")
    print(f"\nWritten to: {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
