# Held-Out Counterfactual Validation

**Dataset**: `logs/counterfactual.jsonl` — 10018 total rejections
**Split**: first 70% train / last 30% held-out test
**Train**: 7012 records | **Test**: 3006 records
**Rug threshold**: `mc_delta_pct <= -50%`
**Pump threshold**: `mc_delta_pct >= +100%`

**Base rug-rate on held-out set**: **36.3%** (1091/3006 of ALL rejections rugged)

## Filter performance — sorted by lift over base rate

Only categories with >= 30 held-out samples shown. A filter is **validated** iff:
1. Its 95% Wilson CI on lift over base rate excludes 0 (statistical significance), AND
2. Train rug-rate and test rug-rate differ by less than 10pp (stable pattern, not drift)

| Reason | n(train) | rug%(train) | n(test) | rug%(test) | lift | 95% CI on lift | pump%(test) | verdict |
|--------|---------:|------------:|--------:|-----------:|-----:|:--------------:|------------:|:--------|
| `big_init_buy_4.94sol` | 322 | 93.5% | 109 | 95.4% | +59.1pp | [+53.4, +61.7]pp | 0.0% | ✅ validated |
| `big_init_buy_4.00sol` | 94 | 86.2% | 57 | 87.7% | +51.4pp | [+40.5, +57.6]pp | 1.8% | ✅ validated |
| `big_init_buy_5.00sol` | 152 | 98.0% | 117 | 84.6% | +48.3pp | [+40.7, +53.7]pp | 0.0% | ❌ |
| `big_init_buy_3.95sol` | 159 | 88.1% | 46 | 82.6% | +46.3pp | [+33.0, +54.6]pp | 2.2% | ✅ validated |
| `score_band_0` | 31 | 25.8% | 31 | 41.9% | +5.6pp | [-9.9, +22.9]pp | 3.2% | ❌ |
| `score_band_10` | 503 | 38.8% | 624 | 33.8% | -2.5pp | [-6.1, +1.3]pp | 1.4% | ❌ |
| `score_band_20` | 967 | 35.9% | 227 | 15.0% | -21.3pp | [-25.4, -16.1]pp | 4.4% | ❌ |
| `big_init_buy_2.96sol` | 295 | 6.4% | 114 | 9.6% | -26.6pp | [-30.8, -19.8]pp | 1.8% | ❌ |
| `big_init_buy_1.98sol` | 309 | 16.2% | 105 | 3.8% | -32.5pp | [-34.8, -26.9]pp | 1.9% | ❌ |
| `big_init_buy_2.00sol` | 235 | 5.5% | 78 | 2.6% | -33.7pp | [-35.6, -27.4]pp | 2.6% | ❌ |
| `big_init_buy_3.00sol` | 438 | 2.5% | 153 | 2.0% | -34.3pp | [-35.6, -30.7]pp | 1.3% | ❌ |
| `no_symbol` | 114 | 0.0% | 56 | 0.0% | -36.3pp | [-36.3, -29.9]pp | 0.0% | ❌ |

## How to read this

- **rug%(test)**: of the rejections we made in the held-out window, what fraction went on to rug? Higher is better — the filter is catching real rugs.
- **lift**: rug%(test) minus base rate. If positive, this filter is selecting for *worse-than-average* rugs (good — it's a meaningful signal).
- **95% CI on lift**: if the entire interval is above 0, the lift is statistically significant at 95% confidence. If it crosses 0, the filter's edge could be noise.
- **pump%(test)**: of the rejections, what fraction went on to >+100%? If > 10%, the filter is paying meaningful opportunity cost — every pump rejected was a winner we missed.
- **verdict**: a filter that passes both train→test stability AND CI-excludes-zero is genuinely validated. Anything else is fragile and may not survive in the future.

## Action items

- **For each ❌ row**: consider relaxing or removing the filter. It's not demonstrably better than rejecting randomly.
- **For each ⚠️ KILLS WINNERS row**: the filter rejects too many real pumps. Even if validated as a rug-catcher, the opportunity cost may exceed the savings.
- **For each ✅ row**: keep the filter. The signal is real and stable.