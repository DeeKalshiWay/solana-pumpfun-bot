"""Regression test for SignalScorer's no-symbol filter.

Live observation on a 12h live-paper run: tokens with `???` symbol
(no metadata) consistently produced impossible PnL (+30,000%, +21,000%,
+3,200%) — 17 of 33 closed trades, accounting for ~12 SOL of fake
"credited wins" on a 2.5 SOL bankroll.

Root cause: these tokens migrate to Raydium within seconds; the
bonding-curve PDA closes; the fallback price feeds (Jupiter /
DexScreener) return corrupted quotes from thin or manipulated
post-migration pools. The executor and wallet math are honest; the
input is junk.

An earlier audit (2026-05-10) had disabled the no-symbol filter,
citing 0% rug rate on `???` tokens — but that was measured with the
same corrupted feed, so fake +PnL was counted as "win." With the
filter re-enabled, the scorer rejects metadata-less mints before
any buy.

These tests pin the contract.
"""
import asyncio

import pytest

from analyzer.signal_scorer import SignalScorer


@pytest.fixture
def scorer():
    raw_q = asyncio.Queue()
    trade_q = asyncio.Queue()
    return SignalScorer(raw_q, trade_q)


def _good_token(symbol: str = "DOGE") -> dict:
    """Token shape that passes other hard filters (curve %, etc).
    Anything that the symbol-check needs to evaluate."""
    return {
        "mint":              "MintAddress1111111111111111111111111111111",
        "symbol":            symbol,
        "name":              "Doge Coin",
        "creator":           "CreatorAddress1111111111111111111111111111",
        "initial_buy_sol":   0.1,
        "bonding_curve_pct": 30.0,
        "market_cap_sol":    30.0,
        "v_sol_in_bonding":  30.0,
        "v_tokens_in_bonding": 1_000_000_000,
        "buys_5m":           10,
        "sells_5m":          3,
        "price_change_5m":   10.0,
        "holder_count":      25,
        "age_minutes":       1.0,
    }


class TestNoSymbolFilter:
    def test_missing_symbol_rejected(self, scorer):
        token = _good_token(symbol="???")
        token.pop("symbol", None)   # actually missing
        assert scorer._passes_hard_filters(token) is False
        assert token.get("reject_reason") == "no_symbol"

    def test_question_mark_symbol_rejected(self, scorer):
        """The exact pattern observed live — pump.fun's "???" placeholder
        for tokens whose metadata didn't parse."""
        token = _good_token(symbol="???")
        assert scorer._passes_hard_filters(token) is False
        assert token.get("reject_reason") == "no_symbol"

    def test_empty_string_symbol_rejected(self, scorer):
        token = _good_token(symbol="")
        assert scorer._passes_hard_filters(token) is False
        assert token.get("reject_reason") == "no_symbol"

    def test_real_symbol_accepted(self, scorer):
        """A normal token with a real name passes the symbol filter
        (and proceeds to the other hard checks)."""
        token = _good_token(symbol="DOGE")
        # Just make sure it doesn't reject on symbol grounds — other
        # filters may or may not pass depending on data.
        scorer._passes_hard_filters(token)
        assert token.get("reject_reason") != "no_symbol"

    def test_unusual_but_real_symbol_accepted(self, scorer):
        """Punctuation-heavy memecoin names (G.A.N>G, S.C.A.R.E observed
        live) should NOT be filtered — they're real names, not the ???
        sentinel."""
        for sym in ("S.C.A.R.E", "G.A.N>G", "Atty", "CTO"):
            token = _good_token(symbol=sym)
            scorer._passes_hard_filters(token)
            assert token.get("reject_reason") != "no_symbol", (
                f"symbol {sym!r} incorrectly rejected as no_symbol"
            )
