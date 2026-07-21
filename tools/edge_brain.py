"""
tools/edge_brain.py

The learning layer for the graduation sniper. No ML dependencies — everything
is transparent, auditable statistics with small-sample guards. Rewritten
2026-07-21 (the "five tiers" upgrade) to fix the core weakness: the old brain
learned only from the ~20 trades we actually took while 300+ labelled
examples sat unused in the shadow dataset.

What it does now:

1. SHADOW-FED COMPLETION MODEL (Tier 1) — the primary signal. Every token
   that reached the decision zone is a labelled example (graduated? y/n) in
   logs/graduation_trades.jsonl as a `shadow_outcome` event. The brain reads
   ALL of them (14x the trade count) and estimates P(graduate | features)
   for a candidate via a time-decayed k-nearest-cohort lookup. Since our
   dominant loss is non-completion, completion probability IS most of the
   win/loss signal.

2. EV GATE WITH DERIVED BREAK-EVEN (Tier 2) — vetoes on expected value, not
   win rate. Win/loss magnitudes are read from the friction-era `close`
   events (self-identified by the `fees_sol` field), so break-even is
   derived from reality, never hand-set:
       EV% = p*avg_win% + (1-p)*avg_loss%   (p = pessimistic completion prob)
   A candidate is vetoed when its Wilson-lower-bound EV is below zero.

3. WHOLE-TOKEN COHORT, NOT INDEPENDENT BUCKETS (Tier 3) — the veto asks
   "tokens that look like THIS one completed X% of the time" over the joint
   feature vector, instead of four univariate buckets that double-count
   blame. Univariate buckets survive only as a cold-start fallback + report.

4. RICH FEATURES (Tier 4) — velocity, max_share, steps, token age, creator
   graduation history, reply count, and the hour's own completion prior all
   feed the cohort. Creator history is credit as well as blame.

5. ERA-AWARE / TIME-DECAYED (Tier 5) — examples are exponentially
   down-weighted by age (half-life HALFLIFE_DAYS) and dropped past
   MAX_AGE_DAYS, so stale evidence from a since-changed config can't keep
   vetoing. Economics use friction-era closes only.

MISTAKE MEMORY (unchanged) — creators whose tokens stopped out on us
accumulate strikes; at CREATOR_STRIKE_LIMIT their future tokens are rejected.
Mints we exited badly are never re-entered.

Every veto/strike/suggestion is journaled to logs/edge_brain_journal.jsonl
with the evidence that produced it, so the operator can always audit WHY.
"""

from __future__ import annotations

import json
import math
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAIN_FILE = os.path.join(ROOT, "logs", "edge_brain.json")
JOURNAL = os.path.join(ROOT, "logs", "edge_brain_journal.jsonl")
TRADES_LOG = os.path.join(ROOT, "logs", "graduation_trades.jsonl")

# Fallback break-even (used only until enough friction-era closes exist to
# derive it). Matches the last measured post-friction economics.
BREAKEVEN_WIN_RATE = 0.81
DEFAULT_WIN_PCT = 2.7        # avg % gain on a completed-for-us exit
DEFAULT_LOSS_PCT = -11.3     # avg % loss on a failed exit
ECON_PRIOR_N = 8            # pseudo-count: shrink derived economics toward the
                           # conservative defaults until this many friction-era
                           # closes exist (a 4-sample loss est. is dangerously
                           # optimistic — the big disaster stops were pre-fees)

MIN_N_VETO = 8               # cold-start fallback: univariate bucket min n
CREATOR_STRIKE_LIMIT = 2
Z = 1.28                     # ~80% one-sided confidence for the Wilson bound

# Cohort model
REFRESH_S = 300.0            # rebuild models from the log at most this often
HALFLIFE_DAYS = 5.0          # time-decay half-life for example weights
MAX_AGE_DAYS = 30.0          # drop examples older than this outright
KNN_K = 25                   # neighbours per cohort lookup
MIN_COHORT_WEIGHT = 10.0     # effective weighted-n before the cohort can veto
EV_FLOOR_PCT = 0.0           # veto when pessimistic EV% is below this

# Joint feature vector (numeric dims; None values imputed to the dataset mean)
FEATURES = ["velocity_5m", "max_share", "steps", "age_min",
            "creator_grads", "replies", "hour_prior"]

# Bounded parameter adaptation (unchanged mechanism)
MIN_N_ARM = 15
ENTRY_ARMS = [80.0, 81.0, 82.0]
VELOCITY_ARMS = [3.0, 4.5, 6.0]
ENTRY_BOUNDS = (80.0, 82.5)
VELOCITY_BOUNDS = (2.5, 8.0)


def _wilson_lb(wins: float, n: float, z: float = Z) -> float:
    """Wilson score lower bound — pessimistic rate estimate that self-corrects
    for small samples. Accepts fractional (weighted) wins/n."""
    if n <= 0:
        return 0.0
    p = max(0.0, min(1.0, wins / n))
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - spread) / denom)


# ---- univariate buckets (cold-start fallback + human-readable report) ----
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
        # models rebuilt from the trades log (cached; see _refresh)
        self._examples: list = []       # [{vec, grad, weight}]
        self._stats: dict = {}          # per-feature (mean, std)
        self._hour_prior: dict = {}     # hour -> completion rate
        self._econ = {"win_pct": DEFAULT_WIN_PCT, "loss_pct": DEFAULT_LOSS_PCT,
                      "n_win": 0, "n_loss": 0}
        self._buckets: dict = {}        # univariate fallback, from shadow data
        self._refresh_ts = 0.0
        self._refresh(force=True)

    # ---------------- persistence (hand-maintained memory only) -----------
    def _load(self) -> dict:
        if os.path.exists(BRAIN_FILE):
            try:
                d = json.load(open(BRAIN_FILE))
                d.setdefault("creators", {})
                d.setdefault("no_reentry", [])
                d.setdefault("arms", {})
                d.setdefault("trades_seen", 0)
                return d
            except Exception:
                pass
        return {"creators": {}, "no_reentry": [], "arms": {},
                "trades_seen": 0}

    def _save(self):
        tmp = BRAIN_FILE + ".tmp"
        json.dump(self.data, open(tmp, "w"), indent=1)
        os.replace(tmp, BRAIN_FILE)

    def _journal(self, rec: dict):
        rec.setdefault("ts", time.time())
        try:
            with open(JOURNAL, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    # ---------------- model build (Tiers 1,2,4,5) -------------------------
    def _refresh(self, force: bool = False):
        now = time.time()
        if not force and now - self._refresh_ts < REFRESH_S:
            return
        self._refresh_ts = now
        shadow, closes = [], []
        try:
            with open(TRADES_LOG, encoding="utf-8") as f:
                for line in f:
                    if '"shadow_outcome"' in line:
                        try:
                            shadow.append(json.loads(line))
                        except Exception:
                            pass
                    elif '"close"' in line and '"fees_sol"' in line:
                        try:
                            closes.append(json.loads(line))
                        except Exception:
                            pass
        except OSError:
            pass

        # --- hour completion prior (used as a cohort feature) ---
        hour_agg: dict = {}
        for s in shadow:
            h = (s.get("snap") or {}).get("hour_utc")
            if h is None:
                continue
            d = hour_agg.setdefault(int(h), [0, 0])
            d[0] += 1
            d[1] += int(s.get("outcome") == "graduated")
        overall = (sum(d[1] for d in hour_agg.values())
                   / max(sum(d[0] for d in hour_agg.values()), 1))
        self._hour_prior = {h: (d[1] / d[0] if d[0] else overall)
                            for h, d in hour_agg.items()}
        self._hour_prior_default = overall

        # --- raw feature rows (+ time-decay weight, era cutoff) ---
        raw, buckets = [], {}
        for s in shadow:
            snap = s.get("snap") or {}
            ts = s.get("ts") or snap.get("ts") or now
            age_days = (now - ts) / 86400.0
            if age_days > MAX_AGE_DAYS:
                continue
            grad = int(s.get("outcome") == "graduated")
            weight = 0.5 ** (age_days / HALFLIFE_DAYS)
            feats = self._feat_dict(snap)
            raw.append((feats, grad, weight))
            # univariate fallback buckets, shadow-fed
            for b in (_bucket_velocity(snap.get("velocity_5m") or 0),
                      _bucket_entry_sol(snap.get("real_sol") or 0)):
                bd = buckets.setdefault(b, [0.0, 0.0])
                bd[0] += weight
                bd[1] += weight * grad
        self._buckets = buckets

        # --- standardize features across the dataset ---
        self._stats = {}
        for k in FEATURES:
            vals = [f[k] for f, _, _ in raw if f.get(k) is not None]
            if vals:
                mean = sum(vals) / len(vals)
                var = sum((v - mean) ** 2 for v in vals) / max(len(vals), 1)
                self._stats[k] = (mean, math.sqrt(var) or 1.0)
            else:
                self._stats[k] = (0.0, 1.0)
        self._examples = [
            {"vec": self._standardize(f), "grad": g, "weight": w}
            for f, g, w in raw]

        # --- economics (Tier 2): friction-era closes only ---
        wins = [c["net_pct"] for c in closes
                if c.get("exit_reason") in ("pre_grad_exit", "migration")
                and (c.get("net_pct") or 0) > 0]
        losses = [c["net_pct"] for c in closes
                  if c.get("exit_reason") in ("stall_stop", "disaster_stop",
                                              "timeout")
                  or (c.get("net_pct") or 0) <= 0]
        # pseudo-count shrinkage toward the conservative defaults
        win_pct = ((sum(wins) + ECON_PRIOR_N * DEFAULT_WIN_PCT)
                   / (len(wins) + ECON_PRIOR_N))
        loss_pct = ((sum(losses) + ECON_PRIOR_N * DEFAULT_LOSS_PCT)
                    / (len(losses) + ECON_PRIOR_N))
        self._econ = {"win_pct": win_pct, "loss_pct": loss_pct,
                      "n_win": len(wins), "n_loss": len(losses)}

    def _feat_dict(self, snap: dict) -> dict:
        h = snap.get("hour_utc")
        return {
            "velocity_5m": snap.get("velocity_5m"),
            "max_share": snap.get("max_share"),
            "steps": snap.get("steps"),
            "age_min": snap.get("age_min"),
            "creator_grads": snap.get("creator_grads"),
            "replies": snap.get("replies"),
            "hour_prior": (self._hour_prior.get(int(h))
                           if h is not None else None),
        }

    def _standardize(self, feats: dict) -> list:
        out = []
        for k in FEATURES:
            mean, std = self._stats.get(k, (0.0, 1.0))
            v = feats.get(k)
            out.append(0.0 if v is None else (v - mean) / std)  # None -> mean
        return out

    # ---------------- completion cohort (Tier 1,3) ------------------------
    def _completion_prob(self, feats: dict):
        """(effective_n, weighted_grad_rate, wilson_lb) for a candidate via a
        time-decayed k-nearest cohort over the joint feature vector."""
        if not self._examples:
            return 0.0, None, None
        q = self._standardize(feats)
        scored = []
        for ex in self._examples:
            d2 = sum((a - b) ** 2 for a, b in zip(q, ex["vec"]))
            scored.append((d2, ex))
        scored.sort(key=lambda x: x[0])
        cohort = scored[:KNN_K]
        wsum = sum(ex["weight"] for _, ex in cohort)
        gsum = sum(ex["weight"] * ex["grad"] for _, ex in cohort)
        if wsum <= 0:
            return 0.0, None, None
        rate = gsum / wsum
        return wsum, rate, _wilson_lb(gsum, wsum)

    def breakeven(self) -> float:
        """Derived break-even win rate from realized economics."""
        w, l = self._econ["win_pct"], abs(self._econ["loss_pct"])
        return l / (w + l) if (w + l) > 0 else BREAKEVEN_WIN_RATE

    # ---------------- learning (mistake memory + arms) --------------------
    def record(self, *, mint: str, creator: str, entry_real_sol: float,
               velocity: float, buyers: int, pnl_sol: float,
               exit_reason: str):
        """Update hand-maintained memory from a closed trade. The completion
        and economics models are rebuilt from the log, not here."""
        win = pnl_sol > 0
        entry_arm = max((a for a in ENTRY_ARMS if a <= entry_real_sol),
                        default=ENTRY_ARMS[0])
        vel_arm = max((a for a in VELOCITY_ARMS if a <= velocity),
                      default=VELOCITY_ARMS[0])
        for key in (f"entry_arm_{entry_arm}", f"vel_arm_{vel_arm}"):
            d = self.data["arms"].setdefault(key, {"n": 0, "wins": 0, "pnl": 0.0})
            d["n"] += 1
            d["wins"] += int(win)
            d["pnl"] = round(d["pnl"] + pnl_sol, 6)

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
                                   "creator": creator, "strikes": c["strikes"]})
        elif creator and creator in self.data["creators"]:
            self.data["creators"][creator]["trades"] += 1

        self.data["trades_seen"] += 1
        self._save()
        self._refresh(force=True)   # new shadow_outcome likely just landed too

    # ---------------- gating ----------------------------------------------
    def allows(self, *, mint: str, creator: str, entry_real_sol: float = 0.0,
               velocity: float = 0.0, buyers: int = 0,
               features: dict | None = None) -> tuple[bool, str]:
        """Pre-entry gate. Returns (allowed, reason_if_vetoed). Never raises —
        on any internal error it falls back to allow so a brain bug can't
        freeze all trading."""
        # 1. Mistake memory — hard blocks from our own realized losses
        if mint in self.data["no_reentry"]:
            return False, "no_reentry_mint"
        c = self.data["creators"].get(creator or "")
        if c and c["strikes"] >= CREATOR_STRIKE_LIMIT:
            return False, f"creator_blocked({c['strikes']} strikes)"

        try:
            self._refresh()
            feats = dict(features or {})
            feats.setdefault("velocity_5m", velocity)
            feats.setdefault("entry_real_sol", entry_real_sol)
            h = feats.get("hour_utc")
            feats["hour_prior"] = (self._hour_prior.get(int(h))
                                   if h is not None else None)
            eff_n, rate, lb = self._completion_prob(feats)

            # 2. EV gate on the joint cohort (primary path). Decision uses
            # the point-estimate completion rate — the Wilson LB is too harsh
            # a rule (it would veto most of the pool the upstream gates
            # already filtered, starving both P&L and data). MIN_COHORT_WEIGHT
            # guards tiny cohorts; lb is journaled for audit.
            if rate is not None and eff_n >= MIN_COHORT_WEIGHT:
                win_pct, loss_pct = self._econ["win_pct"], self._econ["loss_pct"]
                ev = rate * win_pct + (1 - rate) * loss_pct
                if ev < EV_FLOOR_PCT:
                    self._journal({"action": "VETO_EV", "mint": mint,
                                   "cohort_n": round(eff_n, 1),
                                   "grad_rate": round(rate, 3),
                                   "grad_lb": round(lb, 3),
                                   "ev_pct": round(ev, 2),
                                   "breakeven": round(self.breakeven(), 3)})
                    return False, (f"ev_veto(p={rate:.0%},lb={lb:.0%},"
                                   f"ev={ev:+.1f}%,n={eff_n:.0f})")
                return True, ""

            # 3. Cold-start fallback: univariate shadow-fed bucket veto
            be = self.breakeven()
            for b in (_bucket_velocity(velocity),
                      _bucket_entry_sol(entry_real_sol)):
                bd = self._buckets.get(b)
                if not bd or bd[0] < MIN_N_VETO:
                    continue
                blb = _wilson_lb(bd[1], bd[0])
                if blb < be and (bd[1] / bd[0]) < be:
                    return False, f"bucket_veto:{b}(lb={blb:.2f})"
        except Exception as e:
            self._journal({"action": "ALLOW_ERROR",
                           "error": f"{type(e).__name__}:{str(e)[:80]}",
                           "mint": mint})
        return True, ""

    # ---------------- adaptation ------------------------------------------
    def suggest_params(self) -> dict:
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
                out[name] = {"value": max(bounds[0], min(bounds[1], best)),
                             "ev_per_trade": round(best_ev, 5)}
        if out:
            self._journal({"action": "SUGGEST_PARAMS",
                           **{k: v["value"] for k, v in out.items()}})
        return out

    # ---------------- reporting -------------------------------------------
    def report(self) -> str:
        self._refresh(force=True)
        e = self._econ
        be = self.breakeven()
        lines = [
            f"trades learned from: {self.data['trades_seen']}  |  "
            f"shadow examples: {len(self._examples)} (time-decayed)",
            f"economics (friction-era): win {e['win_pct']:+.1f}% "
            f"(n={e['n_win']}) / loss {e['loss_pct']:+.1f}% (n={e['n_loss']}) "
            f"-> derived break-even {be:.0%}",
        ]
        # cohort model sanity: completion rate at a few reference points
        blocked = [c for c, d in self.data["creators"].items()
                   if d["strikes"] >= CREATOR_STRIKE_LIMIT]
        lines.append(f"blocked creators: {len(blocked)}  "
                     f"no-reentry mints: {len(self.data['no_reentry'])}  "
                     f"cohort k={KNN_K} halflife={HALFLIFE_DAYS}d")
        # univariate view (diagnostic)
        for b, bd in sorted(self._buckets.items()):
            if bd[0] < 3:
                continue
            rate = bd[1] / bd[0]
            flag = " LOW" if rate < be else ""
            lines.append(f"  {b:<14} eff_n={bd[0]:>5.1f} grad={rate:.0%}{flag}")
        return "\n".join(lines)


if __name__ == "__main__":
    print(EdgeBrain().report())
