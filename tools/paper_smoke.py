"""
tools/paper_smoke.py

Bounded end-to-end smoke test for the paper trading pipeline.

Why
----
The live WebSocket (PumpPortal) is Cloudflare-blocked from many networks
and requires an upstream subscription. This harness sidesteps that by
INJECTING synthetic tokens through the same scorer → fusion → trade →
exit path the real bot uses. Useful for:
  - Validating a config change before live paper
  - Confirming a new factor or fusion pattern fires as expected
  - Sanity-checking that PAPER_STARTING_SOL=<X> behaves correctly

What it does
------------
1. Builds 7 synthetic tokens with deliberately varied feature mixes that
   span the full fusion catalog (winners, losers, neutral, fusion-positive,
   fusion-negative).
2. Runs each through SignalScorer._compute_score and signal_fusion.compute_fusion.
3. For tokens that beat MIN_BUY_SCORE, executes a paper buy via PaperExecutor.
4. Drives synthetic price moves and exits each position via PaperExecutor.sell.
5. Prints a PnL summary keyed by fusion patterns fired so we can see which
   alignments paid.

Usage
-----
    PAPER_STARTING_SOL=2.5 python -m tools.paper_smoke
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path so module imports work when run via
# `python tools/paper_smoke.py` as well as `python -m tools.paper_smoke`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Force paper mode regardless of caller's .env so this never accidentally
# routes through a live executor.
os.environ["PAPER_TRADING"] = "1"
os.environ.setdefault("PAPER_STARTING_SOL", "2.5")

from analyzer.signal_fusion import compute_fusion  # noqa: E402
from analyzer.signal_scorer import SignalScorer  # noqa: E402
from config import (  # noqa: E402
    FUSION_ENABLED,
    FUSION_MAX_BONUS,
    FUSION_MAX_PENALTY,
    MAX_SOL_PER_TRADE,
    MIN_BUY_SCORE,
    PAPER_STARTING_SOL,
)
from trader.paper_executor import PaperExecutor  # noqa: E402
from trader.paper_wallet import PaperWallet  # noqa: E402


# ── Synthetic tokens ──────────────────────────────────────────────────────────
#
# Each entry is (description, token_dict, simulated_price_multiple). Price
# multiple is what the price moves to at exit (1.0 = flat, 0.5 = -50%, 3.0 = +200%).
# The multiple is applied to the entry price right before sell().
#
# Feature mixes target each fusion pattern:
#   TOK-A  organic_launch + smart_crowd_sync       (multi-positive)
#   TOK-B  social_confirmed                        (single-positive)
#   TOK-C  velocity_stack + prime_mc_smart         (multi-positive)
#   TOK-D  hype_no_followthrough                   (single-negative)
#   TOK-E  tape_divergence                         (single-negative)
#   TOK-F  no fusion patterns, base score only     (control)
#   TOK-G  fires positive + negative simultaneously (overlap)

SYNTHETIC = [
    (
        "TOK-A organic_launch + smart_crowd_sync",
        {
            "mint":               "AAAA" + "1" * 36,
            "symbol":             "ORGA",
            "name":               "Organic Alpha",
            "creator":            "C" + "r" * 41,
            "initial_buy_sol":    0.10,
            "bonding_curve_pct":  40,
            "v_sol_in_bonding":   30,
            "v_tokens_in_bonding": 1_000_000,
            "market_cap_sol":     30,
            "buys_5m":            22,
            "sells_5m":           4,
            "price_change_5m":    18,
            "price_change_1h":    35,
            "holder_count":       35,
            "pf_reply_count":     25,
            "pf_comment_velocity": 4,
            "pf_is_live":         True,
            "pf_has_twitter":     True,
            "smart_buyer_count":  2,
            "image_uri":          "https://example/img.png",
            "age_minutes":        3,
        },
        2.4,
    ),
    (
        "TOK-B social_confirmed (X hype + smart)",
        {
            "mint":               "BBBB" + "1" * 36,
            "symbol":             "XHYP",
            "name":               "X Hype Alpha",
            "creator":            "X" + "r" * 41,
            "initial_buy_sol":    0.25,
            "bonding_curve_pct":  22,
            "v_sol_in_bonding":   15,
            "v_tokens_in_bonding": 1_000_000,
            "market_cap_sol":     20,
            "buys_5m":            10,
            "sells_5m":           3,
            "price_change_5m":    8,
            "price_change_1h":    15,
            "holder_count":       18,
            "pf_reply_count":     8,
            "pf_comment_velocity": 6,
            "x_hype_match":       True,
            "smart_buyer_count":  1,
            "image_uri":          "https://example/img.png",
            "age_minutes":        2,
        },
        1.7,
    ),
    (
        "TOK-C velocity_stack + prime_mc_smart",
        {
            "mint":               "CCCC" + "1" * 36,
            "symbol":             "VELO",
            "name":               "Velocity Stack",
            "creator":            "V" + "r" * 41,
            "initial_buy_sol":    0.40,
            "bonding_curve_pct":  18,
            "v_sol_in_bonding":   12,
            "v_tokens_in_bonding": 1_000_000,
            "market_cap_sol":     45,
            "buys_5m":            25,
            "sells_5m":           5,
            "price_change_5m":    12,
            "price_change_1h":    22,
            "holder_count":       28,
            "pf_reply_count":     10,
            "pf_comment_velocity": 3,
            "smart_buyer_count":  1,
            "image_uri":          "https://example/img.png",
            "age_minutes":        4,
        },
        1.6,
    ),
    (
        "TOK-D hype_no_followthrough (rejected expected)",
        {
            "mint":               "DDDD" + "1" * 36,
            "symbol":             "PROMO",
            "name":               "Promo Only",
            "creator":            "P" + "r" * 41,
            "initial_buy_sol":    0.30,
            "bonding_curve_pct":  12,
            "v_sol_in_bonding":   8,
            "v_tokens_in_bonding": 1_000_000,
            "market_cap_sol":     12,
            "buys_5m":            3,
            "sells_5m":           5,
            "price_change_5m":    -2,
            "price_change_1h":    5,
            "holder_count":       9,
            "pf_reply_count":     3,
            "influencer_mention": True,
            "smart_buyer_count":  0,
            "image_uri":          "https://example/img.png",
            "age_minutes":        2,
        },
        0.65,
    ),
    (
        "TOK-E tape_divergence (rejected expected)",
        {
            "mint":               "EEEE" + "1" * 36,
            "symbol":             "DUMP",
            "name":               "Hype Dump",
            "creator":            "D" + "r" * 41,
            "initial_buy_sol":    0.50,
            "bonding_curve_pct":  20,
            "v_sol_in_bonding":   10,
            "v_tokens_in_bonding": 1_000_000,
            "market_cap_sol":     20,
            "buys_5m":            3,
            "sells_5m":           14,
            "price_change_5m":    -18,
            "price_change_1h":    -5,
            "holder_count":       12,
            "pf_reply_count":     22,
            "pf_comment_velocity": 12,
            "smart_buyer_count":  0,
            "image_uri":          "https://example/img.png",
            "age_minutes":        3,
        },
        0.4,
    ),
    (
        "TOK-F neutral (no fusion fires)",
        {
            "mint":               "FFFF" + "1" * 36,
            "symbol":             "NEUT",
            "name":               "Neutral",
            "creator":            "N" + "r" * 41,
            "initial_buy_sol":    0.60,
            "bonding_curve_pct":  20,
            "v_sol_in_bonding":   8,
            "v_tokens_in_bonding": 1_000_000,
            "market_cap_sol":     18,
            "buys_5m":            6,
            "sells_5m":           3,
            "price_change_5m":    5,
            "price_change_1h":    10,
            "holder_count":       12,
            "pf_reply_count":     4,
            "smart_buyer_count":  0,
            "image_uri":          "https://example/img.png",
            "age_minutes":        4,
        },
        1.1,
    ),
    (
        "TOK-G overlap: social_confirmed + tape_divergence",
        {
            "mint":               "GGGG" + "1" * 36,
            "symbol":             "MIXED",
            "name":               "Mixed Signals",
            "creator":            "M" + "r" * 41,
            "initial_buy_sol":    0.40,
            "bonding_curve_pct":  25,
            "v_sol_in_bonding":   15,
            "v_tokens_in_bonding": 1_000_000,
            "market_cap_sol":     25,
            "buys_5m":            4,
            "sells_5m":           9,
            "price_change_5m":    -8,
            "price_change_1h":    3,
            "holder_count":       16,
            "pf_reply_count":     15,
            "pf_comment_velocity": 6,
            "x_hype_match":       True,
            "smart_buyer_count":  1,
            "image_uri":          "https://example/img.png",
            "age_minutes":        3,
        },
        0.85,
    ),
]


def _line(s: str = "", char: str = "─", n: int = 78) -> str:
    return char * n if not s else f" {s} ".center(n, char)


async def main() -> int:
    print(_line("PAPER SMOKE TEST", "═"))
    print(f"Starting balance:  {PAPER_STARTING_SOL:.3f} SOL  (set via PAPER_STARTING_SOL)")
    print(f"Max per trade:     {MAX_SOL_PER_TRADE} SOL")
    print(f"Min buy score:     {MIN_BUY_SCORE}/100")
    print(f"Fusion:            enabled={FUSION_ENABLED}  cap=±{FUSION_MAX_BONUS}/-{FUSION_MAX_PENALTY}")
    print(_line())

    wallet   = PaperWallet(PAPER_STARTING_SOL)
    executor = PaperExecutor(wallet)
    await executor.start()

    # Build a scorer without starting its WebSocket loop. We only need
    # _compute_score; the queue plumbing is not exercised here.
    scorer = SignalScorer(raw_queue=asyncio.Queue(), trade_queue=asyncio.Queue())

    pnl_per_pattern: dict[str, list[float]] = {}
    summary_rows: list[tuple[str, str, int, str, float, float, str]] = []

    for desc, token, price_mult in SYNTHETIC:
        symbol = token["symbol"]
        mint   = token["mint"]

        # Run the four-factor + fusion compute path the real scorer uses.
        base_score, breakdown = scorer._compute_score(token)
        token["raw_score"] = base_score

        fusion_delta, fusion_breakdown = compute_fusion(
            token, FUSION_MAX_BONUS, FUSION_MAX_PENALTY,
        )
        final_score = max(0, min(100, base_score + fusion_delta))
        token["score"] = final_score
        token["score_breakdown"] = breakdown
        token["fusion_patterns"] = fusion_breakdown

        patterns = ",".join(fusion_breakdown.keys()) or "—"
        verdict_score = f"base={base_score:>3d} fusion={fusion_delta:+d} final={final_score:>3d}"

        if final_score < MIN_BUY_SCORE:
            print(
                f"[REJECT] {symbol:<6} | {verdict_score:<40} | patterns={patterns}\n"
                f"         {desc}"
            )
            summary_rows.append((symbol, "REJECT", final_score, patterns, 0.0, 0.0, "below_min_score"))
            continue

        # Size and buy.
        sol_amount = min(MAX_SOL_PER_TRADE, wallet._balance * 0.05)
        if sol_amount <= 0:
            print(f"[SKIP]   {symbol:<6} | no capacity")
            continue

        print(_line(f"BUY {symbol}", "·"))
        print(f"  {desc}")
        print(f"  {verdict_score} | patterns={patterns}")

        result = await executor.buy(mint, sol_amount, token=token)
        if not result.get("success"):
            print(f"  buy failed: {result.get('error')}")
            summary_rows.append((symbol, "BUY_FAIL", final_score, patterns, 0.0, 0.0, result.get("error", "?")))
            continue

        tokens_received = result["tokens_expected"]
        sol_spent       = result["sol_spent"]
        entry_price     = sol_spent / tokens_received

        # Simulate the price move and exit.
        exit_price = entry_price * price_mult
        executor.update_price(mint, exit_price)
        sell_res = await executor.sell(mint, tokens_received, reason="smoke_exit")
        sol_recv = sell_res.get("sol_received", 0.0)
        pnl      = sol_recv - sol_spent
        pnl_pct  = (pnl / sol_spent) * 100 if sol_spent else 0.0

        # Attribute PnL to each fusion pattern that fired (for the
        # per-pattern PnL roll-up at the end).
        for pat in fusion_breakdown.keys():
            pnl_per_pattern.setdefault(pat, []).append(pnl)

        verdict = "WIN " if pnl > 0 else "LOSS"
        print(f"  exit: spent={sol_spent:.4f} SOL  recv={sol_recv:.4f} SOL  "
              f"pnl={pnl:+.4f} SOL ({pnl_pct:+.1f}%)  {verdict}")
        summary_rows.append((symbol, "TRADE", final_score, patterns, sol_spent, pnl, verdict.strip()))

    final_balance = wallet._balance

    print(_line("RESULTS", "═"))
    print(f"{'sym':<7}{'action':<10}{'score':<8}{'patterns':<48}{'spent':>10}{'pnl':>10}  {'outcome':<10}")
    print(_line())
    for sym, action, sc, pats, spent, pnl, outcome in summary_rows:
        print(f"{sym:<7}{action:<10}{sc:<8}{pats[:46]:<48}{spent:>10.4f}{pnl:>+10.4f}  {outcome:<10}")

    print(_line())
    starting = PAPER_STARTING_SOL
    delta    = final_balance - starting
    pct      = (delta / starting) * 100 if starting else 0.0
    print(f"Starting balance:  {starting:.4f} SOL")
    print(f"Final balance:     {final_balance:.4f} SOL")
    print(f"Net PnL:           {delta:+.4f} SOL  ({pct:+.2f}%)")

    if pnl_per_pattern:
        print(_line("PnL by fusion pattern", "─"))
        for pat, pnls in sorted(pnl_per_pattern.items()):
            avg = sum(pnls) / len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            print(f"  {pat:<28} trades={len(pnls):>2}  wr={wins}/{len(pnls)}  "
                  f"avg_pnl={avg:+.4f} SOL  total={sum(pnls):+.4f} SOL")

    print(_line("DONE", "═"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
