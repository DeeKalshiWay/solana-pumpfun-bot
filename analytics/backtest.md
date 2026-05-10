# Backtester — A/B strategy comparison

Baseline: **Baseline**. Other configs scored against it.

## Side-by-side

| Config | Trades | WR | Total PnL | Avg/trade | Avg Win | Avg Loss | Max DD | vs Baseline |
|--------|-------:|---:|----------:|----------:|--------:|---------:|-------:|------------:|
| Baseline | 990 | 4.4% | -10.3138 | -0.01042 | +0.02225 | -0.01194 | -10.3425 |  |
| PostAudit | 990 | 15.8% | -26.4052 | -0.02667 | +0.03074 | -0.03741 | -26.5350 | -16.0914 SOL |
| Tighter | 1244 | 10.9% | -43.0321 | -0.03459 | +0.03608 | -0.04319 | -43.1619 | -32.7183 SOL |
| ScoreOnly | 1588 | 9.3% | -59.0525 | -0.03719 | +0.04653 | -0.04573 | -59.1823 | -48.7387 SOL |

## Configs tested

### Baseline
```
min_score        = 32
max_init_buy_sol = 1.5
max_curve_pct    = 80
trade_size_sol   = 0.025
```

### PostAudit
```
min_score        = 32
max_init_buy_sol = 1.5
max_curve_pct    = 80
trade_size_sol   = 0.1
```

### Tighter
```
min_score        = 35
max_init_buy_sol = 4.0
max_curve_pct    = 60
trade_size_sol   = 0.1
```

### ScoreOnly
```
min_score        = 35
max_init_buy_sol = 999
max_curve_pct    = 100
trade_size_sol   = 0.1
```

## How to read this

- **Trades**: number of historical signals the config would have entered.
  More trades is not better — fewer high-quality entries can win.
- **WR**: win rate. 67% historical is great BUT see the concentration
  audit — most of it was driven by a few moonshots.
- **Total PnL**: cumulative simulated SOL profit. **Compare RELATIVE
  to baseline, not absolute** — this is a generous backtest (no
  position-count caps, no slippage scaling, no latency).
- **Max DD**: deepest underwater the cumulative curve went. A config
  that ends positive but had a −5 SOL drawdown along the way may be
  psychologically unrunnable.

## Honest limitations

1. No concurrent-position constraint — every qualifying signal trades.
   Real-world caps (MAX_OPEN_POSITIONS, MAX_TOTAL_EXPOSURE_SOL) would
   reduce trade count and PnL.
2. Friction is flat 0.005 SOL/round-trip regardless of trade size.
   At larger sizes friction is a smaller %; this backtest understates
   the friction-floor advantage of bigger trades.
3. Rejected-mint outcomes are 10-min MC snapshots, not full lifecycles.
   Real strategies would exit on stops/TPs before the snapshot.
4. Survivorship bias: the data only includes mints we *saw*. Mints
   that died too fast for the WS to deliver them never enter the set.

Use this for **relative ranking** of configs, not absolute PnL forecasts.