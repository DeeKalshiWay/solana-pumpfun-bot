"""
tools/edge_brain.py

The learning layer for the graduation sniper — outcome attribution,
mistake memory, and bounded parameter adaptation. No ML dependencies;
everything is transparent bucketed statistics with small-sample guards.

What it learns (persisted to logs/edge_brain.json):

1. FEATURE-BUCKET WIN RATES — every closed trade is attributed to buckets:
     entry_sol:  [80-81), [81-82), [82-83), [83-84.5)
     velocity:   [0-1.5), [1.5-3), [3-6), [6+)
     buyers:     [6-8), [8-12), [12+)
     utc_hour:   0-23 (no public data exists on this — our own edge)
   A bucket is VETOED when its Wilson-score lower bound on win rate drops
   below the strategy's break-even (win ~+3.8%, stall ~-6.9% → breakeven
   ~0.645) with at least MIN_N_VETO samples. Vetoes lift automatically if
   later trades pull the bound back up — the brain can un-learn.

2. MISTAKE MEMORY — creators whose tokens stall-stopped or timed out on us
   accumulate strikes; at CREATOR_STRIKE_LIMIT their future tokens are
   rejected outright (same philosophy as the rug-memory / bleeders
   blacklist on the copy side). Mints we exited badly are never re-entered.

3. BOUNDED PARAMETER ADAPTATION — the brain scores discrete "arms" for the
   entry threshold and velocity floor by realized EV and recommends the
   best arm once it has MIN_N_ARM samples. The sniper applies suggestions
   only inside hard bounds (entry ∈ [80.0, 82.5], velocity ∈ [1.0, 3.0]).
   Set EDGE_BRAIN_AUTOTUNE=0 in .env to freeze (report-only), mirroring
   the AUTO_TUNE_ENABLED precedent on the main bot.

Every decision (veto, strike, suggestion) is written to
logs/edge_brain_journal.jsonl with the evidence that produced it, so the
operator can always audit WHY the bot refused a trade.
"""

from __future__ import annotations

import json
import math
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAIN_FILE = os.path.join(ROOT, "logs", "edge_brain.json")
JOURNAL = os.path.join(ROOT, "logs", "edge_brain_journal.jsonl")

# Break-even win rate for the graduation trade. 0.645 was derived from the
# original 1.5-SOL stop (+3.82% win / -6.94% loss); 0.71 on 2026-07-17 from
# realized stop-redesign economics; 0.81 on 2026-07-21 after the friction
# model compressed wins to +2.7% avg vs -11.3% avg loss (drift check fired
# twice). Post-friction numbers are the ones that matter.
BREAKEVEN_WIN_RATE = 0.81
MIN_N_VETO = 8          # min samples in a bucket before it can be vetoed
MIN_N_ARM = 15          # min samples per arm before autotune trusts it
CREATOR_STRIKE_LIMIT = 2
Z = 1.28                # ~80% one-sided confidence for the Wilson bound

ENTRY_ARMS = [80.0, 81.0, 82.0]
# 2026-07-21: arms/bounds re-based around the new velocity floor (3.0) after
# the shadow-data gate rewire — the old [1.0, 3.0] range is below the floor
VELOCITY_ARMS = [3.0, 4.5, 6.0]
ENTRY_BOUNDS = (80.0, 82.5)
VELOCITY_BOUNDS = (2.5, 8.0)


def _wilson_lb(wins: int, n: int, z: float = Z) -> float:
    """Wilson score lower bound — pessimistic win-rate estimate that
    self-corrects for small samples (n=3 3/3 is NOT treated as 100%)."""
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - spread) / denom)


def _bucket_entry_sol(x: float) -> str:
    for lo, hi in ((80, 81), (81, 82), (82, 83), (83, 84.5)):
        if lo <= x < hi:
            return f"entry_{lo}-{hi}"
    return "entry_other"


def _bucket_velocity(x: float) -> str:
    for lo, hi in ((0, 1.5), (1.5, 3), (3, 6)):
        if lo <= x < hi:
            return f"vel_{lo}-{hi}"
    return "vel_6+"


def _bucket_buyers(x: int) -> str:
    for lo, hi in ((6, 8), (8, 12)):
        if lo <= x < hi:
            return f"buyers_{lo}-{hi}"
    return "buyers_12+"


class EdgeBrain:
    def __init__(self):
        self.data = self._load()

    # ---------------- persistence ----------------
    def _load(self) -> dict:
        if os.path.exists(BRAIN_FILE):
            try:
                return json.load(open(BRAIN_FILE))
            except Exception:
                pass
        return {
            "buckets": {},          # bucket_key -> {"n": int, "wins": int, "pnl": float}
            "creators": {},         # creator -> {"strikes": int, "trades": int}
            "no_reentry": [],       # mints never to re-enter
            "arms": {},             # arm_key -> {"n": int, "wins": int, "pnl": float}
            "trades_seen": 0,
        }

    def _save(self):
        tmp = BRAIN_FILE + ".tmp"
        json.dump(self.data, open(tmp, "w"), indent=1)
        os.replace(tmp, BRAIN_FILE)

    def _journal(self, rec: dict):
        rec.setdefault("ts", time.time())
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    # ---------------- learning ----------------
    def record(self, *, mint: str, creator: str, entry_real_sol: float,
               velocity: float, buyers: int, pnl_sol: float,
               exit_reason: str):
        """Attribute a closed trade to its feature buckets and update memory."""
        win = pnl_sol > 0
        hour = time.gmtime().tm_hour
        buckets = [
            _bucket_entry_sol(entry_real_sol),
            _bucket_velocity(velocity),
            _bucket_buyers(buyers),
            f"hour_{hour:02d}",
        ]
        for b in buckets:
            d = self.data["buckets"].setdefault(b, {"n": 0, "wins": 0, "pnl": 0.0})
            d["n"] += 1
            d["wins"] += int(win)
            d["pnl"] = round(d["pnl"] + pnl_sol, 6)

        # Arm attribution (which entry/velocity regime produced this trade)
        entry_arm = max((a for a in ENTRY_ARMS if a <= entry_real_sol),
                        default=ENTRY_ARMS[0])
        vel_arm = max((a for a in VELOCITY_ARMS if a <= velocity),
                      default=VELOCITY_ARMS[0])
        for key in (f"entry_arm_{entry_arm}", f"vel_arm_{vel_arm}"):
            d = self.data["arms"].setdefault(key, {"n": 0, "wins": 0, "pnl": 0.0})
            d["n"] += 1
            d["wins"] += int(win)
            d["pnl"] = round(d["pnl"] + pnl_sol, 6)

        # Mistake memory
        # 2026-07-17: disaster_stop added — it was missing when the exit
        # reason was introduced, so TOSHI (7AzV4vtq) was re-entered 3x in
        # 6 min while a pump-dump cycle ran on the curve top (-0.18 SOL)
        if exit_reason in ("stall_stop", "timeout", "disaster_stop"):
            self.data["no_reentry"].append(mint)
            self.data["no_reentry"] = self.data["no_reentry"][-500:]
            if creator:
                c = self.data["creators"].setdefault(
                    creator, {"strikes": 0, "trades": 0})
                c["strikes"] += 1
                c["trades"] += 1
                if c["strikes"] == CREATOR_STRIKE_LIMIT:
                    self._journal({"action": "CREATOR_BLOCKED",
                                   "creator": creator,
                                   "strikes": c["strikes"]})
        elif creator and creator in self.data["creators"]:
            self.data["creators"][creator]["trades"] += 1

        self.data["trades_seen"] += 1
        self._save()

    # ---------------- gating ----------------
    def allows(self, *, mint: str, creator: str, entry_real_sol: float,
               velocity: float, buyers: int) -> tuple[bool, str]:
        """Pre-entry veto check. Returns (allowed, reason_if_vetoed)."""
        if mint in self.data["no_reentry"]:
            return False, "no_reentry_mint"
        c = self.data["creators"].get(creator or "")
        if c and c["strikes"] >= CREATOR_STRIKE_LIMIT:
            return False, f"creator_blocked({c['strikes']} strikes)"
        # NOTE: hour_* buckets are recorded and reported but intentionally NOT
        # used for vetoing — an hour aggregates trades that lost for other
        # (causal) reasons, so hour vetoes double-count blame. Once we have
        # per-hour data with the causal buckets controlled, revisit.
        for b in (_bucket_entry_sol(entry_real_sol),
                  _bucket_velocity(velocity),
                  _bucket_buyers(buyers)):
            d = self.data["buckets"].get(b)
            if not d or d["n"] < MIN_N_VETO:
                continue
            lb = _wilson_lb(d["wins"], d["n"])
            if lb < BREAKEVEN_WIN_RATE and d["wins"] / d["n"] < BREAKEVEN_WIN_RATE:
                self._journal({"action": "VETO", "bucket": b,
                               "n": d["n"], "wins": d["wins"],
                               "wilson_lb": round(lb, 3),
                               "mint": mint})
                return False, f"learned_veto:{b}(lb={lb:.2f},n={d['n']})"
        return True, ""

    # ---------------- adaptation ----------------
    def suggest_params(self) -> dict:
        """Best entry-threshold and velocity-floor arms by realized EV,
        only once each arm has MIN_N_ARM samples. Values are clamped to
        hard bounds by the caller regardless."""
        out = {}
        for prefix, arms, bounds, name in (
                ("entry_arm_", ENTRY_ARMS, ENTRY_BOUNDS, "entry_real_sol"),
                ("vel_arm_", VELOCITY_ARMS, VELOCITY_BOUNDS, "velocity_floor")):
            best, best_ev = None, None
            for a in arms:
                d = self.data["arms"].get(f"{prefix}{a}")
                if not d or d["n"] < MIN_N_ARM:
                    continue
                ev = d["pnl"] / d["n"]
                if best_ev is None or ev > best_ev:
                    best, best_ev = a, ev
            if best is not None:
                val = max(bounds[0], min(bounds[1], best))
                out[name] = {"value": val, "ev_per_trade": round(best_ev, 5)}
        if out:
            self._journal({"action": "SUGGEST_PARAMS", **{
                k: v["value"] for k, v in out.items()}})
        return out

    # ---------------- reporting ----------------
    def report(self) -> str:
        lines = [f"trades learned from: {self.data['trades_seen']}"]
        for b, d in sorted(self.data["buckets"].items()):
            if d["n"] == 0:
                continue
            lb = _wilson_lb(d["wins"], d["n"])
            flag = " VETO" if (d["n"] >= MIN_N_VETO
                               and lb < BREAKEVEN_WIN_RATE
                               and d["wins"] / d["n"] < BREAKEVEN_WIN_RATE) else ""
            lines.append(f"  {b:<16} n={d['n']:>3} win={d['wins']/d['n']:.0%} "
                         f"lb={lb:.2f} pnl={d['pnl']:+.4f}{flag}")
        blocked = [c for c, d in self.data["creators"].items()
                   if d["strikes"] >= CREATOR_STRIKE_LIMIT]
        lines.append(f"blocked creators: {len(blocked)}  "
                     f"no-reentry mints: {len(self.data['no_reentry'])}")
        return "\n".join(lines)


if __name__ == "__main__":
    print(EdgeBrain().report())
