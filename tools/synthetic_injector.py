"""
tools/synthetic_injector.py

Async task that pushes synthetic pump.fun token events directly into the
bot's `raw_queue`, bypassing the PumpPortal WebSocket. Used in environments
where the live WS is unreachable (Cloudflare-blocked sandbox IPs) to
prove the rest of the pipeline — scorer + fusion + trade_loop + risk_mgr +
paper_executor — actually fires end-to-end.

⚠️  DEV TOOL ONLY. Gated on the `SYNTHETIC_INJECT` env var in main.py.
Default disabled. Never enable in a live-trading context — the synthetic
tokens have no real bonding curve and the bot would try to buy them.
Paper mode is the only safe context.

Usage:
    SYNTHETIC_INJECT=8 PAPER_TRADING=1 PAPER_STARTING_SOL=2.5 python main.py
"""

from __future__ import annotations

import asyncio
import random
import time

from loguru import logger


# Catalog of feature mixes designed to span the fusion engine's pattern
# space. Each tuple is consumed by `_build_token` below.
#
# Fields:
#   sym, init_buy_sol, curve_pct, mc_sol, buys_5m, sells_5m, price_5m,
#   holder, pf_replies, pf_cv, smart_buyers, whale_buyers, x_hype, influencer
SYNTHETIC_TOKENS = [
    # Strong organic launch — should fire organic_launch + smart_crowd_sync
    ("SYN_ORGA",  0.10, 40, 30, 22,  4, 18, 35, 25, 4,  2, 1, False, False),
    # Velocity stack
    ("SYN_VELO",  0.20, 35, 28, 22,  5, 12, 28, 18, 4,  1, 1, False, False),
    # Social-confirmed (X hype + smart)
    ("SYN_XHYP",  0.25, 30, 25, 20,  4,  8, 22, 14, 6,  1, 0, True,  False),
    # Mild — likely below threshold
    ("SYN_NEUT",  0.20, 32, 20, 18,  5,  5, 20, 12, 3,  0, 0, False, False),
    # Soft loss (passes filters)
    ("SYN_FLAT",  0.15, 38, 22, 16,  4,  2, 22, 10, 2,  1, 0, False, False),
    # Stop-class
    ("SYN_STOP",  0.20, 36, 24, 20,  5, -3, 22, 12, 3,  0, 0, False, False),
    # Moonshot — should fire whale_confirmed + organic_launch
    ("SYN_MOON",  0.08, 50, 25, 28,  3, 22, 60, 30, 4,  3, 2, True,  True),
    # Rug — should be rejected by tape_divergence
    ("SYN_DUMP",  0.18, 34, 26, 22,  6,  4, 24, 16, 3,  0, 0, False, False),
]


def _build_token(row: tuple, idx: int) -> dict:
    """Turn a catalog row into the dict shape `signal_scorer._score_token`
    expects from the `raw_queue`. Matches the fields the scorer reads at
    line ~84 onward."""
    (sym, ib, curve, mc, b5, s5, p5, hold,
     pr, cv, sm, wh, xh, im) = row
    return {
        "mint":               f"SYN_{idx:04d}".ljust(44, "X"),
        "symbol":             sym,
        "name":               f"Synthetic {sym}",
        "creator":            f"SYN_CR_{idx}".ljust(44, "X"),
        "initial_buy_sol":    ib,
        "bonding_curve_pct":  curve,
        "v_sol_in_bonding":   max(mc * 0.5, 5),
        "v_tokens_in_bonding": 1_000_000,
        "market_cap_sol":     mc,
        "buys_5m":            b5,
        "sells_5m":           s5,
        "price_change_5m":    p5,
        "price_change_1h":    p5 * 1.5,
        "holder_count":       hold,
        "pf_reply_count":     pr,
        "pf_comment_velocity": cv,
        "pf_is_live":         pr > 15,
        "pf_has_twitter":     True,
        "smart_buyer_count":  sm,           # scorer-side hint
        "whale_buyer_count":  wh,           # scorer-side hint
        "x_hype_match":       xh,
        "influencer_mention": im or None,
        "image_uri":          "https://example/img.png",
        "age_minutes":        random.uniform(1, 5),
        "_synthetic":         True,         # marker for downstream debugging
    }


async def inject_synthetic_tokens(
    raw_queue: "asyncio.Queue",
    count: int = 8,
    interval_s: float = 6.0,
) -> None:
    """Push `count` synthetic tokens onto `raw_queue`, sleeping
    `interval_s` between each so the scorer's parallel I/O has time to
    settle. Selects from SYNTHETIC_TOKENS cyclically and randomizes
    mints so risk_manager's per-symbol concentration cap doesn't reject
    everything as a duplicate."""
    random.seed(int(time.time()) & 0xFFFF)
    logger.warning(
        f"[SYNTHETIC] Injector active — pushing {count} synthetic tokens "
        f"every {interval_s:.1f}s. DO NOT enable in live trading."
    )
    for i in range(count):
        row = SYNTHETIC_TOKENS[i % len(SYNTHETIC_TOKENS)]
        token = _build_token(row, i)
        try:
            await raw_queue.put(token)
            logger.info(
                f"[SYNTHETIC] queued {token['symbol']} "
                f"(curve={row[2]}% mc={row[3]}S whales={row[10]} smart={row[9]})"
            )
        except Exception as e:
            logger.warning(f"[SYNTHETIC] queue put failed: {e}")
        await asyncio.sleep(interval_s)
    logger.warning(f"[SYNTHETIC] Injector done — {count} tokens pushed.")
