# Competitive Gap Analysis — Successful Bots vs Ours

**Method**: Read READMEs of 4 actively-developed Solana memecoin bots and the broader open-source landscape (~10 repos surveyed). This is what *successful* public bots have that ours doesn't, and conversely what we have that they don't.

**Reference repos compared**:
- [`chainstacklabs/pumpfun-bonkfun-bot`](https://github.com/chainstacklabs/pumpfun-bonkfun-bot) — infrastructure-grade, well-documented, multi-platform (pump.fun + bonk.fun)
- [`outsmartchad/solana-trading-cli`](https://github.com/outsmartchad/solana-trading-cli) — most comprehensive feature set, 18 DEX adapters, 12 tx providers
- [`mhasner/memecoincopybot`](https://github.com/mhasner/memecoincopybot) — Rust copy-trading bot with Geyser + Jito
- [`vvizardev/pumpfun-laserstream-sniper-rust`](https://github.com/vvizardev/pumpfun-laserstream-sniper-rust) — Rust LaserStream sniper
- [`drixindustries/Rug-Killer-On-Solana`](https://github.com/drixindustries/Rug-Killer-On-Solana) — anti-rug detection layer
- [`machenxi/rugpull-scam-token-detection`](https://github.com/machenxi/rugpull-scam-token-detection) — RugWatch, real-time honeypot detection

---

## 🔴 What successful bots have that we DON'T

### Tier 1 — Direct revenue/loss impact

| Gap | What it is | Who has it | Our state | Effort to close |
|-----|------------|------------|-----------|-----------------|
| **gRPC Geyser detection** | Yellowstone protocol direct stream from validator. ~200ms vs our ~300-500ms via PumpPortal WS aggregator | chainstack, outsmart, mhasner, all rust sniper bots | PumpPortal WS only (aggregator adds latency) | ~3-4 hours, needs Helius LaserStream tier |
| **Real Jito bundles** | Atomic buy + tip tx submitted as bundle, guaranteed inclusion in next slot | outsmart, mhasner | Block Engine sendTransaction only (single tx, no atomicity) | ~1 day rewrite (off PumpPortal Local Tx API) |
| **12-provider tx race** | outsmart-cli sends to: Jito, bloXroute, Helius, Nozomi, Blockrazor, NextBlock, 0slot, Soyas, Astralane, Stellium, Flashblock, Node1 | outsmart-cli | 1-3 providers (just added EXTRA_RPC_URLS) | ~1 day to add and test all providers |
| **Direct tx construction** | Build pump.fun buy/sell instructions yourself with correct discriminators. No PumpPortal dependency. | chainstack, outsmart, all rust bots | PumpPortal Local Tx API dependency = single point of failure | ~1 day, needs Anchor/Solana skill |
| **Copy trading from KOL wallets** | Follow profitable wallets, replicate their trades. mhasner data: **54% same-block, 29% next-block, 83% total inclusion rate** | outsmart, mhasner (core feature), HZCX404 | Not implemented | ~3-4 hours (we already track wallet activity in wallet_intel) |
| **Honeypot detection** | Simulate test trades to confirm sells work. Authority checks (mint, freeze). LP burn verification. Buy/sell tax analysis. | RugCheck.xyz, RugWatch, Rug-Killer | Partial (freeze authority sensor only) | ~4-6 hours |

### Tier 2 — Capability/coverage gaps

| Gap | What | Our state | Effort |
|-----|------|-----------|--------|
| **Multi-DEX support** | outsmart-cli: 18 DEXes (Raydium variants, Meteora DLMM, Orca, Byreal, PancakeSwap, Jupiter, etc.) | pump.fun + Jupiter (graduated tokens only) | Significant; out of pump.fun-niche scope |
| **Durable nonces** | Tx deduplication during retries; protects against double-execution | Not used | ~1 hour |
| **Multi-wallet execution** | Spread trades across N wallets to avoid bot-detection on copy trades | Single wallet | ~1 day |
| **Real-time pool state cache** | Cache bonding-curve state across all observed mints for instant access | Partial (only positions we hold) | ~2-3 hours |
| **AI/ML rug detection** | Rug-Killer uses "Temporal GNN v2 for 10-18% better rug detection" + SyraxML | Pattern-matching only (rug_memory bins) | Significant — would need real ML |

### Tier 3 — Nice-to-haves

- **PostgreSQL backend** (outsmart-cli) — for serious analytical work. We use JSONL+SQLite which is fine for scale we're at.
- **Volume bot capabilities** (HZCX404) — adversarial use, not relevant.
- **Bundling launch capabilities** (hexnome) — creating coordinated launches, not relevant.

---

## 🟢 What WE have that they DON'T

This is genuine differentiation. None of the public bots I surveyed have these:

| Feature | What it does | Why it's unique |
|---------|--------------|-----------------|
| **Counterfactual logging** | Records every rejected signal + polls outcome 10 min later. Tells us: "did our filter prune rugs or kill winners?" | Zero other bots do this. It's the only path to *measuring* our edge. 8,777 records and growing. |
| **Rug pattern memory** | Feature-signature buckets of rugged tokens. Score-penalty on matching candidates. | None of the surveyed bots have a learning loop that adapts from observed rugs. |
| **Passive rug feed** (just shipped) | When a rejected token rugs, we still record the pattern — the bot learns even when wallet is empty. | Unique. Means our learning loop runs 24/7 regardless of capital. |
| **Auto-tuner with rolling WR** | Adjusts MIN_BUY_SCORE up/down based on recent 100-trade WR. Bounded ±3 to ±5 from base. | Most bots use static thresholds. None I saw self-adjust. |
| **Bot-wallet identification by mint count** | Wallets that bought ≥25 distinct mints are flagged as snipers. 384+ tracked in our DB. | Other bots filter by token features; we filter by *buyer* features. |
| **Bundle detector** (when it works) | 2+ early buyers in 4s = bundled launch | chainstack mentions, doesn't implement. We have it (currently dormant — see PRE_LIVE_400_PLAYBOOK §5/D2). |
| **Comprehensive web dashboard** | MtM, positions, signals, creators, intel, learn, report tabs with 2-sec refresh, emergency stop, per-position SELL | Most public bots are CLI-only. Dashboard makes operational use far easier. |
| **Telegram cockpit** | Bot for /status /positions /buy /sell /watch /unwatch + alerts on close + IFTTT relay for influencer feeds | Standalone tool, not common. |
| **Daily email reports** (just shipped) | Midnight summary: trades, learning deltas, counterfactual analysis, auto-tuner state | Unique — none have this. |
| **MtM dashboard math** | wallet flat + position fair value, not just realized PnL | Most just show realized; we show both. |

---

## 🎯 Strategic recommendations

Ranked by ROI:

### MUST FIX before $400 deployment
*(in addition to the audit fixes already shipped)*

1. **Activate the latency stack** — Helius LaserStream subscription + EXTRA_RPC_URLS in `.env`. Already-built code, just needs activation. **Without this, our Stage-1 detection lags ~200-400ms behind the rust bots.** They will outbid us on hot launches.

### HIGH ROI for next session

2. **Direct gRPC Geyser detection** (~3-4 hr work). Helius LaserStream gRPC. Drops detection latency 200-400ms. **Single biggest latency win available without changing tx construction.**

3. **Real Jito bundles** (~1 day). Atomic execution + tip tx. Eliminates "tx stuck in mempool" tail latency. Requires switching off PumpPortal Local Tx API.

4. **Copy-trading mode** (~3-4 hr). Pivot or augment current strategy: track top winners (we already have wallet_intel data), follow their fresh buys with a small lag. mhasner's 83% inclusion rate at 54% same-block is the kind of edge worth chasing.

### MEDIUM ROI

5. **Honeypot simulation** (~4-6 hr). Before-buy: simulate a sell tx with `simulateTransaction` RPC call. If it would fail (transfer hook blocking sells), reject. Catches honeypots that authority-only checks miss.

6. **LP burn verification** (~2 hr) — fetch the LP account, check if frozen / burned. Not all pump.fun tokens have this concept yet but post-migration tokens do.

7. **Multi-provider tx submission** (~1 day, polish). Add bloXroute, Nozomi, NextBlock as paid additions to multi-RPC race. Each one is ~$50-100/mo but stacks.

### LOW ROI / DEFER

- **Multi-DEX support** — out of niche scope. Stay focused on pump.fun.
- **AI/ML rug detection** — our rug_memory pattern-bucket approach is simpler and traceable. Real ML adds black-box risk.
- **Volume booster / bundle creator** — adversarial use, not aligned with strategy.

---

## 🧠 Strategic insight from the comparison

Most successful pump.fun bots are **execution-optimized** (latency, MEV, multi-provider). Our bot is **intelligence-optimized** (counterfactual, rug memory, auto-tuner, bot-wallet ID, creator tracker). 

That's a different bet:
- **Their bet**: "we'll outrun the field on every signal."
- **Our bet**: "we'll only enter signals where the math is on our side, and learn from every observation."

**At $400 the intelligence bet has merit BUT only if execution is fast enough that we land the trades we identify.** With current latency stack (no LaserStream gRPC, no Jito bundles), we'll lose to faster bots on the hottest launches. That biases us toward slightly-slower-but-still-good signals — which is fine, but is what the strategy is implicitly doing already.

**The minimum execution upgrade for $400 deployment**: Helius LaserStream + 3-RPC race + Jito Block Engine endpoint. This is achievable today (already-built code, just needs `.env` activation per `PRE_LIVE_400_PLAYBOOK §1.3`).

**The maximum execution upgrade with one focused next-session day**: gRPC Geyser direct + real Jito bundles. Brings us to parity with the Rust pack on the hot path.

---

## Sources

- [chainstacklabs/pumpfun-bonkfun-bot](https://github.com/chainstacklabs/pumpfun-bonkfun-bot)
- [outsmartchad/solana-trading-cli](https://github.com/outsmartchad/solana-trading-cli)
- [mhasner/memecoincopybot](https://github.com/mhasner/memecoincopybot)
- [vvizardev/pumpfun-laserstream-sniper-rust](https://github.com/vvizardev/pumpfun-laserstream-sniper-rust)
- [Niranjanprasad1/Solana-Memecoin-Trading-Bot](https://github.com/Niranjanprasad1/Solana-Memecoin-Trading-Bot)
- [HZCX404/memecoin-trading-bots](https://github.com/HZCX404/memecoin-trading-bots)
- [Jackhuang166/solana-pumpfun-laserstream-sniper-bot](https://github.com/Jackhuang166/solana-pumpfun-laserstream-sniper-bot)
- [machenxi/rugpull-scam-token-detection](https://github.com/machenxi/rugpull-scam-token-detection)
- [degenfrends/solana-rugchecker](https://github.com/degenfrends/solana-rugchecker)
- [drixindustries/Rug-Killer-On-Solana](https://github.com/drixindustries/Rug-Killer-On-Solana)
- [TreeCityWes/Pump-Fun-Trading-Bot-Solana](https://github.com/TreeCityWes/Pump-Fun-Trading-Bot-Solana)
- [carson2222/pumpfun-bot](https://github.com/carson2222/pumpfun-bot)
- [RugCheck.xyz](https://rugcheck.xyz/)
- [Solsniffer.com](https://www.solsniffer.com/)

---

# REV 2 — Deeper analysis (2026-05-10)

After the first pass I went back with more specific searches and read more credible sources. The first analysis was directionally right but missed concrete numbers and practical details. This section corrects/extends it.

## Hard latency targets — what "competitive" actually means

From [Dysnix's production-grade sniper bot blueprint](https://dysnix.com/blog/complete-stack-competitive-solana-sniper-bots) (the most concrete public writeup I found):

| Stage | Elite target | Acceptable | Where we are |
|-------|--------------|------------|--------------|
| RPC response (`getSlot`, etc.) | **<40ms** | <50ms | ~50-150ms (Helius beta endpoint) |
| Detection (event capture) | **<50ms** | 20-50ms polling | **~300-500ms** (PumpPortal WS aggregator) |
| Tx construction → broadcast | **<100ms** | <150ms | ~300-700ms (PumpPortal API + sign + send) |
| Full pipeline (detect → trade) | **<150ms** | <200ms | **~2-4 seconds** |
| Win rate (top operators) | **>60%** | >50% | 67% historical (encouraging) |

**Takeaway**: we're an order of magnitude slower on the hot path than the elite tier. Not surprising — PumpPortal is an aggregator, and tx construction goes through their HTTP API instead of being built locally. **The intelligence layer is what compensates** for the latency gap.

## D3AD-E's 5ms tx-build claim — how it's actually achieved

[D3AD-E/Solana-sniper-bot](https://github.com/D3AD-E/Solana-sniper-bot) claims "5ms tx build and send time to 4 providers" — concrete techniques:

- **Native Rust module compiled via N-API** for tx construction (not interpreted JS/Python)
- **Local Solana node** + Redis for hot-path state caching
- **Multi-region deployment** with regional replicas
- **Keep-alive HTTP connections** (no new TLS handshake per request)
- **Shred access** for accelerated tx propagation
- **4 providers**: 0slot, NextBlock, Astralane, Node1 (specific names)

We hit none of this. We use Python (slower), a remote RPC (Helius), no local node, single-region, no Rust hot path.

**The gap**: a TypeScript/Node N-API + Rust hybrid is 100-1000× faster than aiohttp Python on tx serialization. This is why most serious bots are Rust or TS-with-Rust.

## Premium tx providers — pricing and tier landscape

This is the part the first analysis lumped together. Here's actual pricing for the providers serious bots use:

| Provider | Pricing | Notes |
|----------|---------|-------|
| **Jito Block Engine** | **Free** (1 req/sec/IP/region default) | sendTransaction with MEV protection. No auth needed. Add to `EXTRA_RPC_URLS` today. |
| **0slot** | Free first week, then paid | Specifically built for "0-slot" inclusion. Free trial is worth running for 7 days. |
| **NextBlock** | **~5 SOL/month (~$1,000)** | TX Stream API + sendTransaction relay. Paid via SOL transfer to `nextstream.sol`. |
| **Astralane** | Paid, "institutional-grade" | Microsecond precision claims. Pricing not public. |
| **Node1.io** | Paid | Used by D3AD-E. |
| **BloxRoute** | Paid, ~$50-500/month tiers | Cross-chain. |
| **Nozomi** | Paid | |
| **LilJit** | Free | Trent.sol Jito derivative. |
| **BlockRazor** | Paid | |
| **PublicNode** | Free | Already in our config. Generic Solana RPC, not optimized for tx. |
| **Helius** | $49+/mo (your tier) | Detection (LaserStream) + RPC + sendTransaction. |

**Practical implication for $400 stakes**:
- **Add for free**: Jito Block Engine (sendTransaction endpoint), LilJit, PublicNode
- **Worth a free trial**: 0slot (1 week)
- **Don't pay yet**: NextBlock at $1k/mo is way out of proportion to $400 stake. **Reconsider only if wallet grows to $5k+ where the marginal latency gain justifies $1k/mo.**

## Jito Python SDK — bigger deal than I estimated

I previously said "real Jito bundles = ~1 day rewrite." That was conservative. The official [`jito-py-rpc`](https://github.com/jito-labs/jito-py-rpc) library exists, with these methods:

- `send_bundle` — submit up to 5 atomic txs
- `send_transaction` — single tx via Jito with MEV protection
- `get_bundle_statuses` — confirm landing
- `get_inflight_bundle_statuses` — monitor recent (5-min) bundle history
- `get_tip_accounts` — fetch the 8 designated tip accounts

**Revised effort estimate**: ~4-6 hours for a basic Jito bundle integration. Steps:
1. `pip install jito-py-rpc`
2. Construct buy + tip-transfer-to-Jito-tip-account in a 2-tx bundle
3. Submit via `send_bundle`
4. Poll `get_bundle_statuses` for landing
5. Test against `simulateBundle` first

Still bigger work than multi-RPC race (which is shipped), but **half the effort I quoted in the first analysis**.

## Tip economics — concrete numbers

| Strategy | Required | Recommended |
|----------|----------|-------------|
| Single tx via Jito sendTransaction | None | **70% priority fee + 30% tip** |
| Bundle via sendBundle | Tip only (no priority fee) | Query `bundles.jito.wtf/api/v1/bundles/tip_floor` for current 50th-percentile tip |
| Minimum tip | **1,000 lamports (0.000001 SOL)** | Often need 10,000-100,000 lamports during competitive periods |

**For our 0.06 SOL trade size**, tips of 0.001-0.005 SOL (1.7%-8.3% of trade) get bundles landing competitively. That's actually *cheaper* than our current `SELL_PRIORITY_FEE_SOL=0.005` (8.3% of trade) — Jito bundles could be a wash on cost while delivering atomic execution.

## Geographic Jito endpoints — pick one closer to your trader

Your trader runs from your Windows machine — assuming North America, the closest Jito Block Engine is:

```
https://ny.mainnet.block-engine.jito.wtf       # NYC — best for east coast US
https://slc.mainnet.block-engine.jito.wtf      # Salt Lake City — best for west coast US
```

Replace the generic `https://mainnet.block-engine.jito.wtf` (which routes to whoever's closest, with extra hop) with the specific regional endpoint. **Saves 20-50ms per tx.**

For Europe: `frankfurt`, `amsterdam`, `dublin`, `london`. Tokyo + Singapore for Asia.

## Sandwich protection — `jitodontfront`

A tx-protection pattern I missed in pass 1: include any pubkey starting with `jitodontfront` (e.g., `jitodontfront111111111111111111111111111111`) in your tx instructions. Bundles will only include the tx if it appears at index 0 — preventing a sandwich attacker from front-running a copy of your tx.

**Worth adding** to our buy txs once we ship Jito bundles. Defensive, costs nothing.

## Auction mechanics — what actually wins

Bundles enter a priority auction every **50ms** at the Block Engine. Selection is **tip-to-compute-units efficiency**, not absolute tip.

**Implication**: a small, lean bundle (1 tip + 1 swap, ~200K CU) with 0.002 SOL tip beats a fat bundle (3 swaps + tip, ~600K CU) with 0.003 SOL tip. **Compute budget management matters.** Our buys currently don't set a CU limit, which means they default to 200K — fine for pump.fun's simple swap, but worth measuring.

## Updated "minimum execution upgrade" recommendation

Replacing the §3 recommendation in the first analysis. Here's the **immediate, free, concrete upgrade**:

```env
# .env
RPC_URL=<your-helius-laserstream-url>

# Free additions — no signup, racing happens automatically
EXTRA_RPC_URLS=https://ny.mainnet.block-engine.jito.wtf/api/v1/transactions,https://slc.mainnet.block-engine.jito.wtf/api/v1/transactions,https://solana-rpc.publicnode.com
```

Three lanes, geo-optimized for North America, no payment beyond Helius. Activate before $400 deployment.

**Then for Jito bundles** (next focused session): `pip install jito-py-rpc`, build the 2-tx (buy + tip) bundle wrapper, replace `_sign_and_send` for buys only. Keep the multi-RPC race as the sell path because sells need atomicity less than buys do.

## What I missed in pass 1 (honest)

1. **Premium tx providers cost real money** — I didn't price them. Some (NextBlock $1k/mo) are way overkill for $400 stake.
2. **Jito has a Python SDK** — I estimated rewrite effort assuming we'd build from protobuf. The SDK halves the effort.
3. **Jito has 9 geo endpoints** — using the generic URL costs 20-50ms vs picking your nearest.
4. **`jitodontfront` sandwich protection** — defensive pattern I should have flagged.
5. **Concrete latency targets** (<150ms full pipeline) — I said "faster than us" without numbers.
6. **D3AD-E's specific tech stack** — the Rust N-API hybrid is *the* answer to Python's serialization slowness.

## Sources (rev 2 — additional)

- [Dysnix: Complete Stack for Competitive Solana Sniper Bots (2026)](https://dysnix.com/blog/complete-stack-competitive-solana-sniper-bots)
- [Dysnix: Top Solana Sniper Bots (2026)](https://dysnix.com/blog/top-solana-sniper-bot)
- [QuickNode: Top 10 Solana Sniper Bots (2026)](https://www.quicknode.com/builders-guide/best/top-10-solana-sniper-bots)
- [Jito Labs: Low Latency Tx Send](https://docs.jito.wtf/lowlatencytxnsend/)
- [Jito Python SDK (`jito-py-rpc`)](https://github.com/jito-labs/jito-py-rpc)
- [Helius LaserStream](https://www.helius.dev/laserstream)
- [Helius Yellowstone gRPC docs](https://www.helius.dev/docs/grpc)
- [QuickNode Yellowstone gRPC Python tutorial](https://www.quicknode.com/docs/solana/yellowstone-grpc/overview/python)
- [D3AD-E/Solana-sniper-bot](https://github.com/D3AD-E/Solana-sniper-bot)
- [0slot.trade](https://0slot.trade/)
- [Astralane](https://astralane.io/)
- [NextBlock TX Stream API](https://docs.nextblock.io/api/tx-stream)
- [vvizardev/solana-relayer-adapter-rust](https://github.com/vvizardev/solana-relayer-adapter-rust) — unified relayer adapter (Rust)
- [roswelly/solana-block-engine-client](https://github.com/roswelly/solana-block-engine-client) — Rust client supporting Jito/Nozomi/ZeroSlot/BlockRazor/Astralane/NextBlock
- [Bundles tip floor API](https://bundles.jito.wtf/api/v1/bundles/tip_floor)
