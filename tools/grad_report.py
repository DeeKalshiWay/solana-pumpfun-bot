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
    open_pos = state.get("positions", {})
    if open_pos:
        print(f"OPEN POSITIONS ({len(open_pos)}):")
        for mint, p in open_pos.items():
            age = (time.time() - p.get("entry_ts", 0)) / 60
            print(f"  {p.get('symbol','?'):<12} entered at real_sol="
                  f"{p.get('entry_real_sol', 0):.1f}  size={p.get('size_sol')}"
                  f"  age={age:.0f}min")

    # Trades
    opens, closes, skips = [], [], []
    if os.path.exists(TRADES):
        for line in open(TRADES, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            {"open": opens, "close": closes, "skip": skips}.get(
                r.get("event"), []).append(r)

    print(f"\nTRADES: {len(opens)} opened, {len(closes)} closed, "
          f"{len(skips)} skipped by filters/brain")

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
