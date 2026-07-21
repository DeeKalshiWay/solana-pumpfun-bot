"""Tests for the shadow-fed EV brain (tools/edge_brain.py)."""

import json
import time

import tools.edge_brain as eb


def _write_log(path, shadow_rows, close_rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in shadow_rows:
            f.write(json.dumps(r) + "\n")
        for r in close_rows:
            f.write(json.dumps(r) + "\n")


def _shadow(grad, vel, ms, steps, age, cg, replies, hour, ts):
    return {"event": "shadow_outcome",
            "outcome": "graduated" if grad else "died", "ts": ts,
            "snap": {"real_sol": 81.0, "velocity_5m": vel, "max_share": ms,
                     "steps": steps, "age_min": age, "creator_grads": cg,
                     "replies": replies, "hour_utc": hour, "ts": ts}}


def _close(net_pct, reason, ts):
    return {"event": "close", "net_pct": net_pct, "exit_reason": reason,
            "fees_sol": 0.0014, "ts": ts}   # fees_sol => friction-era


def _brain(tmp_path, monkeypatch, shadow_rows, close_rows, state=None):
    log = tmp_path / "trades.jsonl"
    brain_file = tmp_path / "brain.json"
    journal = tmp_path / "journal.jsonl"
    _write_log(log, shadow_rows, close_rows)
    if state is not None:
        brain_file.write_text(json.dumps(state))
    monkeypatch.setattr(eb, "TRADES_LOG", str(log))
    monkeypatch.setattr(eb, "BRAIN_FILE", str(brain_file))
    monkeypatch.setattr(eb, "JOURNAL", str(journal))
    return eb.EdgeBrain()


def test_cold_start_allows(tmp_path, monkeypatch):
    """No data at all -> never freezes trading."""
    brain = _brain(tmp_path, monkeypatch, [], [])
    ok, why = brain.allows(mint="M", creator="C", entry_real_sol=81.0,
                           velocity=4.0, features={"velocity_5m": 4.0})
    assert ok, why


def test_no_reentry_blocks(tmp_path, monkeypatch):
    brain = _brain(tmp_path, monkeypatch, [], [],
                   state={"creators": {}, "no_reentry": ["BAD"], "arms": {},
                          "trades_seen": 0})
    ok, why = brain.allows(mint="BAD", creator="C", velocity=5.0)
    assert not ok and why == "no_reentry_mint"


def test_creator_strike_blocks(tmp_path, monkeypatch):
    brain = _brain(tmp_path, monkeypatch, [], [],
                   state={"creators": {"C": {"strikes": 2, "trades": 2}},
                          "no_reentry": [], "arms": {}, "trades_seen": 0})
    ok, why = brain.allows(mint="M", creator="C", velocity=5.0)
    assert not ok and "creator_blocked" in why


def test_ev_veto_on_bad_cohort(tmp_path, monkeypatch):
    """A candidate landing among mostly-failed lookalikes is EV-vetoed."""
    now = time.time()
    # 40 low-velocity, high-max_share examples that mostly DIED
    shadow = [_shadow(grad=(i % 5 == 0),   # 20% completion
                      vel=1.2, ms=0.95, steps=1, age=30, cg=0, replies=2,
                      hour=8, ts=now - 3600) for i in range(40)]
    closes = ([_close(3.0, "pre_grad_exit", now)] * 3 +
              [_close(-12.0, "stall_stop", now)] * 5)
    brain = _brain(tmp_path, monkeypatch, shadow, closes)
    ok, why = brain.allows(mint="M", creator="C", entry_real_sol=81.0,
                           velocity=1.2,
                           features={"velocity_5m": 1.2, "max_share": 0.95,
                                     "steps": 1, "age_min": 30,
                                     "creator_grads": 0, "replies": 2,
                                     "hour_utc": 8})
    assert not ok and "ev_veto" in why, why


def test_ev_allows_good_cohort(tmp_path, monkeypatch):
    """A candidate among mostly-graduated lookalikes passes."""
    now = time.time()
    shadow = [_shadow(grad=(i % 10 != 0),   # 90% completion
                      vel=6.0, ms=0.5, steps=4, age=5, cg=2, replies=50,
                      hour=14, ts=now - 3600) for i in range(40)]
    closes = ([_close(3.0, "pre_grad_exit", now)] * 6 +
              [_close(-12.0, "stall_stop", now)] * 2)
    brain = _brain(tmp_path, monkeypatch, shadow, closes)
    ok, why = brain.allows(mint="M", creator="C", entry_real_sol=81.0,
                           velocity=6.0,
                           features={"velocity_5m": 6.0, "max_share": 0.5,
                                     "steps": 4, "age_min": 5,
                                     "creator_grads": 2, "replies": 50,
                                     "hour_utc": 14})
    assert ok, why


def test_breakeven_derived_from_economics(tmp_path, monkeypatch):
    # Enough samples that the conservative pseudo-count prior washes out.
    now = time.time()
    closes = ([_close(3.0, "pre_grad_exit", now)] * 100 +
              [_close(-9.0, "stall_stop", now)] * 100)
    brain = _brain(tmp_path, monkeypatch, [], closes)
    # break-even ~= |loss| / (win + |loss|) = 9 / 12 = 0.75
    assert abs(brain.breakeven() - 0.75) < 0.02, brain.breakeven()


def test_economics_prior_guards_tiny_sample(tmp_path, monkeypatch):
    """A handful of unusually-mild friction-era losses must NOT collapse the
    break-even bar — the prior keeps it conservative until data accrues."""
    now = time.time()
    closes = ([_close(1.3, "pre_grad_exit", now)] * 5 +
              [_close(-3.3, "migration", now)] * 4)   # the real 2026-07-21 case
    brain = _brain(tmp_path, monkeypatch, [], closes)
    # raw would be ~72%; shrinkage must keep it near the known-true ~80%
    assert brain.breakeven() > 0.77, brain.breakeven()


def test_time_decay_downweights_old(tmp_path, monkeypatch):
    """Old examples count for less than fresh ones."""
    now = time.time()
    fresh = _shadow(grad=1, vel=6.0, ms=0.5, steps=4, age=5, cg=1, replies=10,
                    hour=14, ts=now - 60)
    old = _shadow(grad=0, vel=6.0, ms=0.5, steps=4, age=5, cg=1, replies=10,
                  hour=14, ts=now - 20 * 86400)   # 20 days -> 4 half-lives
    brain = _brain(tmp_path, monkeypatch, [fresh] * 3 + [old] * 3, [])
    eff_n, rate, lb = brain._completion_prob(
        {"velocity_5m": 6.0, "max_share": 0.5, "steps": 4, "age_min": 5,
         "creator_grads": 1, "replies": 10, "hour_utc": 14})
    assert rate > 0.5, rate   # fresh graduated examples dominate the old dead
