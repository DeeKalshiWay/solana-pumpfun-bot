# pump_bot — async Python trading systems demo

[![CI](https://github.com/DeeKalshiWay/solana-pumpfun-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/DeeKalshiWay/solana-pumpfun-bot/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Production-style architecture for low-latency event processing,
applied to pump.fun memecoin trading. Built to demonstrate systems
engineering — async pipelines, fan-out from a single WebSocket,
crash-safe persistence, observability — not as a profitable strategy.
The trading layer is a paper-mode test bed for the systems work
underneath; see **Limitations** for an honest read on the strategy.

## What this project demonstrates

- **Real-time async pipeline.** One PumpPortal WebSocket fans out via
  pub/sub to 8 detector modules. No duplicate connections, no
  rate-limit waste. ~70 ms decision latency on Helius beta endpoint.
- **Reliability hygiene.** Atomic JSON writes (write-temp +
  `os.replace`), PID lockfile prevents duplicate instances, watchdog
  auto-restarts the process, WebSocket rate-limit backoff with
  exponential jitter, log rotation + gzip.
- **Adaptive risk engine.** Kelly-lite sizing scaled by rolling win
  rate, multi-tier circuit breakers (loss streak, daily DD, lifetime
  DD), trailing stops with regime switch (tight → moonshot mode),
  no-movement exit, time exit.
- **Observability.** aiohttp web dashboard at `:8765` with 7 REST
  endpoints, hourly equity snapshots, single-page SPA with custom SVG
  equity chart, click-to-pin trade markers, drawdown view.
- **Test + lint floor.** ruff + mypy + pytest in CI on every push,
  Docker build smoke test, GitHub Actions workflow at
  [`.github/workflows/ci.yml`](.github/workflows/ci.yml).
- **Two deployment paths.** Docker compose for any Linux host, or
  systemd unit + installer script for bare-metal. Windows Task
  Scheduler also supported for personal use.

## Architecture

```
main.py                Orchestrator — wires components, runs the asyncio task graph
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
  influencer_monitor.py  Twitter v2 watchlist for $TICKER mentions
  social_monitor.py    Twitter / Telegram mention store
  dex_monitor.py       DexScreener + Birdeye enrichment
risk/
  manager.py           Position sizing, stop loss, trailing stops, TP ladder,
                       circuit breakers, momentum-stall + no-movement exits
trader/
  executor.py          Smart router: Jupiter for graduated, PumpPortal for bonding curve
  pumpportal_executor.py  Local-tx API integration with priority fees
  wallet.py            Solana wallet (solders Keypair, RPC balance queries)
  paper_executor.py    Paper-trading simulator with realistic-friction model
  paper_wallet.py      Persistent paper SOL balance
logger/
  dashboard.py         Rich terminal dashboard
  web_server.py        aiohttp dashboard at :8765 with 7 API endpoints
  report.py            Hourly snapshots → equity curve + verdict box
deploy/
  pump_bot.service     systemd unit (hardened: NoNewPrivileges, ProtectSystem)
  install.sh           Idempotent root installer for /opt/pump_bot
tests/                 pytest smoke tests for risk math + config invariants
```

## Strategy at a glance

> Paper trading only. The numbers below are what the bot does today;
> see the **Limitations** section for why they should not be read as a
> claim of live profitability.

### Detection
WebSocket subscribes to PumpPortal's `subscribeNewToken`. Every new
mint goes through a single ingestion pipeline; `pumpfun_tracker` and
`wallet_intel` register callbacks rather than opening their own
connections (3 → 1 WS reduces rate-limit risk).

### Scoring (0-100 across 4 factors of 25 each)
1. **Creator signal** — initial buy size + creator track record (top-10
   creators get +20 bonus, blacklisted creators hard-rejected)
2. **Bonding curve progress** — research-backed: tokens past 30%
   migration threshold rug less; velocity bonus for fast accumulators
3. **Community / buy pressure** — holders, buy/sell ratios, social
   mentions, pump.fun reply velocity
4. **Price momentum + market cap range** — sweet spot 25-60 SOL MC

### Position sizing — Kelly-lite adaptive
- Base trade = `MAX_SOL_PER_TRADE` capped at `MAX_POSITION_PCT` of wallet
- Multiplier scales with rolling 20-trade win rate:
  - Hot streak (≥55% WR) → 2.0× (compounds momentum)
  - Cold streak (≤30% WR) → 0.6×
- Hard cap at 2.5× base regardless of streak

### Exit ladder — moonshot-optimized
| Trigger | Action |
|---|---|
| +75% gain | Sell 15% (small lock) |
| +300% gain | Sell 25% |
| +800% gain | Sell 30% |
| Last 30% | Rides with adaptive trailing stop — uncapped tail capture |
| -10% from entry | Stop loss |
| 18% from peak (early) / 35% from peak (after +100%) | Trailing stop |
| Flat ±3% for 2 min | No-movement exit |
| 4 min hold | Time exit |

### Circuit breakers
- 6 consecutive losses → 10-min cooldown
- −25% day drawdown → pause until UTC midnight
- −40% lifetime drawdown → emergency stop (sell all, no new buys)

## Limitations

The paper sim is up 100&times; on a realistic-friction model &mdash;
that is a real result inside the simulator. Whether it survives a
move to real money is unknown, and there are honest open questions
worth flagging:

- **Live performance unverified.** The realistic-friction simulator
  models size-dependent slippage, MEV tax, ~5% tx-fail rate, and
  network fees, so it is closer to live than the legacy 1.5% flat
  slippage path was. But there is still no live data. Until real
  capital is traded, paper PnL is paper PnL.
- **Counterfactual loop &mdash; observability, not validation.**
  Re-polling rejected tokens 10 min later is a useful signal for
  "what is each filter killing." It is *not* held-out validation:
  pump.fun's base rate is ~95 % rug, so a filter beating its rejects
  does not prove it beats the base rate. A held-out test split
  remains future work; for now treat the LEARN tab as observability,
  not proof.
- **Some thresholds are tuned to the trade history.** The TP ladder
  (75 / 300 / 800 %) and the 4-min time exit are partly motivated by
  pump.fun dynamics and partly chosen by inspecting closed-trade
  outcomes. They will need re-tuning for any market regime that does
  not look like the current one.
- **Concentration risk in the top tickers.** Querying the sqlite
  trade log:
  ```
  OHIO     20 trades   33.36 SOL   41.9 % of total PnL
  USTRL     5 trades   10.28 SOL   12.9 %
  USDJT     4 trades   10.18 SOL   12.8 %
                                  ─────
  Top 3 tickers                   67.7 %
  Top 5 tickers                   79.2 %
  ```
  When 80 % of paper PnL comes from five tickers, the result is
  closer to "the system was good at compounding in five lucky names"
  than "the system has broad edge across pump.fun." That should be
  reflected in any expectation of out-of-sample performance.
- **Hot-streak 2&times; sizing is a regime bet.** It assumes recent
  win rate carries forward &mdash; reasonable when memecoin regimes
  persist for hours, weaker when winners are i.i.d. The 2.5&times;
  hard cap limits ruin in either case.

The portfolio value of this project is the systems engineering: the
async pipeline, the dual-write persistence, the CI/Docker/systemd
floor, the observability stack. The trading layer is a working
test bed for that engineering, not a verified profitable strategy.

## Numbers

Paper mode, REALISTIC_PAPER_SIM=1, single canonical run:

| Metric | Value |
|---|---|
| Window | 2026-04-19 → present (live) |
| Starting bankroll | 1.0 SOL (virtual) |
| Current balance | ~105 SOL |
| Closed trades | ~2,900 |
| Win rate | ~42 % |
| Largest single winner | +463 % |
| Concentration risk | ~60 % of PnL from one ticker, multiple entries |

Earlier snapshots circulating elsewhere (1,521 trades / 7,100 % / etc.)
are pre-realistic-friction-sim and superseded by the run above.

## Quick start

### Docker (recommended)

```bash
cp .env.example .env  # fill in SOLANA_PRIVATE_KEY and HELIUS_API_KEY
docker compose up -d
docker compose logs -f
# Dashboard: http://127.0.0.1:8765
```

### Bare metal Python

```bash
python -m pip install -r requirements.txt
cp .env.example .env  # edit
python main.py
# Dashboard: http://127.0.0.1:8765
```

### Linux + systemd (long-running)

```bash
sudo bash deploy/install.sh
systemctl status pump_bot
journalctl -u pump_bot -f
```

### Windows Task Scheduler (personal rig)

```powershell
powershell -ExecutionPolicy Bypass -File install_autostart.ps1
Start-ScheduledTask -TaskName PumpBot24x7
```

> Going live (`PAPER_TRADING = False` in `config.py`) is **not
> recommended** until the issues in **Limitations** are addressed.

## Development

```bash
pip install -r requirements-dev.txt
pytest                # 11 tests, ~0.2 s
ruff check .          # CI-enforced
mypy tests/           # CI-enforced on tests/, lenient elsewhere
```

CI runs all four (lint / typecheck / pytest / Docker build) on every
push to `main` and every PR.

## Live dashboard

The web dashboard at `:8765` has 8 tabs: OVERVIEW, POSITIONS, SIGNALS,
TRADES, CREATORS, INTEL, LEARN (counterfactual + score-band stats),
REPORT (equity curve with crosshair + brush zoom + click-to-pin trade
markers).

## License

MIT — see [LICENSE](LICENSE).

## Built by

**Dennis Wells** ([@DeeKalshiWay](https://github.com/DeeKalshiWay)) —
Las Vegas, NV. Solo project. Open to remote Solana / Python / Web3
backend roles. <denniswells2019@gmail.com>
