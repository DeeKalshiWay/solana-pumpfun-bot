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
