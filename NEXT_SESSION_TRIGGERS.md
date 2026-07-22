# Next-Session Triggers

**For Claude (or me) reading this at the start of a future session:**
Scan this file. If any trigger condition is met, surface it to the operator before doing other work. These are pre-approved work items waiting on objective signals.

---

## Trigger 1 — Jito Bundles + `jitodontfront`

### Wait condition

ANY of:
- Wallet has reached **≥7 SOL** (+50% from ~4.7 SOL $400-at-$85/SOL starting capital)
- **100+ trades** have closed at the new config (0.10 SOL/trade, TP 50/100/500%, rug memory live) AND the trade-DB shows **positive cumulative PnL_sol** of at least **+0.3 SOL** over those 100 trades
- Operator explicitly says "ship Jito bundles" overriding the gate

> Threshold corrected 2026-05-10: previous version used 4 SOL based on $200/SOL
> assumption (would have fired below starting balance at the actual $85/SOL price).

### How to verify the trigger fired

```bash
cd /c/Users/denni/Downloads/pump_bot/pump_bot && python -c "
import json, asyncio
from trader.wallet import SolanaWallet

async def chk():
    w = SolanaWallet(); await w.start()
    bal = await w.get_sol_balance()
    await w.stop()
    return bal

balance = asyncio.run(chk())
trades = []
with open('logs/closed_trades.jsonl', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            try: trades.append(json.loads(line))
            except: pass

# Filter to trades after the 0.06-sizing config (entry timestamp after 2026-05-09 UTC)
NEW_CONFIG_START = 1778760000  # adjust to actual deploy ts when known
new_trades = [t for t in trades if t.get('entry_time', 0) >= NEW_CONFIG_START]
new_pnl = sum(t.get('pnl_sol', 0) for t in new_trades)

print(f'Wallet:                    {balance:.4f} SOL')
print(f'Trades since new config:   {len(new_trades)}')
print(f'PnL since new config:      {new_pnl:+.4f} SOL')
print(f'Trigger 1 fires if:        wallet >= 7 SOL  OR  (>= 100 trades AND PnL >= +0.3 SOL)')
print(f'  Wallet condition:        {\"MET\" if balance >= 7 else \"not met\"}')
print(f'  Trade-count condition:   {\"MET\" if len(new_trades) >= 100 and new_pnl >= 0.3 else \"not met\"}')
"
```

### Work to do once triggered

1. `pip install jito-py-rpc`
2. Build `trader/jito_bundle.py`:
   - Buy tx + tip-transfer-to-Jito-tip-account in a 2-tx bundle
   - Use `https://slc.mainnet.block-engine.jito.wtf/api/v1/bundles` (west coast operator)
   - Submit via `JitoJsonRpcSDK.send_bundle()`
   - Poll `get_bundle_statuses` for `confirmed`/`finalized`
3. Hook into `trader/executor.py` as a NEW path (don't replace existing — race against it):
   - `executor.buy_via_jito_bundle()` alongside `executor.buy()`
   - Race both, first to confirm wins
4. Add `jitodontfront111111111111111111111111111111` pubkey to buy tx instructions for sandwich protection (free, costs nothing extra)
5. Test with `simulateBundle` first before real submission
6. Pre-flight checklist (analogous to the executor.sell bug audit):
   - Verify `TradeExecutor.buy_via_jito_bundle()` signature matches `PaperExecutor` analog
   - Verify return shape matches existing `buy()` so downstream code doesn't break
7. Keep multi-RPC race for sells (atomicity less critical there)

### Estimated effort

4-6 hours focused work. Same risk class as the executor.sell bug — be paranoid, run all tests in `PRE_LIVE_400_PLAYBOOK §6 testing plan` before declaring done.

### Why we deferred

At $400 stake / 0.06 SOL trades, sandwich attacks are below MEV economic threshold and the multi-RPC race covers ~80% of bundle's latency benefit. The fee savings (~0.003-0.005 SOL/trade) only become meaningful at higher trade volume — which only matters if the strategy has demonstrated edge first.

---

## Trigger 2 — LaserStream gRPC Detection (Option 2 scaffold)

### Wait condition

Operator says "ship Option 2 LaserStream scaffold per the plan."

### Reference

See conversation history from 2026-05-10 on the option breakdown. Three paths discussed:
- Option 1: Defer
- **Option 2: Scaffold tonight in dump-only mode** (chosen — but operator said "save for later")
- Option 3: Full ship

Goal: add `detector/laserstream_monitor.py` in dump-only mode (writes events to `logs/laserstream_raw.jsonl` for inspection, does NOT push to `raw_queue`). Confirms gRPC connection works. Parser written next session against captured real data.

### Work

See conversation transcript for full plan. Brief:
1. Generate Yellowstone protobuf stubs from `github.com/rpcpool/yellowstone-grpc`
2. `detector/laserstream_monitor.py` with `dump_only=True` hardcoded
3. Add `LASERSTREAM_GRPC_URL` and `LASERSTREAM_X_TOKEN` to `.env` template
4. Conditional task launch in `main.py` — only if creds set
5. Helper `tools/laserstream_inspect.py` to pretty-print captured messages

### Risk

Low. dump-only mode means zero impact on trade pipeline. Fallback if grpcio install fails on Windows: bot keeps running on PumpPortal as before.

---

## Trigger 3 — TP partial-sell PnL fix (D1)

### Wait condition

NONE — this is a known bug. Ship whenever there's bandwidth. **Lower priority than Triggers 1-2** because it affects bookkeeping accuracy, not trade outcomes.

### What's wrong

`closed_trades.jsonl` records `pnl_sol = sell_result.sol_received - pos.sol_invested` from only the FINAL sell. For staged exits (TP1 → TP2 → final), TP1+TP2 receipts are silently uncaptured. Wallet balance is correct (SOL physically arrives) but per-trade PnL is underreported, biasing auto-tuner WR low.

### Fix

1. Add `realized_sol: float = 0` to `Position` dataclass
2. After every successful TP partial sell, capture `sol_received` via `_sol_delta_from_tx(result['signature'])` and add to `pos.realized_sol`
3. In `close_position`, compute `pnl_sol = sell_result.sol_received + pos.realized_sol - original_sol_invested`
4. Note: original_sol_invested needs to NOT be reduced by the `pos.sol_invested *= (1 - sell_fraction)` line, OR we need to track both

### Estimated effort

~30 min focused, ~1 hour with proper testing.

---

## Trigger 4 — Rugcheck.xyz pre-buy gate (Scrapling integration)

### Wait condition

Both:
- Trigger 1 has fired AND been shipped (Jito bundles deployed)
- Wallet ≥ 9 SOL (~$765 at $85/SOL — proven 2× from starting capital)

### Work

Per `COMPETITIVE_GAP_ANALYSIS.md` discussion. Add `analyzer/rugcheck.py`:
- `Fetcher` (HTTP-only, no browser) for ~200-400ms scrape
- Gate: only used by Smart Caller queue, NEVER hot path
- Caches per-mint for 1 hour

### Why deferred so far down

Adds ~200-400ms latency. Until the rest of the execution stack is sub-second, this would push us further from the <150ms elite target. Only valuable once base latency is solved.

---

## How to use this file

When operator returns and starts a new session:

1. Future me: read this file first.
2. Run the trigger-1 verification script.
3. If any condition is MET, surface it: "Hey, Trigger N has fired — ready to ship the work it gates?"
4. If none fired: don't bring it up unless asked.

## Trigger 5 — Graduation sniper Telegram alerts — ✅ SHIPPED 2026-07-22

Built in `tools/grad_alerts.py`, wired into `tools/graduation_sniper.py`.
Covers all three items from the original spec:

- **Events**: open / close / whipsaw (`post_stop_grad`), plus a new
  orphaned-sell alert (see the PLEROMA bug in the scalp-rewire notes).
- **Daily digest** at 00:00 UTC: balance, win rate, exit-reason split, shadow
  completion, tail-hold ledger. Reads the trades log lazily — only when the
  digest actually fires, never on the 5-second manage tick.
- **Dead-man**, watching PROGRESS rather than liveness, which is the failure
  mode a process watchdog structurally cannot see:
  - no successful curve poll for 10 min → feed is dead
  - no hot-zone discovery for 30 min → discovery is dead
  - tokens tracked but nothing judged for 3 h → judging pipeline stalled
  Re-alerts at most hourly per condition. Silent when there is nothing to
  judge (an empty tracker set is a quiet market, not a fault).

`TELEGRAM_BOT_TOKEN` / `TELEGRAM_OWNER_CHAT_ID` verified present in `.env`.
Set `GRAD_ALERTS=0` to mute. Alerts swallow all their own exceptions —
`tests/test_graduation_scalp.py` asserts an alert-transport failure cannot
reach the trading loop.

Still the watchdog's job: alerting on the process being **dead**. This module
cannot report its own death. Keep `run_graduation_forever.ps1` paired with it.

<details>
<summary>Original trigger spec (for reference)</summary>

### Wait condition

ANY of:
- Operator asks about the graduation sniper's status/results in a future session
  (remind them this is pending before reporting)
- The graduation sniper is found dead/silent again (this is exactly what alerts prevent —
  it already cost 4 days of samples when the July 12 Windows Update reboot killed it silently)
- Graduation strategy goes LIVE with real SOL (alerts become risk management, not QoL — do not go live without them)
- Operator explicitly says "ship graduation telegram alerts"

### What to build (~1 hour, pre-approved 2026-07-16)

Wire `tools/graduation_sniper.py` + `tools/grad_tail.py` into the existing
`logger/telegram_alerts.py` module (already used by the main bot):
- open/close/whipsaw (`post_stop_grad`) events in real time
- daily digest (balance, win rate, shadow completion rate, tail-hold ledger)
- **dead-man alert**: no heartbeat for 10 min, or zero entries AND zero skips
  for N hours during active market hours (catches the PumpPortal-style silent
  degradation a process watchdog cannot)

### How to verify the trigger fired

```bash
cd /c/Users/denni/OneDrive/Desktop/pump.bot2.0 && python -m tools.grad_report
# if PROCESS shows "NO OUTPUT FOR ..." -> the dead-man condition already fired
```

</details>

---

## Trigger 6 — Scalp-only readout (opened 2026-07-22)

### Wait condition

**30 closed trades** at the scalp-only config (band [80.0, 81.0), exit 83.5,
stall-stop off, disaster 5.0). That is the point at which the win rate has a
usable standard error.

### What to check

The rewire is a bet on a specific, falsifiable claim: that the book lost on
exit structure rather than on entry quality. The evidence for it was that 88%
of entered mints graduated while the book still lost 0.77 SOL. If that claim is
right, these should all move together:

| metric | before | expected after |
|---|---|---|
| win rate | 33% | > 60% |
| migration exits | 30% of closes | < 10% |
| stall stops | 15 (-0.343 SOL) | 0 (disabled) |
| avg win | +1.7% | +2.0% to +3.3% |
| entries with no runway | 11 of 55 | 0 |

**If the win rate is above 60% but the book is still negative**, the tail is the
problem, not the structure — look at the disaster-stop fills, and consider
whether 0.25 SOL is too large for a 7% death rate.

**If the win rate is still near 33%**, the entry-quality claim was wrong and the
strategy should be halted rather than tuned further. Do not add gates.

```bash
cd /c/Users/denni/OneDrive/Desktop/pump.bot2.0 && python -m tools.grad_report
```

---

## Trigger 7 — Wire go_live_gate to the graduation book (opened 2026-07-22)

`analytics/go_live_gate.py` reads the MAIN bot's trade DB and reports
"no trades on disk yet" for criteria 1-3. It has never evaluated the
graduation strategy, so the gate that is supposed to guard going live is blind
to the only strategy being actively developed. Wire it to
`logs/graduation_trades.jsonl` before any live-capital discussion.

No wait condition — ship whenever there is bandwidth. Blocking for go-live.

---

Operator prompts that bypass triggers:
- "ship Jito bundles" → Trigger 1
- "ship Option 2 LaserStream scaffold" → Trigger 2
- "fix the TP partial-sell PnL tracking" → Trigger 3
- "ship rugcheck pre-buy" → Trigger 4
- ~~"ship graduation telegram alerts" → Trigger 5~~ (shipped 2026-07-22)
- "read out the scalp results" → Trigger 6
- "wire the go-live gate to graduation" → Trigger 7
