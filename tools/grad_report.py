"""
tools/grad_report.py

One-command status report for the graduation sniper. Run when you get back:

    python -m tools.grad_report

Shows: balance, every trade taken (with entry features), win rate by exit
reason, everything the brain has learned, what's being tracked right now,
and the last activity lines from the sniper's own log.
"""

from __future__ import annotations

import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES = os.path.join(ROOT, "logs", "graduation_trades.jsonl")
STATE = os.path.join(ROOT, "logs", "graduation_state.json")
OUT = os.path.join(ROOT, "logs", "graduation_sniper.out")


def main():
    print("=" * 68)
    print("GRADUATION SNIPER — STATUS REPORT")
    print("=" * 68)

    # Account
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:
            pass
    acct = state.get("account", {})
    seed = acct.get("seed_sol", 5.0)
    realized = acct.get("realized_sol", 0.0)
    bal = seed + realized
    print(f"\nBALANCE: {bal:.4f} SOL  (seed {seed:.2f}, realized {realized:+.4f})"
          f"  ≈ ${bal * 85:.2f}")
    fees = acct.get("fees_sol", 0.0)
    if fees:
        print(f"  tx fees paid (friction model, incl. reverts): {fees:.4f} SOL")
    open_pos = state.get("positions", {})
    if open_pos:
        print(f"OPEN POSITIONS ({len(open_pos)}):")
        for mint, p in open_pos.items():
            age = (time.time() - p.get("entry_ts", 0)) / 60
            print(f"  {p.get('symbol','?'):<12} entered at real_sol="
                  f"{p.get('entry_real_sol', 0):.1f}  size={p.get('size_sol')}"
                  f"  age={age:.0f}min")

    # Trades
    opens, closes, skips, whipsaws = [], [], [], []
    shadow, tail_opens, tail_closes, tail_passes = [], [], [], []
    entry_fails = []
    if os.path.exists(TRADES):
        for line in open(TRADES, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            {"open": opens, "close": closes, "skip": skips,
             "post_stop_grad": whipsaws, "shadow_outcome": shadow,
             "entry_fail": entry_fails,
             "tail_open": tail_opens, "tail_close": tail_closes,
             "tail_pass": tail_passes}.get(r.get("event"), []).append(r)

    print(f"\nTRADES: {len(opens)} opened, {len(closes)} closed, "
          f"{len(skips)} skipped by filters/brain, "
          f"{len(entry_fails)} buys reverted in flight")

    if closes:
        wins = [c for c in closes if (c.get("pnl_sol") or 0) > 0]
        total = sum(c.get("pnl_sol") or 0 for c in closes)
        print(f"  win rate: {len(wins)}/{len(closes)} "
              f"({len(wins)/len(closes)*100:.0f}%)   net: {total:+.4f} SOL")
        by_reason: dict = {}
        for c in closes:
            r = c.get("exit_reason", "?")
            d = by_reason.setdefault(r, [0, 0.0])
            d[0] += 1
            d[1] += c.get("pnl_sol") or 0
        print("  by exit reason:")
        for r, (n, pnl) in sorted(by_reason.items()):
            print(f"    {r:<15} n={n:>3}  pnl={pnl:+.4f} SOL")
        print("\n  last 10 closes:")
        for c in closes[-10:]:
            ts = time.strftime("%m-%d %H:%M", time.localtime(c.get("ts", 0)))
            print(f"    {ts}  {c.get('symbol','?'):<12} "
                  f"{c.get('pnl_sol', 0):+.4f} SOL ({c.get('net_pct', 0):+.1f}%) "
                  f"{c.get('exit_reason','?')} hold={c.get('hold_s',0):.0f}s")

    # Realized economics vs the brain's assumed break-even (watch item
    # 2026-07-16: BREAKEVEN_WIN_RATE was derived from the old 1.5-SOL stop's
    # -6.9% loss; the 3.0 stop makes losses bigger, raising true break-even)
    recent = closes[-20:]
    wins_pct = [c.get("net_pct", 0) for c in recent
                if (c.get("pnl_sol") or 0) > 0]
    loss_pct = [c.get("net_pct", 0) for c in recent
                if (c.get("pnl_sol") or 0) <= 0]
    if wins_pct and loss_pct:
        avg_w = sum(wins_pct) / len(wins_pct)
        avg_l = sum(loss_pct) / len(loss_pct)
        true_be = abs(avg_l) / (avg_w + abs(avg_l))
        print(f"\nREALIZED ECONOMICS (last {len(recent)} closes):")
        print(f"  avg win {avg_w:+.1f}%   avg loss {avg_l:+.1f}%   "
              f"-> true break-even win rate {true_be:.0%}")
        try:
            from tools.edge_brain import BREAKEVEN_WIN_RATE
            drift = true_be - BREAKEVEN_WIN_RATE
            if abs(drift) > 0.05:
                verdict = ("UNDERSTATES risk - brain vetoes fire too late"
                           if drift > 0 else
                           "OVERSTATES risk - brain vetoes fire too early")
                print(f"  [!] brain assumes {BREAKEVEN_WIN_RATE:.0%} "
                      f"(drift {drift:+.0%}) - {verdict}. "
                      f"Consider updating BREAKEVEN_WIN_RATE in edge_brain.py")
            else:
                print(f"  brain assumes {BREAKEVEN_WIN_RATE:.0%} - "
                      f"within tolerance")
        except Exception:
            pass

    # Whipsaw monitor — post-stop outcome tagging (deployed 2026-07-16)
    WHIPSAW_TRACKING_SINCE = 1784267400.0
    era_stops = [c for c in closes
                 if c.get("exit_reason") in ("stall_stop", "timeout",
                                             "disaster_stop")
                 and c.get("ts", 0) >= WHIPSAW_TRACKING_SINCE]
    pending = state.get("recent_stops", {})
    if era_stops or whipsaws or pending:
        n_whip = len(whipsaws)
        left = sum(abs(w.get("stopped_pnl_sol") or 0) for w in whipsaws)
        print(f"\nWHIPSAW MONITOR (stops that later graduated):")
        print(f"  {n_whip} of {len(era_stops)} tagged stops graduated after "
              f"we bailed ({len(pending)} still pending, 24h window)")
        if n_whip:
            print(f"  SOL lost to whipsaws: {left:.4f} "
                  f"(these were winners exited at a loss)")
        for w in whipsaws[-5:]:
            ts = time.strftime("%m-%d %H:%M", time.localtime(w.get("ts", 0)))
            print(f"    {ts}  {w.get('symbol','?'):<12} graduated "
                  f"{w.get('stop_to_grad_s', 0):.0f}s after "
                  f"{w.get('stop_reason','?')} "
                  f"({w.get('stopped_pnl_sol', 0):+.4f} SOL)")

    # Shadow completion dataset — every watched token that hit the 80-SOL
    # decision zone, traded or not (deployed 2026-07-16)
    if shadow:
        grads = [s for s in shadow if s.get("outcome") == "graduated"]
        rate = len(grads) / len(shadow)
        print(f"\nSHADOW COMPLETION (all tokens that hit the decision zone):")
        print(f"  {len(grads)}/{len(shadow)} graduated ({rate:.0%})   "
              f"[thesis needs ~65-77% depending on stop economics]")
        by_hour: dict = {}
        for s in shadow:
            h = (s.get("snap") or {}).get("hour_utc")
            if h is None:
                continue
            d = by_hour.setdefault(h, [0, 0])
            d[0] += 1
            d[1] += int(s.get("outcome") == "graduated")
        rows = [(h, d[0], d[1]) for h, d in sorted(by_hour.items())
                if d[0] >= 3]
        if rows:
            print("  completion by UTC hour (n>=3):")
            for h, n, g in rows:
                print(f"    {h:02d}:00  n={n:>3}  grad={g / n:.0%}")

    # Tail-hold (post-migration bounce, paper — deployed 2026-07-16)
    if tail_opens or tail_closes or tail_passes:
        tail_bal = state.get("tail", {}).get("realized_sol", 0.0)
        print(f"\nTAIL-HOLD (post-migration bounce, paper):")
        print(f"  watches: {len(tail_opens)} entered, "
              f"{len(tail_passes)} passed (no setup/feed), "
              f"ledger {tail_bal:+.4f} SOL")
        if tail_closes:
            t_wins = [c for c in tail_closes if (c.get("pnl_sol") or 0) > 0]
            t_net = sum(c.get("pnl_sol") or 0 for c in tail_closes)
            print(f"  closed: {len(t_wins)}/{len(tail_closes)} wins, "
                  f"net {t_net:+.4f} SOL")
            for c in tail_closes[-5:]:
                ts = time.strftime("%m-%d %H:%M",
                                   time.localtime(c.get("ts", 0)))
                print(f"    {ts}  {c.get('symbol','?'):<12} "
                      f"{c.get('pnl_sol', 0):+.4f} SOL "
                      f"({c.get('net_pct', 0):+.1f}%) "
                      f"{c.get('exit_reason','?')} "
                      f"hold={c.get('hold_s', 0):.0f}s")

    if skips:
        print(f"\n  last 5 skips:")
        for s in skips[-5:]:
            ts = time.strftime("%m-%d %H:%M", time.localtime(s.get("ts", 0)))
            print(f"    {ts}  {s.get('symbol','?'):<12} {s.get('reason','?')}")

    # Brain
    print("\nBRAIN:")
    try:
        from tools.edge_brain import EdgeBrain
        for line in EdgeBrain().report().split("\n"):
            print(f"  {line}")
    except Exception as e:
        print(f"  (brain unavailable: {e})")

    # Live process activity
    if os.path.exists(OUT):
        print("\nLAST SNIPER ACTIVITY:")
        lines = [l.rstrip() for l in open(OUT, encoding="utf-8",
                                          errors="ignore") if l.strip()]
        for l in lines[-8:]:
            print(f"  {l}")
        age_s = time.time() - os.path.getmtime(OUT)
        status = "RUNNING" if age_s < 180 else f"⚠️ NO OUTPUT FOR {age_s/60:.0f} MIN — may be down"
        print(f"\nPROCESS: {status}")

    print("=" * 68)


if __name__ == "__main__":
    main()
