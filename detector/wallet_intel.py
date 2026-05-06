"""
detector/wallet_intel.py

Two intel features that piggyback on the shared PumpFunMonitor WS:

  1. BOT WALLET ACCUMULATOR
     Tracks every buyer wallet across pump.fun launches. If a wallet has
     bought >= BOT_WALLET_THRESHOLD distinct mints, mark it as a sniper bot.

  2. BUNDLED-LAUNCH DETECTOR
     For each new mint, watch the first BUNDLE_WINDOW_S seconds of trades.
     If 2+ distinct non-creator wallets buy in that window, the launch is
     "bundled" — a coordinated promotion. Tag and reject on sight.

CONSOLIDATED: this module no longer opens its own WS. It registers
create+trade callbacks on the shared PumpFunMonitor so we use only ONE
PumpPortal connection per bot instance (PumpPortal 403-bans IPs that open
multiple connections).

Both intel feeds persist to disk so they accumulate value over weeks even
when the bot is offline.
"""

import asyncio
import json
import os
import time
from collections import defaultdict
from typing import Dict, Set
from loguru import logger


BOT_WALLET_FILE       = "logs/bot_wallets.json"
BUNDLE_LAUNCH_FILE    = "logs/bundled_launches.json"

# Thresholds
BOT_WALLET_THRESHOLD  = 50     # mints bought to qualify as a bot
BUNDLE_WINDOW_S       = 4      # seconds after create to look for bundle
BUNDLE_BUYER_LIMIT    = 2      # 2+ non-creator buyers in window = bundled


class WalletIntel:
    """
    Singleton-style intel store. One PumpPortal connection feeds both
    features. Persists to disk on each update so we don't lose data.
    """

    def __init__(self):
        self.running = False

        # Bot wallet store: address -> {"buys": int, "last_seen": ts, "mints": [recent 50 mints]}
        self._wallets: Dict[str, dict] = {}

        # Bundle store: mint -> {"creator": str, "early_buyers": set, "is_bundled": bool}
        self._bundles: Dict[str, dict] = {}

        # Snapshot of finalized bundle decisions: mint -> True/False
        self._bundle_decided: Dict[str, bool] = {}

        # Mints we are still in the bundle observation window for
        self._observing: Set[str] = set()

        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────
    def _load(self):
        os.makedirs("logs", exist_ok=True)
        if os.path.exists(BOT_WALLET_FILE):
            try:
                with open(BOT_WALLET_FILE, "r", encoding="utf-8") as f:
                    self._wallets = json.load(f)
                logger.info(f"[WALLET-INTEL] Loaded {len(self._wallets)} buyer wallets")
            except Exception as e:
                logger.warning(f"[WALLET-INTEL] Could not load wallets: {e}")
        if os.path.exists(BUNDLE_LAUNCH_FILE):
            try:
                with open(BUNDLE_LAUNCH_FILE, "r", encoding="utf-8") as f:
                    self._bundle_decided = json.load(f)
                logger.info(f"[WALLET-INTEL] Loaded {len(self._bundle_decided)} bundle decisions")
            except Exception as e:
                logger.warning(f"[WALLET-INTEL] Could not load bundles: {e}")

    def _atomic_write(self, path: str, data):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=True)
            os.replace(tmp, path)
        except Exception as e:
            logger.debug(f"[WALLET-INTEL] atomic write error ({path}): {e}")
            try: os.remove(tmp)
            except OSError: pass

    def _save_wallets(self):
        # Trim entries with only 1 buy to keep file size bounded
        trimmed = {k: v for k, v in self._wallets.items() if v["buys"] >= 2}
        self._atomic_write(BOT_WALLET_FILE, trimmed)

    def _save_bundles(self):
        self._atomic_write(BUNDLE_LAUNCH_FILE, self._bundle_decided)

    # ── Public query API ─────────────────────────────────────────────────────
    def is_bot_wallet(self, addr: str) -> bool:
        if not addr:
            return False
        w = self._wallets.get(addr, {})
        return w.get("buys", 0) >= BOT_WALLET_THRESHOLD

    def wallet_buys(self, addr: str) -> int:
        return self._wallets.get(addr, {}).get("buys", 0)

    def is_bundled_launch(self, mint: str) -> bool:
        """Return True if this mint was tagged as a bundled launch."""
        return self._bundle_decided.get(mint, False)

    def bundle_pending(self, mint: str) -> bool:
        """True if we're still inside the observation window for this mint."""
        return mint in self._observing

    def get_known_bots_count(self) -> int:
        return sum(1 for v in self._wallets.values() if v.get("buys", 0) >= BOT_WALLET_THRESHOLD)

    # ── Event ingestion ──────────────────────────────────────────────────────
    def _record_buyer(self, addr: str, mint: str):
        if not addr:
            return
        w = self._wallets.get(addr)
        if w is None:
            self._wallets[addr] = {
                "buys":      1,
                "first_seen": time.time(),
                "last_seen":  time.time(),
                "mints":      [mint],
            }
        else:
            w["buys"] += 1
            w["last_seen"] = time.time()
            mints = w.setdefault("mints", [])
            if mint not in mints:
                mints.append(mint)
                if len(mints) > 50:
                    w["mints"] = mints[-50:]
            if w["buys"] == BOT_WALLET_THRESHOLD:
                logger.warning(
                    f"[WALLET-INTEL] New bot wallet detected: "
                    f"{addr[:8]}... ({w['buys']} mints bought)"
                )

    def _on_create(self, data: dict):
        mint    = data.get("mint")
        creator = data.get("traderPublicKey")
        if not mint or not creator:
            return
        # Initial creator-buy itself counts toward their wallet stats
        self._record_buyer(creator, mint)
        self._bundles[mint] = {
            "creator":      creator,
            "created_at":   time.time(),
            "early_buyers": set(),
        }
        self._observing.add(mint)
        # Schedule the bundle decision after BUNDLE_WINDOW_S
        asyncio.create_task(self._finalize_bundle(mint))

    def _on_buy(self, data: dict):
        mint  = data.get("mint")
        buyer = data.get("traderPublicKey")
        if not mint or not buyer:
            return
        self._record_buyer(buyer, mint)
        # If we're still inside the create window for this mint, count it
        bundle = self._bundles.get(mint)
        if bundle is None:
            return
        if buyer == bundle["creator"]:
            return
        bundle["early_buyers"].add(buyer)

    async def _finalize_bundle(self, mint: str):
        await asyncio.sleep(BUNDLE_WINDOW_S)
        bundle = self._bundles.pop(mint, None)
        self._observing.discard(mint)
        if bundle is None:
            return
        n_buyers = len(bundle["early_buyers"])
        is_bundled = n_buyers >= BUNDLE_BUYER_LIMIT
        self._bundle_decided[mint] = is_bundled
        if is_bundled:
            logger.info(
                f"[WALLET-INTEL] Bundled launch detected: {mint[:8]}... "
                f"({n_buyers} early buyers in {BUNDLE_WINDOW_S}s window)"
            )
        # Save periodically — every 50 decisions is enough
        if len(self._bundle_decided) % 50 == 0:
            self._save_bundles()
        if len(self._wallets) % 100 == 0:
            self._save_wallets()

    # ── Subscription wiring ──────────────────────────────────────────────────
    def attach(self, monitor):
        """
        Register callbacks on the shared PumpFunMonitor so we get every
        create + trade event without opening our own WS connection.
        Call this once during bot startup, before monitor.run() begins.
        """
        monitor.subscribe_create(self._on_create)
        monitor.subscribe_trade(self._on_buy)
        logger.info("[WALLET-INTEL] Attached to shared PumpPortal WS")

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def run(self):
        """No WS of our own — just stay alive while callbacks fire."""
        self.running = True
        # Periodic save so data isn't lost on a crash
        while self.running:
            await asyncio.sleep(60)
            try:
                self._save_wallets()
                self._save_bundles()
            except Exception as e:
                logger.debug(f"[WALLET-INTEL] periodic save error: {e}")

    def stop(self):
        self.running = False
        # Final save on shutdown
        self._save_wallets()
        self._save_bundles()


# Singleton — imported by signal_scorer
wallet_intel = WalletIntel()
