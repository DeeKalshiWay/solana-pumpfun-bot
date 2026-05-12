"""Tests for analyzer.signal_fusion.

Fusion is a pure function over the token dict — no I/O, no globals.
These tests pin every alignment pattern's trigger conditions and the
cap behavior so future tweaks to the catalog don't silently regress.
"""
import pytest

from analyzer.signal_fusion import (
    FUSION_PATTERNS_NEGATIVE,
    FUSION_PATTERNS_POSITIVE,
    compute_fusion,
    detect_patterns,
)


def _base(**overrides) -> dict:
    """Baseline token dict that fires NO patterns. Tests override fields
    to exercise one pattern at a time."""
    t = {
        "smart_buyer_count":      0,
        "whale_buyer_count":      0,
        "whale_buy_volume":       0.0,
        "influencer_mention":     False,
        "x_hype_match":           False,
        "pf_comment_velocity":    0,
        "pf_reply_count":         0,
        "buys_5m":                0,
        "sells_5m":               0,
        "price_change_5m":        0,
        "holder_count":           0,
        "bonding_curve_pct":      0,
        "initial_buy_sol":        0,
        "market_cap_sol":         0,
    }
    t.update(overrides)
    return t


class TestSocialConfirmed:
    """X/influencer hype must coincide with smart capital or engagement."""

    def test_hype_alone_does_not_fire(self):
        assert "social_confirmed" not in detect_patterns(
            _base(influencer_mention=True)
        )

    def test_hype_plus_smart_buyer_fires(self):
        fired = detect_patterns(_base(influencer_mention=True, smart_buyer_count=1))
        assert "social_confirmed" in fired

    def test_hype_plus_comment_velocity_fires(self):
        fired = detect_patterns(_base(x_hype_match=True, pf_comment_velocity=5))
        assert "social_confirmed" in fired

    def test_x_hype_match_treated_same_as_influencer(self):
        a = detect_patterns(_base(influencer_mention=True, smart_buyer_count=1))
        b = detect_patterns(_base(x_hype_match=True,       smart_buyer_count=1))
        assert "social_confirmed" in a and "social_confirmed" in b


class TestOrganicLaunch:
    """Small init buy + ≥30% curve + buy-dominant tape."""

    def test_all_conditions_fires(self):
        fired = detect_patterns(_base(
            initial_buy_sol=0.10, bonding_curve_pct=35,
            buys_5m=8, sells_5m=2,
        ))
        assert "organic_launch" in fired

    def test_big_init_buy_kills_it(self):
        fired = detect_patterns(_base(
            initial_buy_sol=0.50, bonding_curve_pct=35,
            buys_5m=8, sells_5m=2,
        ))
        assert "organic_launch" not in fired

    def test_low_curve_kills_it(self):
        fired = detect_patterns(_base(
            initial_buy_sol=0.10, bonding_curve_pct=20,
            buys_5m=8, sells_5m=2,
        ))
        assert "organic_launch" not in fired

    def test_weak_buy_ratio_kills_it(self):
        fired = detect_patterns(_base(
            initial_buy_sol=0.10, bonding_curve_pct=35,
            buys_5m=4, sells_5m=6,   # 40% buy ratio
        ))
        assert "organic_launch" not in fired


class TestSmartCrowdSync:
    """Smart wallets buying AND public chat is alive."""

    def test_fires_when_all_present(self):
        fired = detect_patterns(_base(
            smart_buyer_count=2, pf_reply_count=20, pf_comment_velocity=3,
        ))
        assert "smart_crowd_sync" in fired

    def test_single_smart_not_enough(self):
        fired = detect_patterns(_base(
            smart_buyer_count=1, pf_reply_count=20, pf_comment_velocity=3,
        ))
        assert "smart_crowd_sync" not in fired


class TestVelocityStack:
    """3-axis surge: volume, price, holders."""

    def test_fires(self):
        fired = detect_patterns(_base(
            buys_5m=20, price_change_5m=10, holder_count=25,
        ))
        assert "velocity_stack" in fired

    def test_missing_holders_kills_it(self):
        fired = detect_patterns(_base(
            buys_5m=20, price_change_5m=10, holder_count=5,
        ))
        assert "velocity_stack" not in fired


class TestPrimeMcSmart:
    """25-60 SOL MC + at least one smart buyer."""

    def test_fires_in_band(self):
        fired = detect_patterns(_base(market_cap_sol=40, smart_buyer_count=1))
        assert "prime_mc_smart" in fired

    def test_outside_band_misses(self):
        fired = detect_patterns(_base(market_cap_sol=80, smart_buyer_count=1))
        assert "prime_mc_smart" not in fired

    def test_no_smart_misses(self):
        fired = detect_patterns(_base(market_cap_sol=40, smart_buyer_count=0))
        assert "prime_mc_smart" not in fired


class TestWhaleConfirmed:
    """Whale (volume-classified) buyer + corroboration. Whales alone
    aren't enough — a single rich noob is too easy to fake — but
    whale + smart wallet OR whale + dominant buy tape is the conjunction
    the pattern catches."""

    def test_whale_alone_does_not_fire(self):
        fired = detect_patterns(_base(whale_buyer_count=1))
        assert "whale_confirmed" not in fired

    def test_whale_plus_smart_fires(self):
        fired = detect_patterns(_base(whale_buyer_count=1, smart_buyer_count=1))
        assert "whale_confirmed" in fired

    def test_whale_plus_dominant_buy_tape_fires(self):
        fired = detect_patterns(_base(
            whale_buyer_count=1, buys_5m=8, sells_5m=2,   # 80% buy ratio
        ))
        assert "whale_confirmed" in fired

    def test_whale_volume_threshold_alone_qualifies_as_whale_side(self):
        """Volume gate (≥2 SOL) substitutes for whale_count — useful when
        the count is zero but a single ticker-by-ticker query is at the
        edge of classification."""
        fired = detect_patterns(_base(
            whale_buyer_count=0, whale_buy_volume=2.5, smart_buyer_count=1,
        ))
        assert "whale_confirmed" in fired

    def test_weak_corroboration_kills_it(self):
        """Whale + flat tape + no smart = no fire."""
        fired = detect_patterns(_base(
            whale_buyer_count=1, smart_buyer_count=0,
            buys_5m=2, sells_5m=2,   # 50% buy ratio, below 65% threshold
        ))
        assert "whale_confirmed" not in fired


class TestNegativeAlignments:
    """Co-fired negatives must dock the score, not boost it."""

    def test_tape_divergence_fires(self):
        fired = detect_patterns(_base(
            pf_comment_velocity=10,
            buys_5m=2, sells_5m=8,        # 20% buy ratio
            price_change_5m=-15,
        ))
        assert "tape_divergence" in fired

    def test_hype_no_followthrough_fires(self):
        fired = detect_patterns(_base(
            influencer_mention=True,
            smart_buyer_count=0,
            buys_5m=3, sells_5m=4,        # 43% buy ratio
        ))
        assert "hype_no_followthrough" in fired

    def test_negative_pattern_yields_negative_delta(self):
        delta, _ = compute_fusion(_base(
            pf_comment_velocity=10,
            buys_5m=2, sells_5m=8,
            price_change_5m=-15,
        ))
        assert delta < 0


class TestComputeFusion:
    """End-to-end: detect_patterns + capping + breakdown shape."""

    def test_no_patterns_returns_zero(self):
        delta, breakdown = compute_fusion(_base())
        assert delta == 0
        assert breakdown == {}

    def test_breakdown_is_signed_per_pattern(self):
        delta, breakdown = compute_fusion(_base(
            influencer_mention=True, smart_buyer_count=1,
        ))
        assert delta > 0
        assert breakdown["social_confirmed"] == FUSION_PATTERNS_POSITIVE["social_confirmed"]

    def test_positive_cap_enforced(self):
        # Fire every positive pattern at once. Raw sum would exceed the
        # default cap of 15 — the cap must clamp it.
        token = _base(
            # social_confirmed
            influencer_mention=True, smart_buyer_count=2,
            pf_comment_velocity=10, pf_reply_count=20,
            # organic_launch
            initial_buy_sol=0.10, bonding_curve_pct=40,
            buys_5m=20, sells_5m=2,    # also satisfies velocity_stack
            # velocity_stack already covered by buys_5m / price
            price_change_5m=10, holder_count=30,
            # prime_mc_smart
            market_cap_sol=35,
        )
        delta, breakdown = compute_fusion(token, max_bonus=15)
        # raw_positive_sum = 8 + 7 + 8 + 6 + 4 = 33, capped at 15
        assert delta == 15
        # breakdown still records every individual contribution (so the
        # counterfactual log can see WHICH patterns fired, not just the
        # capped sum).
        assert sum(v for v in breakdown.values() if v > 0) == 33

    def test_negative_cap_enforced(self):
        # Both negative patterns ALSO trigger social_confirmed (because
        # influencer_mention + comment_velocity≥5 is the same fork that
        # the positive pattern reads). Test the cap math directly:
        # raw negatives must sum below the cap, and the delta must reflect
        # the capped negative summed with the (separately capped) positive.
        token = _base(
            influencer_mention=True,
            smart_buyer_count=0,
            pf_comment_velocity=10,
            buys_5m=2, sells_5m=8,
            price_change_5m=-15,
        )
        delta, breakdown = compute_fusion(token, max_bonus=15, max_penalty=10)

        raw_pos = sum(v for v in breakdown.values() if v > 0)
        raw_neg = sum(v for v in breakdown.values() if v < 0)
        # tape_divergence(-8) + hype_no_followthrough(-5) = -13
        assert raw_neg == -13
        # social_confirmed(+8) is the only positive that fires
        assert raw_pos == 8

        # Cap is applied: negatives clamp to -10, positives clamp to +15.
        # delta = pos_capped + neg_capped = 8 + (-10) = -2
        assert delta == -2

    def test_negative_cap_isolated(self):
        # Isolate negative-cap math by zeroing the positive cap. This pins
        # the cap mechanism independent of pattern-overlap noise.
        token = _base(
            influencer_mention=True,
            smart_buyer_count=0,
            pf_comment_velocity=10,
            buys_5m=2, sells_5m=8,
            price_change_5m=-15,
        )
        delta, breakdown = compute_fusion(token, max_bonus=0, max_penalty=10)
        assert sum(v for v in breakdown.values() if v < 0) == -13
        # pos_capped = 0, neg_capped = -10
        assert delta == -10

    def test_positive_minus_negative_sums(self):
        token = _base(
            influencer_mention=True, smart_buyer_count=1,   # +8 social_confirmed
            pf_comment_velocity=10,
            buys_5m=2, sells_5m=8, price_change_5m=-15,     # -8 tape_divergence
        )
        delta, breakdown = compute_fusion(token)
        # Both patterns fire; +8 - 8 = 0 (still tracked in breakdown).
        assert "social_confirmed" in breakdown
        assert "tape_divergence"  in breakdown
        assert delta == 0


class TestNoneToleration:
    """Some upstream monitors emit None for missing numeric fields.
    Treating None as 0 prevents TypeError crashes in the scorer loop."""

    def test_none_fields_do_not_crash(self):
        # All required numeric inputs explicitly set to None.
        delta, _ = compute_fusion({
            "smart_buyer_count":  None,
            "pf_comment_velocity": None,
            "pf_reply_count":     None,
            "buys_5m":            None,
            "sells_5m":           None,
            "price_change_5m":    None,
            "holder_count":       None,
            "bonding_curve_pct":  None,
            "initial_buy_sol":    None,
            "market_cap_sol":     None,
        })
        assert delta == 0


@pytest.mark.parametrize("name,_", FUSION_PATTERNS_POSITIVE.items())
def test_every_positive_pattern_is_capped(name, _):
    """Defensive: nobody accidentally raises a pattern bonus above the
    fusion cap, which would let a single pattern dominate the score."""
    DEFAULT_MAX_BONUS = 15
    assert FUSION_PATTERNS_POSITIVE[name] <= DEFAULT_MAX_BONUS, (
        f"{name} bonus exceeds default FUSION_MAX_BONUS"
    )


@pytest.mark.parametrize("name,_", FUSION_PATTERNS_NEGATIVE.items())
def test_every_negative_pattern_is_capped(name, _):
    DEFAULT_MAX_PENALTY = 10
    assert FUSION_PATTERNS_NEGATIVE[name] >= -DEFAULT_MAX_PENALTY, (
        f"{name} penalty exceeds default FUSION_MAX_PENALTY"
    )
