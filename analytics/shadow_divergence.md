# Shadow Mode Divergence Report

**Status**: no shadow-mode data on disk yet.

`analyzer/shadow_mode.py` ships the recording API and `ShadowMode`
singleton, but the wiring into the live trade pipeline
(`trader/pumpportal_executor.py` and `risk/manager.py`) is intentionally
deferred — see the module docstring for the half-shipped scope.

Once wired, every live trade writes a record to
`logs/shadow_outcomes.jsonl` with both LIVE and SIM PnL for the same
decision. Re-run this script and the report will populate with:

- Overall mean / median / p99 divergence (slippage tax)
- Per-mint breakdown (which tokens are hostile)
- Trend over time (is friction trending up or down?)
- Verdict: is paper mode trustworthy or systematically optimistic?

Run after a few hours of live trading with wiring in place.
