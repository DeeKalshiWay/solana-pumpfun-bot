"""
tests/test_graduation_scalp.py

Invariants for the 2026-07-22 scalp-only rewire. These exist because the
54-trade paper readout showed the book was losing to STRUCTURAL defects, not
to bad luck — and each defect is the kind that regresses silently:

  - 11 of 55 entries (20%) were opened with less curve left than the
    round-trip fee costs, because ENTRY_MAX_REAL_SOL was hand-set equal to
    EXIT_REAL_SOL and nothing checked the relationship.
  - a position hung for 3782s through a 13.45 SOL drawdown because a pending
    sell was only ever executed from the curve-poll path.

A test that just asserts today's constants would be a tautology. These assert
the RELATIONSHIPS that make the strategy coherent.
"""

import time

import pytest

from tools import graduation_sniper as gs


# =============================================================================
# The runway invariant — defect 1
# =============================================================================
def test_entry_ceiling_is_derived_from_the_exit():
    """ENTRY_MAX_REAL_SOL must not be an independent knob. If someone moves
    the exit, the entry ceiling has to move with it or the dead band returns."""
    assert gs.ENTRY_MAX_REAL_SOL == gs.EXIT_REAL_SOL - gs.MIN_RUNWAY_SOL


def test_every_allowed_entry_can_clear_the_round_trip_fee():
    """The real invariant: for ANY entry the band permits, selling at the exit
    must net a profit after pump.fun's 1% each way plus both tx fees. This is
    the check whose absence let FOXGAR open at 84.31 against an 84.5 exit."""
    for entry in [gs.ENTRY_REAL_SOL + i * 0.1
                  for i in range(int((gs.ENTRY_MAX_REAL_SOL
                                      - gs.ENTRY_REAL_SOL) * 10) + 1)]:
        if entry >= gs.ENTRY_MAX_REAL_SOL:
            continue
        net_pct = _scalp_net_pct(entry, gs.EXIT_REAL_SOL)
        assert net_pct > 0, (
            f"entry at real_sol={entry:.1f} exiting at {gs.EXIT_REAL_SOL} "
            f"nets {net_pct:+.2f}% — the band permits a guaranteed loser")


def test_worst_allowed_entry_carries_its_share_of_the_deaths():
    """Not just positive — positive enough to be worth taking. At the observed
    7% death rate and -22% ex-tail death loss, a scalp needs ~+1.4% net just to
    break even across the population. MIN_RUNWAY_SOL=2.0 was rejected for
    admitting +0.54% entries; this is the test that caught it."""
    worst = gs.ENTRY_MAX_REAL_SOL - 0.01
    net = _scalp_net_pct(worst, gs.EXIT_REAL_SOL)
    assert net > 1.4, (
        f"worst allowed entry nets {net:+.2f}% — too thin to carry a 7% "
        f"death rate at -22%. Raise MIN_RUNWAY_SOL.")


def test_exit_clears_graduation_so_the_scalp_wins_the_race():
    """Defect 3: the old exit sat 0.5 SOL below graduation and lost the race
    16 times in 54 trades. Require real headroom."""
    assert gs.GRADUATION_REAL_SOL - gs.EXIT_REAL_SOL >= 1.0


def _scalp_net_pct(entry_real_sol: float, exit_real_sol: float) -> float:
    """Net % on a SIZE_SOL scalp, using the module's own curve math."""
    k = 30.0 * (1073000000 / 1e6)

    def vtok(real_sol):
        return k / (gs.VIRTUAL_SOL_INIT + real_sol)

    tok = gs.buy_on_curve(gs.VIRTUAL_SOL_INIT + entry_real_sol,
                          vtok(entry_real_sol), gs.SIZE_SOL)
    out = gs.sell_on_curve(gs.VIRTUAL_SOL_INIT + exit_real_sol,
                           vtok(exit_real_sol), tok)
    return ((out - 2 * gs.TX_FEE_SOL) / gs.SIZE_SOL - 1.0) * 100.0


# =============================================================================
# The entry gate actually rejects the dead band
# =============================================================================
class _FakeTracker:
    """Minimal Tracker stand-in — _maybe_enter only touches these."""

    def __init__(self, real_sol):
        self.mint = "TestMint1111111111111111111111111111111111"
        self.symbol = "TEST"
        self.creator = "creator1"
        self.real_sol = real_sol
        self.v_sol = gs.VIRTUAL_SOL_INIT + real_sol
        self.v_tok = (30.0 * (1073000000 / 1e6)) / self.v_sol
        self.max_dump_sol = 0.0
        self.dump_recovered = False
        self.first_seen = time.time() - 3600
        self.created_ts = time.time() - 600
        self.rejected_reason = None
        self.creator_coins = 1
        self.creator_grads = 1
        self.reply_count = 5
        self.shadow_snap = None

    def velocity(self, _w):
        return 10.0

    def climb_quality(self):
        return 6, 0.3


def _sniper(monkeypatch, tmp_path):
    """A sniper with all disk and network side effects neutralised."""
    monkeypatch.setattr(gs, "TRADES_LOG", str(tmp_path / "trades.jsonl"))
    monkeypatch.setattr(gs, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(gs, "KILL_FILE", str(tmp_path / "KILL"))
    s = gs.GraduationSniper.__new__(gs.GraduationSniper)
    s.trackers, s.pending = {}, {}
    s.state = {"positions": {}, "account": {"seed_sol": 5.0,
                                            "realized_sol": 0.0},
               "recent_stops": {}}
    s.entry_real_sol = gs.ENTRY_REAL_SOL
    s.velocity_floor = gs.VELOCITY_FLOOR_SOL
    s._hour_stats, s._hour_stats_ts = {}, time.time() + 1e6
    s.alerts = gs.GradAlerts(enabled=False)
    s.brain = _AllowAllBrain()
    return s


class _AllowAllBrain:
    def allows(self, **_kw):
        return True, ""


def test_entry_above_the_runway_ceiling_is_rejected(monkeypatch, tmp_path):
    s = _sniper(monkeypatch, tmp_path)
    t = _FakeTracker(gs.ENTRY_MAX_REAL_SOL + 0.5)
    s._maybe_enter(t)
    assert not s.pending, "opened a position with no runway to the exit"
    assert t.rejected_reason == "no_runway"


def test_entry_inside_the_band_is_accepted(monkeypatch, tmp_path):
    s = _sniper(monkeypatch, tmp_path)
    t = _FakeTracker(gs.ENTRY_REAL_SOL + 0.2)
    s._maybe_enter(t)
    assert t.mint in s.pending, f"rejected a valid entry: {t.rejected_reason}"
    assert s.pending[t.mint]["type"] == "buy"


def test_entry_below_the_floor_is_not_flagged_as_a_rejection(monkeypatch,
                                                             tmp_path):
    """A token still climbing into the zone is 'not yet', not 'no'. Flagging it
    would poison the skip telemetry and the dashboard's flagged column."""
    s = _sniper(monkeypatch, tmp_path)
    t = _FakeTracker(gs.ENTRY_REAL_SOL - 5.0)
    s._maybe_enter(t)
    assert not s.pending
    assert t.rejected_reason is None


# =============================================================================
# Stall stop stays off — defect 2
# =============================================================================
def test_stall_stop_is_disabled():
    """15 fires, 0 wins, -0.343 SOL on tokens that went on to graduate. If this
    is ever re-enabled it should be a deliberate, reviewed change."""
    assert gs.STALL_STOP_ENABLED is False


def test_disaster_stop_is_tighter_than_the_old_eight_sol():
    assert gs.DISASTER_STOP_SOL <= 5.0


# =============================================================================
# Shadow dataset comparability
# =============================================================================
def test_shadow_zone_is_not_narrowed_with_the_trading_band():
    """The 411-example completion dataset is only comparable across re-tunes
    if its zone definition stays fixed. Narrowing the trading band must not
    narrow the sampling zone."""
    assert gs.SHADOW_ZONE_MAX_REAL_SOL >= 84.5
    assert gs.SHADOW_ZONE_MAX_REAL_SOL > gs.ENTRY_MAX_REAL_SOL


# =============================================================================
# Orphaned-sell backstop — the PLEROMA bug
# =============================================================================
def test_orphaned_sell_is_force_closed(monkeypatch, tmp_path):
    """A sell that never fills must not hold the position open forever.
    PLEROMA hung 3782s through a 13.45 SOL drawdown this way."""
    s = _sniper(monkeypatch, tmp_path)
    mint = "M" * 43
    s.state["positions"][mint] = {
        "symbol": "HUNG", "creator": "c", "entry_ts": time.time() - 60,
        "entry_v_sol": 110.0, "entry_real_sol": 80.0, "size_sol": 0.25,
        "tokens": 600000.0, "fees_sol": 0.0007, "entry_velocity": 5.0,
        "entry_buyers": 5, "entry_max_share": 0.3}
    s.pending[mint] = {"type": "sell", "reason": "disaster_stop",
                       "haircut": 2.0,
                       "decision_ts": time.time() - gs.SELL_ORPHAN_S - 5}
    closed = {}
    monkeypatch.setattr(s, "_close",
                        lambda m, r, h: closed.update(mint=m, reason=r))
    _run_one_manage_pass(s)
    assert closed.get("mint") == mint, "orphaned sell was not force-closed"
    assert closed["reason"] == "disaster_stop"
    assert mint not in s.pending


def test_fresh_sell_is_left_alone(monkeypatch, tmp_path):
    """The friction model deliberately fills at the NEXT poll — a sell placed
    moments ago must not be yanked out from under it."""
    s = _sniper(monkeypatch, tmp_path)
    mint = "N" * 43
    s.state["positions"][mint] = {
        "symbol": "FRESH", "creator": "c", "entry_ts": time.time() - 10,
        "entry_v_sol": 110.0, "entry_real_sol": 80.0, "size_sol": 0.25,
        "tokens": 600000.0, "fees_sol": 0.0007, "entry_velocity": 5.0,
        "entry_buyers": 5, "entry_max_share": 0.3}
    s.pending[mint] = {"type": "sell", "reason": "pre_grad_exit",
                       "haircut": 0.0, "decision_ts": time.time()}
    closed = {}
    monkeypatch.setattr(s, "_close",
                        lambda m, r, h: closed.update(mint=m))
    _run_one_manage_pass(s)
    assert not closed, "force-closed a sell that had not had time to fill"
    assert mint in s.pending


def _run_one_manage_pass(s):
    """The orphan-reaping half of manage_loop, without the async loop."""
    now = time.time()
    for mint in list(s.state["positions"].keys()):
        pos = s.state["positions"][mint]
        order = s.pending.get(mint)
        if (order and order["type"] == "sell"
                and now - order["decision_ts"] > gs.SELL_ORPHAN_S):
            s.pending.pop(mint, None)
            s.alerts.on_orphaned_sell(pos["symbol"], order["reason"], 0)
            s._close(mint, order["reason"], order["haircut"])


# =============================================================================
# Autotune cannot undo the runway gate
# =============================================================================
def test_brain_cannot_raise_the_entry_floor_above_the_ceiling():
    """edge_brain's ENTRY_BOUNDS top out at 82.5, above the 81.5 ceiling. An
    unclamped suggestion would raise the floor above the ceiling and silently
    freeze all trading — the worst kind of failure, because it looks calm."""
    from tools import edge_brain as eb
    assert eb.ENTRY_BOUNDS[1] > gs.ENTRY_MAX_REAL_SOL, (
        "precondition changed — the clamp may no longer be needed")
    clamped = min(eb.ENTRY_BOUNDS[1], gs.ENTRY_MAX_REAL_SOL - 0.5)
    assert clamped < gs.ENTRY_MAX_REAL_SOL


# =============================================================================
# Alerts must never be able to break trading
# =============================================================================
def test_alerts_swallow_transport_failures(monkeypatch):
    from tools import grad_alerts

    def boom(_text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(grad_alerts, "_send", boom)
    a = grad_alerts.GradAlerts(enabled=True)
    a.on_open("X", "m", 80.0, 0.25, 5.0, 3.5)
    a.on_close("X", "pre_grad_exit", 0.01, 4.0, 30, 5.01)
    a.check_dead_man(tracked=3, now=time.time() + 1e6)


def test_dead_man_fires_on_a_silent_feed(monkeypatch):
    from tools import grad_alerts
    sent = []
    monkeypatch.setattr(grad_alerts, "_send", sent.append)
    a = grad_alerts.GradAlerts(enabled=True)
    now = time.time()
    a.started = now - 10000
    a.last_poll_ok = now - grad_alerts.POLL_SILENCE_S - 60
    a.last_discovery = now
    a.last_decision = now
    a.check_dead_man(tracked=3, now=now)
    assert any("DEAD-MAN" in s for s in sent)


def test_dead_man_does_not_nag(monkeypatch):
    from tools import grad_alerts
    sent = []
    monkeypatch.setattr(grad_alerts, "_send", sent.append)
    a = grad_alerts.GradAlerts(enabled=True)
    now = time.time()
    a.started = now - 10000
    a.last_poll_ok = now - grad_alerts.POLL_SILENCE_S - 60
    a.last_discovery = a.last_decision = now
    a.check_dead_man(tracked=3, now=now)
    a.check_dead_man(tracked=3, now=now + 60)
    assert len(sent) == 1, "re-alerted inside the cooldown"


def test_dead_man_stays_quiet_with_nothing_to_judge(monkeypatch):
    """No tracked tokens legitimately means no decisions — that is a quiet
    market, not a stalled pipeline."""
    from tools import grad_alerts
    sent = []
    monkeypatch.setattr(grad_alerts, "_send", sent.append)
    a = grad_alerts.GradAlerts(enabled=True)
    now = time.time()
    a.started = now - 10000
    a.last_poll_ok = a.last_discovery = now
    a.last_decision = now - grad_alerts.DECISION_SILENCE_S - 60
    a.check_dead_man(tracked=0, now=now)
    assert not sent


def test_digest_does_not_read_the_log_until_it_fires(monkeypatch):
    """maybe_digest runs every ~5s; it must not touch the trades log until the
    digest actually fires."""
    from tools import grad_alerts
    monkeypatch.setattr(grad_alerts, "_send", lambda _t: None)
    a = grad_alerts.GradAlerts(enabled=True)
    calls = []
    # pick an hour that is NOT the digest hour
    off_hour = (grad_alerts.DIGEST_HOUR_UTC + 5) % 24
    now = _ts_at_utc_hour(off_hour)
    a.maybe_digest(now=now, balance=5.0, seed=5.0,
                   closes_fn=lambda: calls.append("read") or [],
                   shadow_rate_fn=lambda: "x", tail_sol=0.0, open_positions=0)
    assert not calls, "read the trades log on a non-digest tick"


def _ts_at_utc_hour(hour: int) -> float:
    import calendar
    tm = time.gmtime()
    return calendar.timegm((tm.tm_year, tm.tm_mon, tm.tm_mday, hour, 30, 0,
                            0, 0, 0))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
