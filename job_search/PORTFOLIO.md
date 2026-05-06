# Solana Pump.fun Trading Bot — Portfolio Summary

A production-grade autonomous trading bot for the Solana / pump.fun ecosystem.
Single-developer project, ~5,000 lines of Python, designed and built end-to-end.

## Live demo / metrics (paper trading)
- **+320% PnL over 4 days** on a 1.0 SOL paper bankroll (1.0 → 4.20 SOL)
- **717 closed trades, 43.7% win rate**
- Bot caught the **same moonshot ticker 4 separate times** (each +328% to +463%)
  via momentum-stall exits — demonstrates the strategy can recognize and
  re-engage with continuing pump cycles rather than locking out after first sell
- Top trade gained 0.366 SOL (≈19% of total wallet) in 36 seconds

## Architecture

```
detector/          Token detection (PumpPortal WebSocket, single shared connection
                   with pub/sub callbacks for downstream consumers)
analyzer/          4-factor scoring engine (creator track record, bonding curve
                   progress, community velocity, price momentum)
risk/              Position sizing (Kelly-lite adaptive), tiered take-profits,
                   trailing stops with moonshot-mode widening, multi-tier
                   circuit breakers (loss-streak, daily-loss, emergency-stop)
trader/            Smart router: Jupiter for graduated tokens, PumpPortal for
                   bonding-curve tokens. Atomic JSON state with PID lockfile.
logger/            aiohttp web dashboard, hourly snapshot logger, equity curve
                   with click-to-pin trade markers, brush-to-zoom, live updates
analyzer/          Counterfactual learning loop — logs every rejection, polls
                   market cap 10 minutes later, aggregates per-filter
                   "did we filter rugs or kill winners" verdicts
```

## Skills demonstrated

**Backend / Systems**
- Async Python (asyncio, aiohttp, websockets) — full async architecture
- WebSocket multiplexing — single connection serving 3 downstream consumers via pub/sub
- Atomic file I/O for crash-safe state persistence
- PID lockfile single-instance guarantee
- Watchdog + scheduled-task auto-restart (Windows Task Scheduler integration)

**Solana / Web3**
- Direct integration with Solana RPC (Helius), WebSocket subscriptions
- Transaction signing with `solders` (versioned transactions)
- Jupiter aggregator + PumpPortal local-tx API integration
- Bonding curve math (virtual reserves, migration thresholds)
- Token holder concentration analysis via `getTokenLargestAccounts`

**Quantitative / Trading**
- Multi-factor signal scoring with bounded subscores
- Kelly-lite adaptive position sizing based on rolling win rate
- Tiered take-profit ladder optimized for power-law return distributions
- Counterfactual analysis to validate filter decisions
- Score-band performance attribution

**Frontend / DevX**
- Single-file SPA dashboard (no framework, ~700 lines, gold-on-black terminal aesthetic)
- Custom SVG equity curve with hover crosshair, click-to-pin tooltips, brush-to-zoom
- Multi-mode chart (SOL / PnL% / Trades / Drawdown)
- Live data via aiohttp polling

**Reliability engineering**
- Diagnosed and fixed multi-instance race conditions (file corruption, port collisions)
- Implemented exponential WS reconnection backoff after 403 rate-limiting incident
- Log rotation (loguru) with gzip compression
- Self-healing recovery from RPC bans

## What this proves I can do

- Take an ambiguous goal ("build a Solana trading bot that makes money") and decompose it into modular, testable systems
- Read research papers + open-source code + integrate into a coherent system
- Debug production issues across async Python, Windows scheduling, network protocols, file systems
- Build measurement and analysis tools (counterfactual learning, score-band attribution) to know whether the system actually works
- Write maintainable code with clear module boundaries

## Code repository
[Add your GitHub link here once you push the code]

## Live dashboard
http://127.0.0.1:8765 (local; or LAN URL when shared)

## What I'd do next given more time
- Replace public RPC with self-hosted Geyser stream (sub-50ms detection)
- Add Jito bundle support for atomic buy+sell with MEV protection
- Train an ML model on the 30,000+ counterfactual outcomes to learn filter weights
- Multi-wallet farming with shared intelligence
- Real-time strategy adaptation per regime (bull/chop/dump auto-detection)

---

# Suggested job titles to search

- Solana Engineer
- Web3 Backend Engineer
- Python Developer (Crypto)
- Quantitative Developer
- Trading Systems Engineer
- DeFi Engineer
- Blockchain Developer
- Smart Contract Engineer
- Algorithmic Trading Developer

# Salary ranges (US remote, 2026)

| Role | Range |
|---|---|
| Junior Python / Web3 Engineer | $70-110k |
| Mid-level Solana / Backend | $110-160k |
| Senior Trading Systems / DeFi | $160-250k |
| Quant Dev (firm) | $180-400k+ bonus |

For freelance / contract: $80-200/hr depending on niche.

# Where to apply (in order of payoff)

1. **Solana ecosystem companies** — Helius, Jito Labs, Phantom, Magic Eden, Tensor, Drift, Mango Markets, Marginfi, Solana Foundation
2. **Crypto-native trading firms** — Wintermute, Jump Crypto, Cumberland, Galaxy Digital, GSR
3. **Generalist crypto** — Coinbase, Kraken, Robinhood Crypto, Anchorage Digital
4. **Web3 startups** with Python backend (lots on Wellfound formerly AngelList)
5. **Freelance:** Toptal, Upwork (Solana category), Crypto.jobs
