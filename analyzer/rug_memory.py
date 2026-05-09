"""
analyzer/rug_memory.py

Pattern memory of rugged trades. When a closed trade rugs (pnl_pct
<= RUG_PNL_THRESHOLD), its feature fingerprint is recorded. New
candidates whose fingerprint matches enough past rugs get a score
penalty so the bot stops repeating the same losing pattern.

This is the cheapest possible "learn from past mistakes" loop — no
ML, no training pass, no drift. Just signature-bucket counting.

Persistence: append-only JSONL at logs/rug_patterns.jsonl. Counts
are rebuilt in memory on startup.
"""

import json
import os
import time

from loguru import logger

RUG_LOG_FILE      = "logs/rug_patterns.jsonl"
RUG_PNL_THRESHOLD = -50.0   # pnl_pct <= this counts as a rug
MATCH_MIN_RUGS    = 3       # need this many matching rugs before penalizing
MAX_PENALTY       = 15      # max score points to dock per match


def _bin_init_buy(v: float) -> str:
    if v <= 0:    return "0"
    if v < 0.2:   return "0_02"
    if v < 0.5:   return "02_05"
    if v < 1.0:   return "05_1"
    if v < 2.0:   return "1_2"
    return "2plus"


def _bin_curve_pct(v: float) -> str:
    if v < 10:    return "0_10"
    if v < 20:    return "10_20"
    if v < 40:    return "20_40"
    if v < 60:    return "40_60"
    return "60plus"


def _bin_score(v: int) -> str:
    if v < 32:    return "lt32"
    if v < 35:    return "32_34"
    if v < 40:    return "35_39"
    return "40plus"


def signature(token: dict) -> str:
    """Compact bucket key for a token's feature pattern."""
    init_buy = float(token.get("initial_buy_sol", 0) or 0)
    curve    = float(token.get("bonding_curve_pct", 0) or 0)
    sc       = int(token.get("score", 0) or 0)
    return "|".join([
        _bin_init_buy(init_buy),
        _bin_curve_pct(curve),
        _bin_score(sc),
    ])


class RugMemory:
    def __init__(self):
        self._counts: dict[str, int] = {}
        self._total_rugs = 0
        self._load()

    def _load(self):
        if not os.path.exists(RUG_LOG_FILE):
            return
        try:
            with open(RUG_LOG_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    sig = rec.get("signature")
                    if sig:
                        self._counts[sig] = self._counts.get(sig, 0) + 1
                        self._total_rugs += 1
            logger.info(
                f"[RUG-MEM] Loaded {self._total_rugs} rug patterns "
                f"across {len(self._counts)} signature buckets"
            )
        except Exception as e:
            logger.warning(f"[RUG-MEM] load failed: {e}")

    def record_rug(self, token_features: dict, pnl_pct: float, hold_minutes: float, mint: str = "", symbol: str = ""):
        """
        Append a rug record. token_features should carry the same fields
        seen at score-time: initial_buy_sol, bonding_curve_pct, score.
        Caller is responsible for the rug threshold check (we re-check
        defensively here too).
        """
        if pnl_pct > RUG_PNL_THRESHOLD:
            return
        sig = signature(token_features)
        rec = {
            "ts":           time.time(),
            "signature":    sig,
            "mint":         mint,
            "symbol":       symbol,
            "pnl_pct":      round(pnl_pct, 2),
            "hold_minutes": round(hold_minutes, 1),
            "init_buy_sol": float(token_features.get("initial_buy_sol", 0) or 0),
            "curve_pct":    float(token_features.get("bonding_curve_pct", 0) or 0),
            "score":        int(token_features.get("score", 0) or 0),
        }
        try:
            os.makedirs("logs", exist_ok=True)
            with open(RUG_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            self._counts[sig] = self._counts.get(sig, 0) + 1
            self._total_rugs += 1
            logger.info(
                f"[RUG-MEM] Recorded rug · {symbol or mint[:8]} · sig={sig} · "
                f"now={self._counts[sig]}× · pnl={pnl_pct:.1f}%"
            )
        except Exception as e:
            logger.debug(f"[RUG-MEM] record failed: {e}")

    def score_penalty(self, token: dict) -> int:
        """
        Return a non-negative score deduction. 0 means no match.
        Linear scaling: more historical rugs at this signature = bigger dock.
        """
        sig = signature(token)
        n = self._counts.get(sig, 0)
        if n < MATCH_MIN_RUGS:
            return 0
        return min(MAX_PENALTY, n * 2)

    def matched_count(self, token: dict) -> int:
        """How many past rugs share this token's signature."""
        return self._counts.get(signature(token), 0)

    def stats(self) -> dict:
        return {
            "total_rugs":  self._total_rugs,
            "unique_sigs": len(self._counts),
            "top_sigs":    sorted(self._counts.items(), key=lambda x: -x[1])[:10],
        }


# Singleton — imported by signal_scorer + risk_manager
rug_memory = RugMemory()
