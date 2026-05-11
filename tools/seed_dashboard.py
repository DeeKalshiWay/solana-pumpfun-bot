"""
tools/seed_dashboard.py

Populates the paper-trading dashboard with synthetic trades so the
dashboard has data to show in environments where the live PumpPortal
WebSocket isn't reachable (Cloudflare-blocked sandbox IPs).

Differs from tools/paper_smoke.py in that this one persists through
the real RiskManager + TradeDB so the dashboard's /api/status,
/api/trades, /api/signals all reflect the seeded activity after the
bot is restarted.

Usage:
    # Stop any running bot first.
    rm -f logs/paper_wallet.json logs/risk_state.json logs/trades.db
    PAPER_STARTING_SOL=2.5 python -m tools.seed_dashboard
    PAPER_TRADING=1 PAPER_STARTING_SOL=2.5 python main.py
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["PAPER_TRADING"] = "1"
os.environ.setdefault("PAPER_STARTING_SOL", "2.5")

from analyzer.signal_fusion import compute_fusion       # noqa: E402
from analyzer.signal_scorer import SignalScorer         # noqa: E402
from config import (                                    # noqa: E402
    FUSION_MAX_BONUS,
    FUSION_MAX_PENALTY,
)
from risk.manager import RiskManager                    # noqa: E402
from trader.paper_executor import PaperExecutor         # noqa: E402
from trader.paper_wallet import PaperWallet             # noqa: E402


# Synthetic feature mixes — same catalog as tools/paper_smoke.py but with
# a wider price-multiple range so the resulting equity curve has visible
# wins AND losses for the dashboard chart.
SYNTHETIC = [
    # Realistic: every entry passes the score filter (strong feature mix
    # at score time), then the price-multiple controls outcome. That's
    # the live failure mode — high-confidence buys that don't pan out.
    ("ORGA",  0.10, 40, 30, 22,  4, 18, 35, 25, 4,  2, True,  0.30, 2.6),  # big win
    ("VELO",  0.20, 35, 28, 22,  5, 12, 28, 18, 4,  1, False, 0.05, 1.5),  # win
    ("XHYP",  0.25, 30, 25, 20,  4,  8, 22, 14, 6,  1, False, 0.18, 1.6),  # win
    ("NEUT",  0.20, 32, 20, 18,  5,  5, 20, 12, 3,  1, False, 0.0,  1.05), # tiny win
    ("FLAT",  0.15, 38, 22, 16,  4,  2, 22, 10, 2,  1, False, 0.0,  0.92), # small loss
    ("STOP",  0.20, 36, 24, 20,  5, -3, 22, 12, 3,  1, False, 0.0,  0.82), # stop
    ("MOON",  0.08, 50, 25, 28,  3, 22, 60, 30, 4,  3, True,  0.20, 4.5),  # moonshot
    ("DUMP",  0.18, 34, 26, 22,  6,  4, 24, 16, 3,  1, False, 0.10, 0.45), # rug
]
# Field order: symbol, init_buy, curve_pct, mc_sol, buys_5m, sells_5m,
#              price_5m, holder_count, pf_replies, pf_cv, smart_buyers,
#              x_hype, influencer_mention, price_mult_on_exit


def build_token(row, idx: int) -> dict:
    sym, ib, curve, mc, b5, s5, p5, hold, pr, cv, sm, xh, im, _mult = row
    return {
        "mint":               (sym + str(idx))[:4] + "1" * 40,
        "symbol":             sym,
        "name":               sym + " coin",
        "creator":            sym[0] * 42,
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
        "smart_buyer_count":  sm,
        "x_hype_match":       xh,
        "influencer_mention": im if im else None,
        "image_uri":          "https://example/img.png",
        "age_minutes":        random.uniform(1, 5),
    }


async def main() -> int:
    random.seed(7)   # deterministic feed

    wallet   = PaperWallet(float(os.environ["PAPER_STARTING_SOL"]))
    executor = PaperExecutor(wallet)
    await executor.start()

    risk_mgr = RiskManager(wallet, executor)
    await risk_mgr.initialize()

    scorer = SignalScorer(raw_queue=asyncio.Queue(), trade_queue=asyncio.Queue())

    print(f"Seeding dashboard from {await wallet.get_sol_balance():.4f} SOL starting balance...")

    closed = 0
    for idx, row in enumerate(SYNTHETIC):
        token = build_token(row, idx)
        price_mult = row[-1]

        base_score, breakdown = scorer._compute_score(token)
        token["raw_score"] = base_score
        fusion_delta, fusion_breakdown = compute_fusion(
            token, FUSION_MAX_BONUS, FUSION_MAX_PENALTY,
        )
        final = max(0, min(100, base_score + fusion_delta))
        token["score"] = final
        token["score_breakdown"] = breakdown
        token["fusion_patterns"] = fusion_breakdown
        token["scored_at"] = time.time()

        if final < 60:
            print(f"  REJECT {token['symbol']:<6} score={final}")
            continue

        sol_amount = min(0.05, await wallet.get_sol_balance() * 0.05)
        if sol_amount <= 0:
            break

        buy = await executor.buy(token["mint"], sol_amount, token=token)
        if not buy["success"]:
            print(f"  BUY FAILED {token['symbol']}: {buy.get('error')}")
            continue

        risk_mgr.open_position(token, buy)

        tokens_received = buy["tokens_expected"]
        entry_price     = buy["sol_spent"] / tokens_received
        exit_price      = entry_price * price_mult
        executor.update_price(token["mint"], exit_price)

        # Decide a reason based on the price multiple to mimic real exit-class
        # distribution. The dashboard groups trades by reason; varied reasons
        # make the trades panel readable.
        if price_mult >= 2.5: reason = "tp_2"
        elif price_mult >= 1.5: reason = "tp_1"
        elif price_mult >= 1.0: reason = "trailing_stop"
        elif price_mult >= 0.85: reason = "no_movement"
        elif price_mult >= 0.6: reason = "stop_loss"
        else: reason = "rug_exit"

        sell = await executor.sell(token["mint"], tokens_received, reason=reason)
        if not sell["success"]:
            print(f"  SELL FAILED {token['symbol']}: {sell.get('error')}")
            continue

        risk_mgr.close_position(token["mint"], sell)
        closed += 1

    stats = risk_mgr.get_stats()
    print()
    print(f"Closed trades:  {closed}")
    print(f"Final balance:  {await wallet.get_sol_balance():.4f} SOL")
    print(f"Win rate:       {stats['win_rate']*100:.1f}%")
    print(f"Total PnL:      {stats['total_pnl_sol']:+.4f} SOL")
    print()
    print("State persisted to logs/. Start the bot to see the dashboard.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
