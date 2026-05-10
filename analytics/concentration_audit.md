# Concentration Audit — is the PnL real edge or one lucky moonshot?

**Dataset**: `logs/closed_trades.jsonl` — 295 closed trades, 261 unique symbols
**Total PnL**: +0.0370 SOL
**Per-ticker cap**: 10.0% of |total PnL| = ±0.0037 SOL

## Verdict

🔴 LUCKY — one ticker contributed >50% of total PnL. Without that single trade you'd be near zero or negative. This is not edge, this is sample-size-of-one.

## Concentration metrics

| Metric | Value |
|--------|-------|
| Gini coefficient (PnL distribution) | **0.589** (moderately concentrated) |
| Top 1 symbol's share of total PnL | **102.3%** |
| Top 3 symbols' share | **284.7%** |
| Top 10 symbols' share | **627.9%** |
| Total PnL uncapped | **+0.0370 SOL** |
| Total PnL capped at ±0.0037 per ticker | **+0.0148 SOL** |
| PnL retention after cap | **39.9%** |

## Top 15 contributors

| # | Symbol | trades | wins | gross PnL | % of total | capped PnL |
|---|--------|-------:|-----:|----------:|-----------:|-----------:|
| 1 | `CLAUDE` | 2 | 2 | +0.0378 | +102.3% | +0.0037 |
| 2 | `Quant` | 2 | 2 | +0.0338 | +91.3% | +0.0037 |
| 3 | `UFO` | 1 | 1 | +0.0337 | +91.1% | +0.0037 |
| 4 | `ARES` | 1 | 1 | +0.0285 | +77.0% | +0.0037 |
| 5 | `Anomaly` | 1 | 1 | +0.0257 | +69.5% | +0.0037 |
| 6 | `IOWE` | 1 | 1 | +0.0185 | +50.0% | +0.0037 |
| 7 | `SERIOUS ` | 1 | 1 | +0.0181 | +49.0% | +0.0037 |
| 8 | `motion` | 10 | 10 | +0.0178 | +48.1% | +0.0037 |
| 9 | `SCAN` | 1 | 1 | +0.0106 | +28.8% | +0.0037 |
| 10 | `GNFOS` | 1 | 1 | +0.0077 | +20.9% | +0.0037 |
| 11 | `vibe` | 1 | 1 | +0.0075 | +20.1% | +0.0037 |
| 12 | `KOMUGI OG` | 4 | 4 | +0.0065 | +17.6% | +0.0037 |
| 13 | `NXT` | 1 | 1 | +0.0061 | +16.5% | +0.0037 |
| 14 | `Magic` | 1 | 1 | +0.0057 | +15.4% | +0.0037 |
| 15 | `STACKED DEV` | 3 | 3 | +0.0054 | +14.6% | +0.0037 |

## Bottom 15 contributors (biggest losers)

| # | Symbol | trades | gross PnL | % of total |
|---|--------|-------:|----------:|-----------:|
| 1 | `E404X` | 1 | -0.0366 | -98.9% |
| 2 | `illegal` | 1 | -0.0272 | -73.5% |
| 3 | `Aura` | 3 | -0.0140 | -37.9% |
| 4 | `RANDO` | 1 | -0.0115 | -31.0% |
| 5 | `HANTU` | 1 | -0.0111 | -30.1% |
| 6 | `SPACETITS` | 1 | -0.0111 | -30.0% |
| 7 | `AGI` | 2 | -0.0101 | -27.3% |
| 8 | `CLUTCH ` | 1 | -0.0100 | -27.0% |
| 9 | `KMA` | 1 | -0.0089 | -24.1% |
| 10 | `tokens` | 2 | -0.0088 | -23.8% |
| 11 | `TGM` | 1 | -0.0088 | -23.8% |
| 12 | `STABLEMEME` | 1 | -0.0086 | -23.1% |
| 13 | `TRENCH` | 4 | -0.0084 | -22.6% |
| 14 | `PMAS` | 1 | -0.0084 | -22.6% |
| 15 | `ALIENMONK` | 1 | -0.0080 | -21.5% |

## How to read this

- **Gini > 0.7**: highly concentrated. A few tickers dominate. Edge claim is fragile.
- **Top 1 > 50% of PnL**: one moonshot is doing all the work. **Not edge — luck.**
- **Capped retention < 30%**: most of the PnL comes from a few outliers. Cap them and the curve flatlines.
- **Capped retention > 70%**: gains are distributed. The strategy has structural edge.

## Why this matters

Survivorship bias is the #1 way trading bot operators fool themselves. A backtest that 100×'d may have done so because one token in the sample went 5,000×, lifting everything else into the noise. Capping each ticker's contribution at a small percentage of the total kills that effect and shows what the strategy looked like *on the median trade* — which is what tomorrow's trade will be.

If you can't survive a cap, you don't have edge. You have a winning lottery ticket.