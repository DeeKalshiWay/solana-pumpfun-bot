"""
tools/grad_alerts.py

Telegram alerting for the graduation sniper — deferred edge item #4, shipped
2026-07-22 (NEXT_SESSION_TRIGGERS.md Trigger 5, pre-approved 2026-07-16).

Three jobs:

  1. EVENTS — open / close / whipsaw (post_stop_grad), pushed as they happen.
  2. DIGEST — one daily summary: balance, win rate, exit-reason split, shadow
     completion, tail-hold ledger.
  3. DEAD-MAN — the reason this exists. The July 12 Windows Update reboot
     killed the sniper silently and cost 4 days of samples before anyone
     noticed. A process watchdog only catches a process that DIED; it cannot
     catch a process that is alive and no longer working (the PumpPortal
     `subscribeTokenTrade` failure mode, where the first 3-hour run watched 16
     graduations happen with zero entries because no events ever arrived).
     So the dead-man watches PROGRESS, not liveness:
       - no successful curve poll for POLL_SILENCE_S      -> feed is dead
       - no entry AND no skip decision for DECISION_SILENCE_S -> pipeline is
         dead (tokens are being tracked but nothing is being judged)
       - no token discovered for DISCOVERY_SILENCE_S      -> discovery is dead

Alerts degrade to no-ops when TELEGRAM_BOT_TOKEN / TELEGRAM_OWNER_CHAT_ID are
unset, and every public method swallows its own exceptions: an alerting bug
must never be able to take down the trading loop.

Process death itself stays the watchdog's job (run_graduation_forever.ps1) —
this module cannot alert about its own death. Pair the two.
"""

from __future__ import annotations

import os
import time

try:
    from logger.telegram_alerts import send_alert as _send
except Exception:                                    # pragma: no cover
    def _send(text: str) -> None:                    # alerting must be optional
        pass

# --- dead-man thresholds ----------------------------------------------------
POLL_SILENCE_S = 600.0           # 10 min without a successful curve poll
DISCOVERY_SILENCE_S = 1800.0     # 30 min without discovering any hot-zone token
DECISION_SILENCE_S = 10800.0     # 3 h with tokens tracked but nothing judged
REALERT_COOLDOWN_S = 3600.0      # re-nag at most hourly per condition
DIGEST_HOUR_UTC = 0              # daily digest at 00:00 UTC


def _fmt_sol(x: float) -> str:
    return f"{x:+.4f} SOL"


class GradAlerts:
    """Owns alert state. One instance per sniper process."""

    def __init__(self, enabled: bool | None = None):
        if enabled is None:
            enabled = os.environ.get("GRAD_ALERTS", "1") != "0"
        self.enabled = enabled
        now = time.time()
        # progress clocks — updated by the sniper as work actually happens
        self.last_poll_ok = now
        self.last_discovery = now
        self.last_decision = now
        self.started = now
        self._fired: dict[str, float] = {}
        self._last_digest_day: str | None = None

    # ---------------- plumbing ----------------
    def _send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            _send(text)
        except Exception:
            pass        # never let an alert failure reach the trading loop

    def _once(self, key: str, text: str) -> None:
        """Send, then stay quiet on this condition for REALERT_COOLDOWN_S."""
        now = time.time()
        if now - self._fired.get(key, 0.0) < REALERT_COOLDOWN_S:
            return
        self._fired[key] = now
        self._send(text)

    def _clear(self, key: str) -> None:
        self._fired.pop(key, None)

    # ---------------- progress clocks ----------------
    def note_poll(self) -> None:
        self.last_poll_ok = time.time()
        self._clear("poll_silent")

    def note_discovery(self) -> None:
        self.last_discovery = time.time()
        self._clear("discovery_silent")

    def note_decision(self) -> None:
        """An entry or a skip — proof the judging pipeline ran."""
        self.last_decision = time.time()
        self._clear("decision_silent")

    # ---------------- trade events ----------------
    def on_open(self, symbol: str, mint: str, real_sol: float, size_sol: float,
                velocity: float, runway: float) -> None:
        self._send(
            f"🟢 <b>OPEN {symbol}</b>\n"
            f"entry real_sol {real_sol:.2f} · runway {runway:.2f} SOL\n"
            f"size {size_sol} SOL · vel {velocity:.1f}/5m\n"
            f"<code>{mint}</code>")

    def on_close(self, symbol: str, reason: str, pnl_sol: float,
                 net_pct: float, hold_s: float, balance: float) -> None:
        icon = "✅" if pnl_sol > 0 else "🔴"
        self._send(
            f"{icon} <b>CLOSE {symbol}</b> — {reason}\n"
            f"{_fmt_sol(pnl_sol)} ({net_pct:+.1f}%) · hold {hold_s:.0f}s\n"
            f"balance {balance:.3f} SOL")

    def on_whipsaw(self, symbol: str, reason: str, secs_after: float,
                   pnl_sol: float) -> None:
        self._send(
            f"⚠️ <b>WHIPSAW {symbol}</b>\n"
            f"graduated {secs_after:.0f}s after our {reason}\n"
            f"we exited at {_fmt_sol(pnl_sol)} — this was a winner")

    def on_orphaned_sell(self, symbol: str, reason: str, age_s: float) -> None:
        self._send(
            f"🧟 <b>ORPHANED SELL {symbol}</b>\n"
            f"{reason} sat unfilled {age_s:.0f}s — forced close.\n"
            f"The mint's curve poll is failing; check the pump.fun API.")

    # ---------------- dead-man ----------------
    def check_dead_man(self, *, tracked: int, now: float | None = None) -> None:
        """Called from the manage loop. Alerts on stalled PROGRESS, which is
        what a process watchdog structurally cannot see."""
        now = now or time.time()
        if now - self.started < POLL_SILENCE_S:
            return                       # grace period on a fresh start

        quiet = now - self.last_poll_ok
        if quiet > POLL_SILENCE_S:
            self._once("poll_silent",
                       f"💀 <b>GRAD DEAD-MAN</b>\n"
                       f"No successful curve poll for {quiet / 60:.0f} min.\n"
                       f"Process is alive but the pump.fun feed is not "
                       f"answering — trading is effectively stopped.")

        quiet = now - self.last_discovery
        if quiet > DISCOVERY_SILENCE_S:
            self._once("discovery_silent",
                       f"💀 <b>GRAD DEAD-MAN</b>\n"
                       f"No hot-zone token discovered for {quiet / 60:.0f} min.\n"
                       f"Discovery poll may be failing or rate-limited.")

        # Only meaningful while there is something to judge — an empty tracker
        # set legitimately produces no decisions.
        quiet = now - self.last_decision
        if tracked > 0 and quiet > DECISION_SILENCE_S:
            self._once("decision_silent",
                       f"💀 <b>GRAD DEAD-MAN</b>\n"
                       f"{tracked} tokens tracked but no entry or skip decided "
                       f"for {quiet / 3600:.1f} h.\n"
                       f"The judging pipeline has stalled.")

    # ---------------- daily digest ----------------
    def maybe_digest(self, *, now: float | None = None, balance: float,
                     seed: float, closes_fn, shadow_rate_fn,
                     tail_sol: float, open_positions: int) -> None:
        """closes_fn/shadow_rate_fn are CALLABLES, not values — this runs on
        every manage tick (~5s) and must not touch the trades log until the
        digest actually fires."""
        now = now or time.time()
        tm = time.gmtime(now)
        day = time.strftime("%Y-%m-%d", tm)
        if tm.tm_hour != DIGEST_HOUR_UTC or self._last_digest_day == day:
            return
        self._last_digest_day = day
        closes = closes_fn()
        shadow_rate = shadow_rate_fn()
        if not closes:
            self._send(f"📊 <b>GRAD DIGEST</b> {day}\n"
                       f"balance {balance:.3f} SOL · no closed trades yet")
            return
        wins = [c for c in closes if (c.get("pnl_sol") or 0) > 0]
        net = sum(c.get("pnl_sol") or 0 for c in closes)
        by: dict[str, list] = {}
        for c in closes:
            by.setdefault(c.get("exit_reason", "?"), []).append(c)
        lines = [f"📊 <b>GRAD DIGEST</b> {day}",
                 f"balance <b>{balance:.3f} SOL</b> "
                 f"({_fmt_sol(balance - seed)} vs seed)",
                 f"{len(closes)} closed · {len(wins)} wins "
                 f"({100 * len(wins) / len(closes):.0f}%) · net {_fmt_sol(net)}",
                 f"open now: {open_positions}",
                 ""]
        for reason, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
            rnet = sum(c.get("pnl_sol") or 0 for c in rs)
            rw = sum(1 for c in rs if (c.get("pnl_sol") or 0) > 0)
            lines.append(f"  {reason}: {len(rs)} · {rw}W · {_fmt_sol(rnet)}")
        lines += ["", f"shadow completion: {shadow_rate}",
                  f"tail-hold ledger: {_fmt_sol(tail_sol)}"]
        self._send("\n".join(lines))

    # ---------------- lifecycle ----------------
    def on_start(self, *, balance: float, entry_lo: float, entry_hi: float,
                 exit_at: float) -> None:
        self._send(
            f"🚀 <b>GRAD SNIPER UP</b>\n"
            f"balance {balance:.3f} SOL · PAPER\n"
            f"scalp band [{entry_lo:.1f}, {entry_hi:.1f}) → exit {exit_at:.1f}")

    def on_kill(self, balance: float) -> None:
        self._send(f"🛑 <b>GRAD KILL SWITCH</b>\nall positions closed · "
                   f"balance {balance:.3f} SOL")
