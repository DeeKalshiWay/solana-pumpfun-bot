"""Regression tests for AUTO_TUNE_ENABLED env switch.

Live observation 2026-05-17: post-merge of the ATA-create + momentum-
stall-gate + rug-memory-hard-reject PRs, the auto_tuner was reading
WR=19% from the 73 historical pre-fix trades (which were artificially
losing due to Anchor 3012 buy failures + pre-TP1 stalls). It then
tightened the score offset to +4, pushing the live threshold from 33
to 37, so post-fix tokens scoring 33–36 silently never queued — bot
"stopped trading" again.

Fix: AUTO_TUNE_ENABLED=false in .env freezes the threshold at the
operator's chosen base. The in-memory offset is preserved so re-
enabling later doesn't drop state.

These tests pin:
  - Default behavior unchanged (still applies offset)
  - When disabled, effective_min_score == MIN_BUY_SCORE regardless of offset
  - When disabled, _evaluate() never shifts offset (even with bad WR)
  - When disabled, offset state is preserved across the call
"""
import importlib

import pytest


@pytest.fixture
def tuner(monkeypatch, tmp_path):
    """Fresh AutoTuner instance pointed at an isolated state file."""
    monkeypatch.chdir(tmp_path)
    import analyzer.auto_tuner as at_mod
    importlib.reload(at_mod)
    return at_mod, at_mod.AutoTuner()


class _FakeRiskMgr:
    def __init__(self, trades):
        self.closed_trades = list(trades)


class TestAutoTuneEnabledDefault:
    """Out-of-the-box behavior preserved: enabled, offset applied."""

    def test_default_applies_offset(self, tuner):
        at_mod, t = tuner
        assert at_mod.AUTO_TUNE_ENABLED is True
        t.offset = 4
        assert t.effective_min_score() == at_mod.MIN_BUY_SCORE + 4

    def test_default_tightens_on_low_wr(self, tuner):
        at_mod, t = tuner
        # 50 trades all losses → WR = 0.0 → must tighten
        t.attach(_FakeRiskMgr([{"pnl_sol": -0.01}] * 50))
        prev = t.offset
        t._evaluate()
        assert t.offset == prev + 1, "default tuner must tighten on low WR"


class TestAutoTuneDisabled:
    """AUTO_TUNE_ENABLED=false: threshold frozen at MIN_BUY_SCORE,
    offset state preserved so re-enabling resumes where it left off."""

    def test_disabled_freezes_threshold(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TUNE_ENABLED", "false")
        import config as cfg_mod
        importlib.reload(cfg_mod)
        import analyzer.auto_tuner as at_mod
        importlib.reload(at_mod)
        t = at_mod.AutoTuner()
        t.offset = 4   # would normally bump threshold by 4
        assert t.effective_min_score() == at_mod.MIN_BUY_SCORE, (
            "AUTO_TUNE_ENABLED=false must freeze threshold at the "
            "operator's chosen base, ignoring the offset"
        )

    def test_disabled_evaluate_does_not_tighten(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TUNE_ENABLED", "false")
        import config as cfg_mod
        importlib.reload(cfg_mod)
        import analyzer.auto_tuner as at_mod
        importlib.reload(at_mod)
        t = at_mod.AutoTuner()
        t.attach(_FakeRiskMgr([{"pnl_sol": -0.01}] * 50))
        prev = t.offset
        t._evaluate()
        assert t.offset == prev, (
            "with AUTO_TUNE_ENABLED=false, _evaluate must not shift the "
            "offset even when WR would otherwise trigger a tighten"
        )

    def test_disabled_preserves_offset_state(self, monkeypatch, tmp_path):
        """Re-enabling later should resume from the same offset, not
        zero — operator may flip the env var on and off as the WR
        sample matures."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TUNE_ENABLED", "false")
        import config as cfg_mod
        importlib.reload(cfg_mod)
        import analyzer.auto_tuner as at_mod
        importlib.reload(at_mod)
        t = at_mod.AutoTuner()
        t.offset = 4
        t.attach(_FakeRiskMgr([{"pnl_sol": -0.01}] * 50))
        t._evaluate()
        assert t.offset == 4, "in-memory offset must be preserved"


class TestOffsetMaxConfigurable:
    """AUTO_TUNE_OFFSET_MAX=0 is a softer middle ground: tuner can still
    loosen on a hot streak but can never tighten above the base."""

    def test_offset_max_zero_blocks_tighten(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TUNE_OFFSET_MAX", "0")
        import config as cfg_mod
        importlib.reload(cfg_mod)
        import analyzer.auto_tuner as at_mod
        importlib.reload(at_mod)
        t = at_mod.AutoTuner()
        # Currently at 0; bad WR would normally bump to +1
        t.attach(_FakeRiskMgr([{"pnl_sol": -0.01}] * 50))
        prev = t.offset
        t._evaluate()
        assert t.offset == prev, (
            "AUTO_TUNE_OFFSET_MAX=0 must block tightening even on bad WR"
        )

    def test_offset_max_zero_still_allows_loosen(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TUNE_OFFSET_MAX", "0")
        import config as cfg_mod
        importlib.reload(cfg_mod)
        import analyzer.auto_tuner as at_mod
        importlib.reload(at_mod)
        t = at_mod.AutoTuner()
        # Bumper: 50 winning trades → WR=1.0 → must loosen
        t.attach(_FakeRiskMgr([{"pnl_sol": +0.01}] * 50))
        prev = t.offset
        t._evaluate()
        assert t.offset == prev - 1, (
            "AUTO_TUNE_OFFSET_MAX=0 must NOT block loosening — the cap "
            "is only on tightening above base"
        )
