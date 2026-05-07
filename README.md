# Solana Pump.fun Trading Bot

Production-grade autonomous trading bot for the Solana / pump.fun ecosystem.
Single shared WebSocket connection, multi-factor signal scoring, Kelly-lite
adaptive position sizing, tiered take-profit ladder, counterfactual learning
loop, and a live web dashboard.

## Highlights

- **+7,100% PnL** over a multi-day paper-trading run on a 1.0 SOL bankroll (1.0 → 72.00 SOL)
- **2,044 closed trades**, 44.7% win rate, biggest single trade +463% (caught the same moonshot ticker 4 times via momentum-stall exits)
- **Single shared PumpPortal WebSocket** with pub/sub fan-out to 3 downstream consumers (eliminates rate-limit triggers)
- **Counterfactual learning loop** — every rejected token gets re-polled 10 minutes later so we know whether each filter is pruning rugs or killing winners. 7,500+ resolved outcomes across 15+ filter classes prove every filter has negative expected value on rejected tokens.
- **Crash-safe persistence** — atomic JSON writes, PID lockfile prevents duplicate instances, watchdog respawns on any failure

## Architecture

```
main.py                Orchestrator — wires every component, runs the asyncio task graph
config.py              Single config surface — every threshold, fee, ladder lives here
analyzer/
  signal_scorer.py     4-factor score (creator / curve / community / momentum)
  counterfactual.py    Re-polls rejected tokens 10 min later, aggregates verdicts
  score_bins.py        Per-score-band win-rate + EV attribution
detector/
  pumpfun_monitor.py   Single shared PumpPortal WS with pub/sub fan-out
  pumpfun_tracker.py   v3 metadata polling for reply velocity, ATH ratio, livestream
  creator_tracker.py   Persistent leaderboard of pump.fun creators
  wallet_intel.py      Bot-wallet accumulator + bundled-launch detection
  holder_filter.py     getTokenLargestAccounts → top-10 concentration check
  influencer_monitor.py Twitter v2 watchlist for $TICKER mentions
  social_monitor.py    Twitter / Telegram mention store
  dex_monitor.py       DexScreener + Birdeye enrichment
risk/
  manager.py           Position sizing, stop loss, trailing stops, TP ladder,
                       circuit breakers, momentum-stall + no-movement exits
trader/
  executor.py          Smart router: Jupiter for graduated, PumpPortal for bonding curve
  pumpportal_executor.py  Local-tx API integration with priority fees
  wallet.py            Solana wallet (solders Keypair, RPC balance queries)
  paper_executor.py    Paper-trading simulator with bonding-curve math
  paper_wallet.py      Persistent paper SOL balance
logger/
  dashboard.py         Rich terminal dashboard
  web_server.py        aiohttp dashboard at :8765 with 7 API endpoints
  report.py            Hourly snapshots → equity curve + verdict box
  setup_logging        Loguru with rotation + gzip
web/
  dashboard.html       Single-file SPA: SVG equity chart with crosshair, brush
                       zoom, click-to-pin trade markers, 4 view modes (SOL,
                       PnL %, Trades, Drawdown), 7 tabs of data
```

## Strategy at a glance

### Detection
WebSocket subscribes to PumpPortal's `subscribeNewToken`. Every new mint goes
through a single ingestion pipeline; `pumpfun_tracker` and `wallet_intel`
register callbacks rather than opening their own connections (3 → 1 WS reduces
rate-limit risk).

### Scoring (0-100 across 4 factors of 25 each)
1. **Creator signal** — initial buy size + creator track record (top-10
   creators get +20 bonus, blacklisted creators hard-rejected)
2. **Bonding curve progress** — research-backed: tokens past 30% migration
   threshold rug less; velocity bonus for fast accumulators
3. **Community / buy pressure** — holders, buy/sell ratios, social mentions,
   pump.fun reply velocity
4. **Price momentum + market cap range** — sweet spot 25-60 SOL MC

### Hard filters (cheap rejects, run before scoring)
- Symbol/name blacklist (TEST, SOL, etc.)
- ATH-ratio dump filter (skip tokens already down 50%+ from peak)
- Bot-creator filter (50+ pump.fun mints bought = sniper)
- Bundled-launch filter (2+ wallets buying in first 4s)
- Holder concentration (top 10 > 70% = rug risk)

### Position sizing — Kelly-lite adaptive
- Base trade = `MAX_SOL_PER_TRADE` capped at `MAX_POSITION_PCT` of wallet
- Multiplier scales with rolling 20-trade win rate:
  - Hot streak (≥55% WR) → 2.0× (compounds momentum)
  - Cold streak (≤30% WR) → 0.6× (avoids digging deeper)
- Hard cap at 2.5× base regardless of streak

### Exit ladder — moonshot-optimized
| Trigger | Action |
|---|---|
| +75% gain | Sell 15% (small lock) |
| +300% gain | Sell 25% |
| +800% gain | Sell 30% |
| Last 30% | Rides with adaptive trailing stop — uncapped tail capture |
| -10% from entry | Stop loss |
| 12% from peak (early) / 35% from peak (after +100%) | Trailing stop |
| Flat ±3% for 2 min | No-movement exit (data: dead tokens don't recover) |
| 4 min hold | Time exit (data: trades >6 min only win 3.7%) |

### Circuit breakers
- 6 consecutive losses → 10-min cooldown
- -25% day drawdown → pause until UTC midnight
- -40% lifetime drawdown → emergency stop (sell all, no new buys)

## Quick start

```bash
# 1. Install dependencies
python -m pip install -r requirements.txt

# 2. Copy env template and fill in keys
cp .env.example .env
# Edit .env: paste your SOLANA_PRIVATE_KEY (from Phantom → Settings → Show Private Key)

# 3. Run (paper mode by default — no real money)
python main.py

# 4. Open dashboard
http://127.0.0.1:8765
```

To go live, set `PAPER_TRADING = False` in `config.py`. **Don't go live until you've run paper for 7+ days and verified positive expected value on the LEARN tab.**

## 24/7 operation (Windows)

```powershell
# Install scheduled task that auto-starts on login + auto-restarts on crash
powershell -ExecutionPolicy Bypass -File install_autostart.ps1
Start-ScheduledTask -TaskName PumpBot24x7
```

Logs land in `logs/`. Watchdog log records all restarts.

## Live dashboard

The web dashboard at `:8765` shows:

- **OVERVIEW** — balance, PnL, win rate, exposure
- **POSITIONS** — currently open positions with live PnL%
- **SIGNALS** — live stream of every token scored
- **TRADES** — closed trade history (persists across restarts)
- **CREATORS** — leaderboard of top creator wallets
- **INTEL** — bot wallets detected, bundle decisions, influencer mentions
- **LEARN** — counterfactual filter analysis + score-band win rates (the
  "is this strategy actually working" tab)
- **REPORT** — hourly equity curve with click-to-pin trade markers, verdict box

## Performance disclaimer

This is paper trading data. Live trading WILL diverge from paper for these
reasons:

- Real slippage on volatile pump.fun tokens is 5-30% per round-trip
  (paper sim uses 1.5%)
- Public-RPC transaction land time is 1-3s; staked Helius drops this to
  200-500ms
- Failed transactions cost gas without filling; ~10% of pump.fun txs fail
  on public RPC
- MEV bots will front-run buys on liquid mints

Realistic live-vs-paper gap: 30-50% of paper EV survives. **Never trade
money you can't afford to lose.** Pump.fun has 95%+ token rug rate.

## License

MIT — see LICENSE.

## Built by

**Dennis Wells** ([@DeeKalshiWay](https://github.com/DeeKalshiWay)) — Las Vegas, NV.
Solo project, ~5,000 lines of Python, designed and shipped in under a week.
Every architectural decision documented in commit history and inline comments.

Open to remote Solana / Python / Web3 backend roles. denniswells2019@gmail.com
