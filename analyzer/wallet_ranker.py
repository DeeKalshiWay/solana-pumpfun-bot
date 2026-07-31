"""
analyzer/wallet_ranker.py

Concentration-robust ranking of candidate "proven" wallets for copy-trading.

WHY THIS EXISTS
---------------
The legacy smart-money rule (>=10 outcomes AND >=60% pumped >=+50%) yields 0
wallets on real data, because pump.fun early-buy outcomes are ~95% negative
when measured as mc_delta_pct at the +10min counterfactual mark. That mark
also *understates* fast flippers (who exit in seconds), so it is a noisy,
pessimistic proxy. This module therefore ranks wallets on several robust,
concentration-aware metrics rather than a single mean/threshold, and the
caller picks a cutoff from the reported distribution.

The cornerstone guard (BLOG_NO_REAL_EDGE lesson, applied at wallet level):
a wallet only counts as "proven" if it stays positive AFTER removing its
single best outcome — i.e. its edge is not one lottery ticket.

This is pure analysis over logs/wallet_outcomes.json: { wallet: [pct, ...] }.
No trading, no network. Importable (rank_wallets / WalletScore) and runnable
as a script for a distribution report.
"""

from __future__ import annotations

import json
import os
import statistics as _st
from dataclasses import dataclass, asdict

WALLET_OUTCOMES_FILE = os.path.join(os.path.dirname(__file__), "..", "logs", "wallet_outcomes.json")

# Round-trip friction on pump.fun (fees + slippage + spread). An early buy must
# clear roughly this to be a real win, so "hit" thresholds are set above it.
FRICTION_PCT = 15.0

# Defaults for what counts as "proven". Deliberately conservative; tune from
# the script's reported distribution.
MIN_OUTCOMES = 15        # sample-size floor
WIN_THRESHOLD = FRICTION_PCT   # an outcome >= this is a "win" (clears friction)
MIN_HIT_RATE = 0.20      # fraction of buys that clear friction
RECENCY_WEIGHT_HALFLIFE = 30.0  # outcomes; recent weighted more (ring buffer is latest-first... see note)


@dataclass
class WalletScore:
    wallet: str
    n: int
    hit_rate_0: float        # frac outcomes >= 0
    hit_rate_friction: float # frac outcomes >= WIN_THRESHOLD
    hit_rate_50: float       # frac outcomes >= 50
    median: float
    mean: float
    mean_drop_best: float    # mean after removing single best outcome (concentration guard)
    best_share: float        # best outcome's share of the sum of positive outcomes
    proven_score: float      # composite, higher = better
    is_proven: bool


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def score_wallet(wallet: str, outcomes: list[float],
                 min_outcomes: int = MIN_OUTCOMES,
                 win_threshold: float = WIN_THRESHOLD,
                 min_hit_rate: float = MIN_HIT_RATE) -> WalletScore:
    n = len(outcomes)
    hit0 = sum(1 for o in outcomes if o >= 0) / n if n else 0.0
    hitf = sum(1 for o in outcomes if o >= win_threshold) / n if n else 0.0
    hit50 = sum(1 for o in outcomes if o >= 50) / n if n else 0.0
    median = _st.median(outcomes) if outcomes else 0.0
    mean = _mean(outcomes)

    # Concentration guard: drop the single best outcome, recompute mean.
    if n >= 2:
        best = max(outcomes)
        rest = list(outcomes)
        rest.remove(best)
        mean_drop_best = _mean(rest)
    else:
        mean_drop_best = mean

    pos = [o for o in outcomes if o > 0]
    sum_pos = sum(pos)
    best_share = (max(outcomes) / sum_pos) if sum_pos > 0 else 1.0

    # Composite: reward friction-clearing hit-rate, require the edge to survive
    # removing the best outcome. proven_score is only meaningful for ranking.
    proven_score = hitf * 100.0 + mean_drop_best - max(0.0, (best_share - 0.5)) * 50.0

    is_proven = (
        n >= min_outcomes
        and hitf >= min_hit_rate
        and mean_drop_best > FRICTION_PCT   # still profitable after losing best ticket
    )

    return WalletScore(
        wallet=wallet, n=n,
        hit_rate_0=round(hit0, 3), hit_rate_friction=round(hitf, 3), hit_rate_50=round(hit50, 3),
        median=round(median, 1), mean=round(mean, 1), mean_drop_best=round(mean_drop_best, 1),
        best_share=round(best_share, 3), proven_score=round(proven_score, 1), is_proven=is_proven,
    )


def rank_wallets(outcomes_by_wallet: dict[str, list[float]], **kw) -> list[WalletScore]:
    scores = [score_wallet(w, o, **kw) for w, o in outcomes_by_wallet.items()]
    scores.sort(key=lambda s: s.proven_score, reverse=True)
    return scores


def load_outcomes(path: str = WALLET_OUTCOMES_FILE) -> dict[str, list[float]]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {w: [float(x) for x in v] for w, v in raw.items() if isinstance(v, list)}


def _report():
    data = load_outcomes()
    scores = rank_wallets(data)
    proven = [s for s in scores if s.is_proven]
    print(f"Wallets: {len(scores)} | Proven (n>={MIN_OUTCOMES}, hit@{WIN_THRESHOLD:.0f}%>={MIN_HIT_RATE:.0%}, mean-drop-best>+{FRICTION_PCT:.0f}%): {len(proven)}")
    # Sensitivity: how many qualify under looser bars
    for hr in (0.15, 0.20, 0.25, 0.30):
        c = sum(1 for s in scores if s.n >= MIN_OUTCOMES and s.hit_rate_friction >= hr and s.mean_drop_best > FRICTION_PCT)
        print(f"  proven @ hit-rate>={hr:.0%}: {c}")
    print()
    hdr = f"{'wallet':<14}{'n':>4}{'hit@0':>7}{'hit@15':>7}{'hit@50':>7}{'med':>7}{'mean':>8}{'mean-xb':>8}{'best%':>7}{'score':>7}{'PROV':>6}"
    print(hdr); print("-" * len(hdr))
    for s in scores[:25]:
        print(f"{s.wallet[:12]:<14}{s.n:>4}{s.hit_rate_0:>7.2f}{s.hit_rate_friction:>7.2f}{s.hit_rate_50:>7.2f}"
              f"{s.median:>7.0f}{s.mean:>8.1f}{s.mean_drop_best:>8.1f}{s.best_share:>7.2f}{s.proven_score:>7.1f}{'Y' if s.is_proven else '':>6}")


if __name__ == "__main__":
    _report()
