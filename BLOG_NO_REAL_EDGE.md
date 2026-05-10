# I Built a Solana Memecoin Bot That Looked Profitable. None of It Was Real Edge.

*An honest writeup of what I learned auditing my own pump.fun trading bot.*

---

## The headline number

After 295 closed trades over 3 days of live pump.fun trading, my bot showed:

- **67% win rate**
- **+0.037 SOL total realized PnL**

Most retail bot operators would call that "working" and scale up. Then I ran one analysis script and the floor fell out.

## The one number that killed the thesis

I attributed every trade's PnL to its token symbol and asked: *what share of the total came from each one?*

| Rank | Symbol | Trades | PnL (SOL) | % of total PnL |
|-----:|--------|-------:|----------:|---------------:|
| 1 | CLAUDE | 2 | +0.0378 | **+102.3%** |
| 2 | Quant  | 2 | +0.0338 | +91.3% |
| 3 | UFO    | 1 | +0.0337 | +91.1% |
| 4 | ARES   | 1 | +0.0285 | +77.0% |
| 5 | Anomaly| 1 | +0.0257 | +69.5% |

**The top single symbol contributed more than 100% of the total PnL.** Without those two CLAUDE trades, the bot was negative.

Without the top three, the bot was −0.114 SOL — about a 5% loss against starting capital.

Top 10 contributors collectively produced **628% of the net PnL** — meaning the bottom 285 trades lost over 5× what the top 10 won.

That's not edge. That's a winning lottery ticket with a lot of survivorship bias on top.

## Why the win rate lied

67% sounds great until you notice the asymmetry:

- **Average winning trade**: +0.0073 SOL  (~3.5% gain on 0.06 SOL trade)
- **Average losing trade**: −0.0151 SOL  (~7% loss)

Wins are half the size of losses. The math:

```
EV = 0.67 × 0.0073 − 0.33 × 0.0151
   = +0.00489 − 0.00498
   = −0.00009 SOL per trade  →  basically zero
```

The 67% WR exactly compensates for the size asymmetry. The strategy is sitting right on break-even and being lifted entirely by a few outsized winners I cannot count on continuing to find.

## The held-out validation that exposed the filter problem

The bot logs every signal it *rejects* to a counterfactual file, then polls the outcome 10 minutes later. Over 7,000+ rejections were on disk. I split chronologically — first 70% to train, last 30% held-out — and asked: for each rejection reason, what's the rug rate on the held-out set vs the base rate?

A filter "validated" iff its 95% Wilson CI on the lift over base rate excludes zero, AND the train-vs-test rates were stable within 10pp.

**Result: 3 of 12 filters validated. 9 of 12 were not.**

The losers weren't just neutral — most had *negative* lift. The `score_band_20` filter (rejections for raw score 20–29) had a 15% rug rate on the held-out set vs a 36% base. **Rejections at that score band rugged less often than random.** That filter was killing winners, not catching rugs.

The only filters that held up under held-out testing were the `big_init_buy_4+_sol` ones — when a creator's initial buy was ≥ 4 SOL, the rug rate climbed to 85–95% on held-out. That's real signal. The rest was self-deception dressed as confidence.

## How I fooled myself

Three failure modes — all classic, all in plain sight, all invisible without the right script:

**1. I trusted aggregate win rate as a proxy for edge.** It isn't. WR × avg-win must beat (1-WR) × avg-loss. On pump.fun, where fees alone eat 5–15% per round trip and stops fire on noise, the only way to clear that is huge winners. WR is a noisy headline.

**2. I tuned filters on the same data I evaluated them on.** When the auto-tune logic looked at "filter X rejected 80% rugs", I didn't ask "what was the base rate it was rejecting from?" If 60% of all rejections rug regardless of reason, an 80% filter is only 20pp ahead — and that gap might not survive a fresh sample.

**3. I never capped the contribution of any single token.** A bot that 10×'s your money because one token went 5,000× has not learned anything generalizable. The next 5,000× could come tomorrow, or in 5 years, or never. Building a strategy that requires it is gambling.

## What I did about it

- **Killed the unvalidated filters.** Replaced static `score_band_20` rejection with a rug-pattern-memory dock that ONLY fires when the specific feature signature matches ≥3 historical rugs — quantified, traceable, sample-size-bounded.
- **Built a counterfactual feed-through into rug memory** so the bot keeps learning patterns from observed market data even when wallet is empty.
- **Rebuilt sizing math around the friction floor.** At 0.06 SOL trades, fees alone made +25% TPs negative. Bumped to 0.10 SOL where +25% wins finally clear friction.
- **Wrote two scripts**: `tools/holdout_validate.py` (CI-based filter validation) and `tools/concentration_audit.py` (per-ticker PnL cap). Both run on every deployment now. If concentration spikes above 50%/top-1, that's a kill signal regardless of headline PnL.

## What the bot is actually good at

Not finding alpha. Generating data. The intelligence layer — counterfactual logging, rug pattern memory, bot-wallet identification, creator track records — is genuinely differentiated from public bots in the space, most of which are execution-optimized but data-blind.

I now have an instrument that *measures* market behavior. It accumulates rug signatures, sniper-bot wallet identities, and creator track records 24/7 whether or not I'm trading. Whether that intelligence ever produces durable edge, I don't yet know. But I'll know it from out-of-sample validation, not from a single moonshot.

## What recruiters should take from this

I'd rather hire (or be) the engineer who killed their own thesis with a 200-line Python script than the one who scaled a strategy that 100×'d on a sample of one. The infrastructure is the same. The intellectual honesty is what separates "I made money in a bull market" from "I understand my system."

The repo is open. The audit scripts are in [`tools/holdout_validate.py`](tools/holdout_validate.py) and [`tools/concentration_audit.py`](tools/concentration_audit.py). The output of both is committed in [`analytics/holdout_validation.md`](analytics/holdout_validation.md) and [`analytics/concentration_audit.md`](analytics/concentration_audit.md) so you can see the actual numbers, not a curated screenshot.

---

*Repository: [github.com/DeeKalshiWay/solana-pumpfun-bot](https://github.com/DeeKalshiWay/solana-pumpfun-bot)*
