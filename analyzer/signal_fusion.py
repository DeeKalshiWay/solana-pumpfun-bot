"""
analyzer/signal_fusion.py

Composes the four-factor scorer's independent signals into ALIGNMENT
patterns. The 4-factor sum adds each signal on its own — fusion gives
an extra bonus (or penalty) when multiple independent signals agree,
which empirically is where edge lives.

Design principles
-----------------
1. Every pattern has a SMALL cap (≤ 8) so it can't dominate the base
   score. Total fusion bonus is hard-capped via FUSION_MAX_BONUS /
   FUSION_MAX_PENALTY in config.
2. Every fired pattern is recorded in the breakdown dict so the
   counterfactual log can attribute outcome lift to specific fusions.
   Without attribution we can't tell if the fusion is actually paying.
3. Patterns trigger on AND-conjunctions of EXISTING token-dict fields.
   No new I/O — composition only. (The new X-feed signal in
   detector/x_feed.py just sets a flag on the token dict.)
4. Negative alignment (loud chatter + real selling, or smart-money +
   rug-pattern match) gets a PENALTY. Fusion isn't only upside; the
   point is to use co-firing signals as confidence either way.
5. Toggle-able via config (FUSION_ENABLED). On by default; can be
   killed instantly via .env if it ever underperforms in holdout.

Patterns (initial set)
----------------------
Positive:
  SOCIAL_CONFIRMED   X/influencer hype + on-chain validation (smart buyer
                     or comment-velocity ≥5)
  ORGANIC_LAUNCH     Small init buy + ≥30% curve + buy_ratio ≥0.7 — the
                     "creator didn't bag-dump, demand is sustained" stack
  SMART_CROWD_SYNC   ≥2 smart buyers + ≥15 replies + comment-velocity ≥2
                     — smart capital + visible crowd attention
  VELOCITY_STACK     buys_5m ≥15 + price_5m >5 + holders ≥20 — multi-axis
                     buying surge (volume, price, holder count all agree)
  PRIME_MC_SMART     market cap in 25-60S sweet spot AND ≥1 smart buyer
  WHALE_CONFIRMED    ≥1 whale buyer (volume-classified, complement to
                     smart-money) AND corroboration (smart wallet or
                     dominant buy-side tape)
                     — well-priced AND well-bought

Negative:
  TAPE_DIVERGENCE    comment_velocity ≥5 BUT buy_ratio <0.4 AND
                     price_5m <0 — loud chatter, real selling. Honeypot
                     tape pattern.
  HYPE_NO_FOLLOWTHROUGH  influencer mention BUT zero smart buyers AND
                     buy_ratio <0.5 — promoter-only hype, no capital.
"""

from __future__ import annotations

from typing import Any

# Per-pattern bonuses/penalties. Small by design — fusion is a
# tie-breaker, not a primary scoring factor.
FUSION_PATTERNS_POSITIVE: dict[str, int] = {
    "social_confirmed":      8,
    "organic_launch":        7,
    "smart_crowd_sync":      8,
    "velocity_stack":        6,
    "prime_mc_smart":        4,
    # Whale presence + at least one corroborating signal. Independent of
    # smart_crowd_sync (which uses the WIN-RATE-classified smart wallets);
    # whales are classified by VOLUME. Both firing together is meaningful
    # because the populations are different — overlap is signal.
    "whale_confirmed":       7,
}

FUSION_PATTERNS_NEGATIVE: dict[str, int] = {
    "tape_divergence":      -8,
    "hype_no_followthrough": -5,
}


def _get(token: dict, key: str, default: Any = 0) -> Any:
    """Tolerant getter: treat None as default (some monitors emit None)."""
    v = token.get(key, default)
    return default if v is None else v


def detect_patterns(token: dict) -> list[str]:
    """Return the list of fusion pattern names that fire for this token.

    Pure function over the token dict. No I/O, no module state. Easy
    to unit-test and easy to replay against historical signals.
    """
    fired: list[str] = []

    # Field extraction — all default-safe.
    smart_buyers_n   = int(_get(token, "smart_buyer_count", 0))
    whale_buyers_n   = int(_get(token, "whale_buyer_count", 0))
    whale_buy_volume = float(_get(token, "whale_buy_volume", 0))
    influencer_hit   = bool(_get(token, "influencer_mention", False)) or \
                       bool(_get(token, "x_hype_match", False))
    comment_velocity = float(_get(token, "pf_comment_velocity", 0))
    pf_replies       = int(_get(token, "pf_reply_count", 0))
    buys_5m          = int(_get(token, "buys_5m", 0))
    sells_5m         = int(_get(token, "sells_5m", 0))
    total_5m         = buys_5m + sells_5m
    buy_ratio        = (buys_5m / total_5m) if total_5m > 0 else 0.0
    price_5m         = float(_get(token, "price_change_5m", 0))
    holders          = int(_get(token, "holder_count", 0))
    curve_pct        = float(_get(token, "bonding_curve_pct", 0))
    initial_buy      = float(_get(token, "initial_buy_sol", 0))
    mc_sol           = float(_get(token, "market_cap_sol", 0))

    # ── Positive alignments ──────────────────────────────────────────

    # SOCIAL_CONFIRMED: hype (X mention or influencer) + capital/engagement
    # validation. Either a smart wallet bought OR the pump.fun chat is
    # actively churning. Hype alone is the lowest-signal feature we have;
    # confirmed hype is meaningfully higher.
    if influencer_hit and (smart_buyers_n >= 1 or comment_velocity >= 5):
        fired.append("social_confirmed")

    # ORGANIC_LAUNCH: the post-cf89f61 "good launch" archetype. Small
    # initial buy (creator can't dump because they didn't load up),
    # past the 30% curve gate (rug rate drops sharply), buys outnumbering
    # sells. These three together were the pattern behind most winners
    # in the holdout validation analysis.
    if initial_buy < 0.30 and curve_pct >= 30 and total_5m >= 5 and buy_ratio >= 0.70:
        fired.append("organic_launch")

    # SMART_CROWD_SYNC: smart wallets buying AND the crowd is talking.
    # Smart-money-only entries can be silent (smart wallets sniping
    # alone is fine but may not graduate); smart + crowd is the
    # public-flywheel pattern.
    if smart_buyers_n >= 2 and pf_replies >= 15 and comment_velocity >= 2:
        fired.append("smart_crowd_sync")

    # VELOCITY_STACK: three independent volume/price/breadth axes all
    # firing at once. Each on its own is in the base scorer; the AND
    # is the surge confirmation.
    if buys_5m >= 15 and price_5m > 5 and holders >= 20:
        fired.append("velocity_stack")

    # PRIME_MC_SMART: market cap is in the 25-60 SOL "best entry" band
    # from the bot's own price-momentum factor, AND a smart wallet
    # bought. Validates the band heuristic with capital.
    if 25 <= mc_sol <= 60 and smart_buyers_n >= 1:
        fired.append("prime_mc_smart")

    # WHALE_CONFIRMED: a whale (volume-classified) bought AND at least
    # one corroborating signal — either a smart wallet (win-rate-classified,
    # different population) ALSO bought, OR the buy-side tape is dominant.
    # Whale entry alone is too easy to fake (single rich noob buys);
    # whale + corroboration is the conjunction we trust.
    if (whale_buyers_n >= 1 or whale_buy_volume >= 2.0) and (
        smart_buyers_n >= 1 or (total_5m >= 5 and buy_ratio >= 0.65)
    ):
        fired.append("whale_confirmed")

    # ── Negative alignments ──────────────────────────────────────────

    # TAPE_DIVERGENCE: chatter is loud but tape is selling. Classic
    # honeypot/exit-pump shape — shills writing, real wallets dumping.
    if comment_velocity >= 5 and total_5m >= 5 and buy_ratio < 0.40 and price_5m < 0:
        fired.append("tape_divergence")

    # HYPE_NO_FOLLOWTHROUGH: influencer or X mention fired but no
    # smart capital bought and the buy/sell tape is weak. Promoter-only
    # hype that the market is ignoring.
    if influencer_hit and smart_buyers_n == 0 and total_5m >= 5 and buy_ratio < 0.50:
        fired.append("hype_no_followthrough")

    return fired


def compute_fusion(
    token: dict,
    max_bonus: int   = 15,
    max_penalty: int = 10,
) -> tuple[int, dict[str, int]]:
    """Return (signed_delta, breakdown).

    breakdown maps pattern_name -> contribution (signed). Saved on the
    token so it surfaces in dashboards and counterfactual logs.
    """
    fired = detect_patterns(token)
    if not fired:
        return 0, {}

    breakdown: dict[str, int] = {}
    pos_sum = 0
    neg_sum = 0

    for name in fired:
        if name in FUSION_PATTERNS_POSITIVE:
            v = FUSION_PATTERNS_POSITIVE[name]
            breakdown[name] = v
            pos_sum += v
        elif name in FUSION_PATTERNS_NEGATIVE:
            v = FUSION_PATTERNS_NEGATIVE[name]
            breakdown[name] = v
            neg_sum += v   # already negative

    pos_capped = min(pos_sum,  max_bonus)
    neg_capped = max(neg_sum, -max_penalty)
    return pos_capped + neg_capped, breakdown
