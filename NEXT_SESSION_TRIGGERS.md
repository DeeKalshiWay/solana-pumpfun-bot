# Next-Session Triggers

**For Claude (or me) reading this at the start of a future session:**
Scan this file. If any trigger condition is met, surface it to the operator before doing other work. These are pre-approved work items waiting on objective signals.

---

## Trigger 1 — Jito Bundles + `jitodontfront`

### Wait condition

ANY of:
- Wallet has reached **≥4 SOL** (doubled from $400 / 2 SOL starting capital)
- **100+ trades** have closed at the new config (0.06 SOL/trade, TP 50/100/500%, rug memory live) AND the trade-DB shows **positive cumulative PnL_sol** over those 100 trades
- Operator explicitly says "ship Jito bundles" overriding the gate

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
print(f'Trigger 1 fires if:        wallet >= 4 SOL  OR  (>= 100 trades AND PnL > 0)')
print(f'  Wallet condition:        {\"MET\" if balance >= 4 else \"not met\"}')
print(f'  Trade-count condition:   {\"MET\" if len(new_trades) >= 100 and new_pnl > 0 else \"not met\"}')
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
- Wallet ≥ 5 SOL

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

Operator prompts that bypass triggers:
- "ship Jito bundles" → Trigger 1
- "ship Option 2 LaserStream scaffold" → Trigger 2
- "fix the TP partial-sell PnL tracking" → Trigger 3
- "ship rugcheck pre-buy" → Trigger 4
