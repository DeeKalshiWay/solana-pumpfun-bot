"""
tools/seed_intel.py

Populates wallet_intel + whale_tracker + bundle/bot-target caches with
synthetic data so the dashboard's /api/intel panel and the scorer's
signal-fusion path have non-zero state to display in environments
where the live PumpPortal WebSocket is unreachable (sandbox IPs are
Cloudflare-blocked).

This is a UI/demo seed — NOT a strategy validation. It doesn't drive
real signals through the bot's decision pipeline. Use tools/paper_smoke.py
or tools/seed_dashboard.py for trade-level demos.

Usage:
    # Stop any running bot first.
    python -m tools.seed_intel
    # Then start the bot normally; it reloads the seeded state from disk.

After running, the dashboard's intel panel shows:
  - 5 smart wallets, 3 noise wallets (via outcome attribution)
  - 10 whales by lifetime SOL volume
  - 3 bundled launches, 2 bot-target launches
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Force paper mode for safety even though this script doesn't trade.
os.environ.setdefault("PAPER_TRADING", "1")

from detector.wallet_intel import wallet_intel  # noqa: E402
from detector.whale_tracker import (  # noqa: E402
    WHALE_MIN_LIFETIME_SOL,
    whale_tracker,
)


def _addr(prefix: str, idx: int) -> str:
    """44-char base58-looking pubkey (enough for serialization)."""
    return (prefix + str(idx)).ljust(44, "X")


def _mint(prefix: str, idx: int) -> str:
    return (prefix + "Mint" + str(idx)).ljust(44, "Z")


def seed_smart_wallets(n: int = 5) -> list[str]:
    """Each wallet gets enough outcomes to clear SMART_WALLET_MIN_BUYS,
    with WR above SMART_WALLET_WIN_PCT (≥60%) where a "win" is
    mc_delta_pct ≥ SMART_WALLET_PUMP_THRESHOLD (+50%)."""
    addrs = []
    for i in range(n):
        a = _addr("Smart", i)
        # 12 outcomes total, 9 winners ≥ +50% (75% WR > 60% threshold)
        outs = [random.uniform(60, 250) for _ in range(9)]
        outs += [random.uniform(-40, 20) for _ in range(3)]
        random.shuffle(outs)
        wallet_intel._outcomes[a] = outs
        # Mark them as "seen on chain" so the wallets dict has them too.
        wallet_intel._wallets[a] = {
            "buys":       len(outs),
            "first_seen": time.time() - 86_400,
            "last_seen":  time.time(),
            "mints":      [_mint("S", i)],
        }
        addrs.append(a)
    return addrs


def seed_noise_wallets(n: int = 3) -> list[str]:
    """Each gets enough outcomes + mostly losing to land in the noise bucket."""
    addrs = []
    for i in range(n):
        a = _addr("Noise", i)
        # 30 outcomes, 5 wins (16% WR < 30% threshold) → noise
        outs = [random.uniform(60, 120) for _ in range(5)]
        outs += [random.uniform(-90, -20) for _ in range(25)]
        random.shuffle(outs)
        wallet_intel._outcomes[a] = outs
        wallet_intel._wallets[a] = {
            "buys":       30,
            "first_seen": time.time() - 86_400,
            "last_seen":  time.time(),
            "mints":      [_mint("N", i)],
        }
        addrs.append(a)
    return addrs


def seed_whales(n: int = 10) -> list[str]:
    """Drive the whale tracker via its _on_trade hot path so volume
    aggregates the same way as live."""
    addrs = []
    for i in range(n):
        a = _addr("Whale", i)
        m = _mint("W", i)
        whale_tracker._on_create({"mint": m})
        # Enough volume to clear WHALE_MIN_LIFETIME_SOL=10
        trades = 25
        per_trade = (WHALE_MIN_LIFETIME_SOL + 5) / trades  # 0.6 SOL/trade × 25 = 15 SOL
        for _ in range(trades):
            whale_tracker._on_trade({
                "txType":          "buy",
                "traderPublicKey": a,
                "mint":            m,
                "solAmount":       per_trade,
            })
        addrs.append(a)
    return addrs


def seed_bundled_launches(n: int = 3) -> list[str]:
    """Mark mints as bundled in wallet_intel._bundle_decided + register
    their early-buyer sets so the api/intel panel shows them."""
    mints = []
    for i in range(n):
        m = _mint("Bundle", i)
        wallet_intel._bundle_decided[m] = True
        # Synthetic 6 early buyers (exceeds BUNDLE_BUYER_LIMIT=5)
        buyers = {_addr(f"BBuyer{i}_", j) for j in range(6)}
        wallet_intel._mint_buyers[m] = buyers
        mints.append(m)
    return mints


def seed_bot_target_launches(noise_addrs: list[str], n: int = 2) -> list[str]:
    """Flag mints where a known noise wallet bought early."""
    mints = []
    for i in range(n):
        m = _mint("Bot", i)
        wallet_intel._bot_buyer_mints[m] = True
        # Pair with a noise wallet that "bought" it
        if noise_addrs:
            wallet_intel._mint_buyers[m] = {noise_addrs[i % len(noise_addrs)]}
        mints.append(m)
    return mints


def main() -> int:
    random.seed(11)
    print("Seeding wallet_intel + whale_tracker for the dashboard intel panel...")

    smart_addrs = seed_smart_wallets()
    noise_addrs = seed_noise_wallets()
    whale_addrs = seed_whales()
    bundled     = seed_bundled_launches()
    bot_mints   = seed_bot_target_launches(noise_addrs)

    # Rebuild the smart/noise sets from the outcomes we just loaded.
    wallet_intel._reclassify()

    # Persist everything to disk in the same format the live bot uses.
    wallet_intel._save_wallets()
    wallet_intel._save_bundles()
    wallet_intel._save_bot_buyer_mints()
    wallet_intel._save_outcomes_snapshot()
    whale_tracker._save()

    n_smart = len(wallet_intel._smart_wallets)
    n_noise = len(wallet_intel._noise_wallets)
    print()
    print(f"  smart wallets persisted:    {n_smart}  (expected {len(smart_addrs)})")
    print(f"  noise wallets persisted:    {n_noise}  (expected {len(noise_addrs)})")
    print(f"  whale_count:                {whale_tracker.stats()['whale_count']}  "
          f"(expected {len(whale_addrs)})")
    print(f"  bundled launches:           {len(bundled)}")
    print(f"  bot-target launches:        {len(bot_mints)}")
    print()
    print("Done. Start the bot to see the populated intel panel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
