"""Regression tests for analyzer.rug_memory.

The rug-memory feature was silently dormant for ~5 hours (commit cf89f61)
because record-time and lookup-time used different bucket keys: the scorer
mutated token["score"] -= rug_penalty before the close path captured it,
so records landed at the post-penalty bin (e.g. lt32) while lookups fired
at the pre-penalty bin (e.g. 35_39). Records and lookups never collided.

These tests pin the contract that fixes that bug: the bucket signature
must depend only on RAW (pre-penalty) score, and a record-then-lookup
round-trip with the same raw_score must match.
"""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def rm(tmp_path, monkeypatch):
    """Fresh RugMemory instance with an isolated JSONL store per test."""
    log_file = tmp_path / "rug_patterns.jsonl"
    import analyzer.rug_memory as rug_mod

    monkeypatch.setattr(rug_mod, "RUG_LOG_FILE", str(log_file))
    # Re-import to pick up monkeypatched module-level constant in any code
    # path that closes over it; RugMemory reads RUG_LOG_FILE at method-call
    # time so a fresh instance is enough.
    importlib.reload(rug_mod)
    monkeypatch.setattr(rug_mod, "RUG_LOG_FILE", str(log_file))
    return rug_mod.RugMemory()


def _features(init_buy: float, curve_pct: float, score: int) -> dict:
    return {
        "initial_buy_sol":   init_buy,
        "bonding_curve_pct": curve_pct,
        "score":             score,
    }


class TestSignatureStability:
    """The bucket key is the heart of the feature — these tests pin it."""

    def test_same_features_same_signature(self):
        from analyzer.rug_memory import signature
        a = _features(0.3, 25.0, 36)
        b = _features(0.3, 25.0, 36)
        assert signature(a) == signature(b)

    def test_scores_in_same_bin_collide(self):
        """36 and 38 both land in the '35_39' bin — by design."""
        from analyzer.rug_memory import signature
        assert signature(_features(0.3, 25.0, 36)) == signature(_features(0.3, 25.0, 38))

    def test_scores_across_bins_differ(self):
        """31 (lt32) and 36 (35_39) must NOT collide — this is the bug."""
        from analyzer.rug_memory import signature
        assert signature(_features(0.3, 25.0, 31)) != signature(_features(0.3, 25.0, 36))


class TestRecordLookupRoundTrip:
    """End-to-end: feed enough rugs, then look up with the SAME features."""

    def test_lookup_misses_below_threshold(self, rm):
        from analyzer.rug_memory import MATCH_MIN_RUGS
        feats = _features(0.3, 25.0, 36)
        for _ in range(MATCH_MIN_RUGS - 1):
            rm.record_rug(feats, pnl_pct=-80.0, hold_minutes=2.0)
        assert rm.score_penalty(feats) == 0

    def test_lookup_hits_at_threshold(self, rm):
        from analyzer.rug_memory import MATCH_MIN_RUGS
        feats = _features(0.3, 25.0, 36)
        for _ in range(MATCH_MIN_RUGS):
            rm.record_rug(feats, pnl_pct=-80.0, hold_minutes=2.0)
        assert rm.score_penalty(feats) > 0
        assert rm.matched_count(feats) == MATCH_MIN_RUGS

    def test_penalty_caps_at_max(self, rm):
        from analyzer.rug_memory import MAX_PENALTY
        feats = _features(0.3, 25.0, 36)
        for _ in range(200):
            rm.record_rug(feats, pnl_pct=-80.0, hold_minutes=2.0)
        assert rm.score_penalty(feats) == MAX_PENALTY


class TestPenaltyAppliedScoreDoesNotChangeBin:
    """The original bug: scorer recorded under raw_score=36 (bin '35_39')
    after applying rug_penalty=15 → effective score 21 (bin 'lt32'). If a
    future refactor passes the POST-penalty score to record_rug, this test
    fails.
    """

    def test_round_trip_with_simulated_scorer_flow(self, rm):
        from analyzer.rug_memory import MATCH_MIN_RUGS

        # 1) Scorer computes raw score and would apply rug penalty here.
        #    The raw_score is what MUST be passed to record_rug/lookup —
        #    not the post-penalty score.
        raw_score   = 36
        rug_penalty = 15
        post_score  = raw_score - rug_penalty  # 21 — lands in a different bin

        feats_raw  = _features(0.3, 25.0, raw_score)
        feats_post = _features(0.3, 25.0, post_score)

        # 2) Sanity: post-penalty score is in a different bucket. If this
        #    fails the bucket boundaries moved — see _bin_score().
        from analyzer.rug_memory import signature
        assert signature(feats_raw) != signature(feats_post)

        # 3) Record under raw_score (the fixed contract).
        for _ in range(MATCH_MIN_RUGS):
            rm.record_rug(feats_raw, pnl_pct=-80.0, hold_minutes=2.0)

        # 4) Lookup with raw_score MUST hit; lookup with post_score MUST miss.
        assert rm.score_penalty(feats_raw)  > 0, \
            "rug_memory lookup with raw_score missed — record/lookup bin mismatch regressed"
        assert rm.score_penalty(feats_post) == 0, \
            "rug_memory bucket boundaries leaked; post-penalty score should not collide"


class TestRugThreshold:
    """A friction-only -50% on a 0.01 SOL trade is not a rug — guard kept
    in risk.manager but the threshold constant lives here."""

    def test_below_threshold_records(self, rm):
        feats = _features(0.3, 25.0, 36)
        rm.record_rug(feats, pnl_pct=-90.0, hold_minutes=1.0)
        assert rm.matched_count(feats) == 1

    def test_above_threshold_skipped(self, rm):
        feats = _features(0.3, 25.0, 36)
        rm.record_rug(feats, pnl_pct=-10.0, hold_minutes=1.0)
        assert rm.matched_count(feats) == 0


class TestPersistence:
    """Restarts must replay the JSONL append-log into the in-memory counts."""

    def test_counts_survive_restart(self, tmp_path, monkeypatch):
        log_file = tmp_path / "rug_patterns.jsonl"
        import analyzer.rug_memory as rug_mod

        monkeypatch.setattr(rug_mod, "RUG_LOG_FILE", str(log_file))

        rm1 = rug_mod.RugMemory()
        feats = _features(0.3, 25.0, 36)
        for _ in range(5):
            rm1.record_rug(feats, pnl_pct=-80.0, hold_minutes=2.0)

        assert Path(log_file).exists()

        # New instance reads the same file.
        rm2 = rug_mod.RugMemory()
        assert rm2.matched_count(feats) == 5
