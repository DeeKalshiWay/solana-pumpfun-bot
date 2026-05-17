"""Regression tests for rug-memory hard-reject + faster-kick-in.

Live observation 2026-05-17: rug_memory data was being collected but
only ever applied as a soft -15 score penalty (capped). High-base-score
tokens still got bought even when their exact pattern had rugged on us
many times.

Change ships three knobs (all env-tunable):
  - RUG_HARD_REJECT_MATCHES (default 3): signatures with N+ historical
    rugs are hard-rejected at the filter stage, regardless of score.
  - RUG_MATCH_MIN_RUGS (default 1): penalty kicks in after the FIRST
    rug, not the third. Faster learning.
  - RUG_MAX_PENALTY (default 50): effectively uncapped — overwhelming
    evidence can dominate the score (for the 1- and 2-match cases that
    survive past hard reject).

These tests pin the contract:
  - 0 matches → no penalty, no reject
  - 1 match → penalty starts (was 0 under old MIN_RUGS=3)
  - 3+ matches → should_hard_reject returns truthy
  - HARD_REJECT_MATCHES=0 disables the feature
"""
import importlib

import pytest


@pytest.fixture
def fresh_rug_memory(tmp_path, monkeypatch):
    """Fresh RugMemory instance pointed at an isolated JSONL file.

    Reload the module so RUG_LOG_FILE and the config-driven thresholds
    are picked up freshly (since env knobs may be monkeypatched).
    """
    log_file = tmp_path / "rug_patterns.jsonl"
    import analyzer.rug_memory as rug_mod
    importlib.reload(rug_mod)
    monkeypatch.setattr(rug_mod, "RUG_LOG_FILE", str(log_file))
    return rug_mod


def _features(init_buy: float = 0.3, curve_pct: float = 25.0, score: int = 36) -> dict:
    return {
        "initial_buy_sol":   init_buy,
        "bonding_curve_pct": curve_pct,
        "score":             score,
    }


class TestKickInAtOneMatch:
    """Penalty should start at the FIRST historical rug under new defaults."""

    def test_no_match_no_penalty(self, fresh_rug_memory):
        rm = fresh_rug_memory.RugMemory()
        assert rm.score_penalty(_features()) == 0
        assert rm.matched_count(_features()) == 0

    def test_single_rug_already_penalizes(self, fresh_rug_memory):
        """Under legacy MIN_RUGS=3 this returned 0. Under new default 1
        it returns the linear penalty (2 * n_matches)."""
        rm = fresh_rug_memory.RugMemory()
        rm.record_rug(_features(), pnl_pct=-80.0, hold_minutes=2.0)
        penalty = rm.score_penalty(_features())
        assert penalty > 0, (
            "first rug at this signature must dock score under "
            "RUG_MATCH_MIN_RUGS=1 — that's the 'kick in faster' contract"
        )


class TestHardReject:
    """Once a signature has rugged N+ times, the data is used as a
    real reject rather than just a score nudge."""

    def test_below_threshold_no_reject(self, fresh_rug_memory):
        rm = fresh_rug_memory.RugMemory()
        # 2 rugs at this signature; default threshold is 3
        rm.record_rug(_features(), pnl_pct=-80.0, hold_minutes=2.0)
        rm.record_rug(_features(), pnl_pct=-70.0, hold_minutes=3.0)
        assert rm.should_hard_reject(_features()) == 0

    def test_at_threshold_rejects(self, fresh_rug_memory):
        rm = fresh_rug_memory.RugMemory()
        for _ in range(3):
            rm.record_rug(_features(), pnl_pct=-80.0, hold_minutes=2.0)
        n = rm.should_hard_reject(_features())
        assert n >= 3, (
            f"signature with 3 historical rugs must hard-reject "
            f"(got {n}) — that's the whole point of the feature"
        )

    def test_unrelated_signature_unaffected(self, fresh_rug_memory):
        """Hard reject is per-signature, not global. Other patterns must
        still flow through."""
        rm = fresh_rug_memory.RugMemory()
        for _ in range(5):
            rm.record_rug(_features(init_buy=0.3), pnl_pct=-80.0, hold_minutes=2.0)
        # Different init_buy bucket — should not be rejected
        assert rm.should_hard_reject(_features(init_buy=1.5)) == 0

    def test_disabled_via_env(self, fresh_rug_memory, monkeypatch):
        """RUG_HARD_REJECT_MATCHES=0 fully disables the hard-reject path
        (escape hatch if it gets too aggressive in production)."""
        monkeypatch.setattr(fresh_rug_memory, "HARD_REJECT_MATCHES", 0)
        rm = fresh_rug_memory.RugMemory()
        for _ in range(10):
            rm.record_rug(_features(), pnl_pct=-80.0, hold_minutes=2.0)
        assert rm.should_hard_reject(_features()) == 0


class TestPenaltyUncapped:
    """A signature with overwhelming historical evidence should be able
    to dock more than the legacy 15-point cap."""

    def test_many_matches_exceed_legacy_cap(self, fresh_rug_memory):
        rm = fresh_rug_memory.RugMemory()
        # 10 rugs → linear = 20 pts. Legacy cap was 15.
        for _ in range(10):
            rm.record_rug(_features(), pnl_pct=-80.0, hold_minutes=2.0)
        penalty = rm.score_penalty(_features())
        assert penalty > 15, (
            f"penalty {penalty} did not exceed legacy 15-pt cap — "
            f"RUG_MAX_PENALTY uncap regressed"
        )
