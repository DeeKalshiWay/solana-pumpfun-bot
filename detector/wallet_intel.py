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

from loguru import logger

BOT_WALLET_FILE       = "logs/bot_wallets.json"
BUNDLE_LAUNCH_FILE    = "logs/bundled_launches.json"
BOT_BUYER_MINTS_FILE  = "logs/bot_buyer_mints.json"

# Smart-money classification (Plan A, 2026-05-10).
# A wallet's "buys" count alone catches both noise-bots AND known winners —
# they both buy dozens of mints. We split that population by *win rate* on
# the mints they bought, using counterfactual mc_delta_pct as the outcome.
WALLET_OUTCOMES_FILE        = "logs/wallet_outcomes.json"   # aggregated, written by bootstrap + live updates
MINT_EARLY_BUYERS_FILE      = "logs/mint_early_buyers.jsonl"  # append-only, mint -> [buyers]
SMART_WALLET_MIN_BUYS       = 10      # need this many outcomes to classify as smart
SMART_WALLET_WIN_PCT        = 0.60    # ≥60% of outcomes >= PUMP_THRESH = smart
SMART_WALLET_PUMP_THRESHOLD = 50.0    # mc_delta_pct ≥ +50% counts as a "win"
NOISE_WALLET_MIN_BUYS       = 25      # same as legacy bot_wallet threshold
NOISE_WALLET_MAX_WIN_PCT    = 0.30    # ≥25 buys AND <30% win = noise
MAX_OUTCOMES_PER_WALLET     = 200     # ring-buffer per wallet, latest first

# Thresholds
BOT_WALLET_THRESHOLD  = 25     # mints bought to qualify as a bot
                                # (lowered from 50 — no human buys 25+ pump.fun
                                # launches; this catches more sniper bots
                                # without meaningful false-positive risk)
BUNDLE_WINDOW_S       = 10     # seconds after create to look for bundle
                                # (raised from 4 — D2 fix attempt: PumpPortal
                                # subscribeTokenTrade has server-side latency
                                # that exceeded the old 4s window, so 0/118K
                                # mints were ever flagged. 10s gives the trade
                                # events time to actually arrive before we
                                # finalize the bundle decision.)
BUNDLE_BUYER_LIMIT    = 5      # 5+ non-creator buyers in window = bundled
                                # (raised from 2 — bundles can be legitimate
                                # team launches; only the extreme coordinated
                                # cases warrant rejection)
BOT_BUYER_MINTS_CAP   = 5000   # ring-buffer size; rugs are old data after a while


class WalletIntel:
    """
    Singleton-style intel store. One PumpPortal connection feeds both
    features. Persists to disk on each update so we don't lose data.
    """

    def __init__(self):
        self.running = False

        # Bot wallet store: address -> {"buys": int, "last_seen": ts, "mints": [recent 50 mints]}
        self._wallets: dict[str, dict] = {}

        # Bundle store: mint -> {"creator": str, "early_buyers": set, "is_bundled": bool}
        self._bundles: dict[str, dict] = {}

        # Snapshot of finalized bundle decisions: mint -> True/False
        self._bundle_decided: dict[str, bool] = {}

        # Mints where a known bot wallet was among the early buyers (any count,
        # not just bundles). Used to reject sniper-targeted launches even when
        # only one bot showed up — bundle threshold is too coarse.
        self._bot_buyer_mints: dict[str, bool] = {}

        # Mints we are still in the bundle observation window for
        self._observing: set[str] = set()

        # Smart-money classification state.
        # _outcomes:    wallet -> list of mc_delta_pct (ring-buffered)
        # _mint_buyers: mint -> set of early-buyer wallets (kept in memory so
        #               attribute_outcome doesn't have to re-read the JSONL).
        #               Survives via the append-only mint_early_buyers.jsonl
        #               which we replay on startup.
        # _smart/_noise: derived sets, refreshed after every reclassify.
        self._outcomes: dict[str, list[float]] = {}
        self._mint_buyers: dict[str, set[str]] = {}
        self._smart_wallets: set[str] = set()
        self._noise_wallets: set[str] = set()

        self._load()
        self._load_smart_money()

    # ── Persistence ──────────────────────────────────────────────────────────
    def _load(self):
        os.makedirs("logs", exist_ok=True)
        if os.path.exists(BOT_WALLET_FILE):
            try:
                with open(BOT_WALLET_FILE, encoding="utf-8") as f:
                    self._wallets = json.load(f)
                logger.info(f"[WALLET-INTEL] Loaded {len(self._wallets)} buyer wallets")
            except Exception as e:
                logger.warning(f"[WALLET-INTEL] Could not load wallets: {e}")
        if os.path.exists(BUNDLE_LAUNCH_FILE):
            try:
                with open(BUNDLE_LAUNCH_FILE, encoding="utf-8") as f:
                    self._bundle_decided = json.load(f)
                logger.info(f"[WALLET-INTEL] Loaded {len(self._bundle_decided)} bundle decisions")
            except Exception as e:
                logger.warning(f"[WALLET-INTEL] Could not load bundles: {e}")
        if os.path.exists(BOT_BUYER_MINTS_FILE):
            try:
                with open(BOT_BUYER_MINTS_FILE, encoding="utf-8") as f:
                    self._bot_buyer_mints = json.load(f)
                logger.info(f"[WALLET-INTEL] Loaded {len(self._bot_buyer_mints)} bot-buyer mint flags")
            except Exception as e:
                logger.warning(f"[WALLET-INTEL] Could not load bot-buyer mints: {e}")

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

    def _save_bot_buyer_mints(self):
        # Trim to ring-buffer cap so the file doesn't grow forever
        if len(self._bot_buyer_mints) > BOT_BUYER_MINTS_CAP:
            keys = list(self._bot_buyer_mints.keys())[-BOT_BUYER_MINTS_CAP:]
            self._bot_buyer_mints = {k: self._bot_buyer_mints[k] for k in keys}
        self._atomic_write(BOT_BUYER_MINTS_FILE, self._bot_buyer_mints)

    # ── Smart-money load / persist / classify ────────────────────────────────
    def _load_smart_money(self):
        """
        On startup: hydrate _outcomes from the aggregated json (one-shot
        snapshot written by bootstrap_smart_wallets.py) AND the in-memory
        mint -> early_buyers map from the append-only jsonl. Then reclassify.
        Both files are optional — without them we operate exactly like the
        legacy buy-count-only model.
        """
        if os.path.exists(WALLET_OUTCOMES_FILE):
            try:
                with open(WALLET_OUTCOMES_FILE, encoding="utf-8") as f:
                    raw = json.load(f)
                # Schema: { wallet_addr: [pct1, pct2, ...] }
                for w, outs in raw.items():
                    if isinstance(outs, list):
                        self._outcomes[w] = [float(p) for p in outs][-MAX_OUTCOMES_PER_WALLET:]
                logger.info(f"[WALLET-INTEL] Loaded outcomes for {len(self._outcomes)} wallets")
            except Exception as e:
                logger.warning(f"[WALLET-INTEL] Could not load wallet_outcomes: {e}")

        if os.path.exists(MINT_EARLY_BUYERS_FILE):
            try:
                with open(MINT_EARLY_BUYERS_FILE, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        mint = r.get("mint")
                        buyers = r.get("buyers") or []
                        if mint and buyers:
                            # Overwrite on duplicate keys (last write wins)
                            self._mint_buyers[mint] = set(buyers)
                logger.info(f"[WALLET-INTEL] Loaded early-buyer sets for {len(self._mint_buyers)} mints")
            except Exception as e:
                logger.warning(f"[WALLET-INTEL] Could not load mint_early_buyers: {e}")

        self._reclassify()
        logger.info(
            f"[WALLET-INTEL] Smart-money classes: "
            f"{len(self._smart_wallets)} smart / {len(self._noise_wallets)} noise / "
            f"{len(self._outcomes) - len(self._smart_wallets) - len(self._noise_wallets)} unclassified"
        )

    def _reclassify(self):
        """
        Walk every wallet with at least SMART_WALLET_MIN_BUYS outcomes and
        sort into smart / noise / unknown. O(n) over wallets with outcomes;
        runs on startup + after each attribute_outcome that crosses a
        threshold boundary. Cheap.
        """
        self._smart_wallets.clear()
        self._noise_wallets.clear()
        for w, outs in self._outcomes.items():
            n = len(outs)
            if n < SMART_WALLET_MIN_BUYS:
                continue
            wins = sum(1 for o in outs if o >= SMART_WALLET_PUMP_THRESHOLD)
            win_rate = wins / n
            if win_rate >= SMART_WALLET_WIN_PCT:
                self._smart_wallets.add(w)
            elif n >= NOISE_WALLET_MIN_BUYS and win_rate < NOISE_WALLET_MAX_WIN_PCT:
                self._noise_wallets.add(w)

    def _save_outcomes_snapshot(self):
        """
        Write the aggregated wallet_outcomes.json snapshot. Called sparingly —
        the append-only updates are durable on their own, this is just a
        startup convenience.
        """
        try:
            self._atomic_write(WALLET_OUTCOMES_FILE, self._outcomes)
        except Exception as e:
            logger.debug(f"[WALLET-INTEL] outcomes snapshot write error: {e}")

    def _append_mint_buyers(self, mint: str, buyers: set[str]):
        """Append one row to mint_early_buyers.jsonl. Bounded growth via
        size check + truncate at 20MB."""
        if not buyers:
            return
        try:
            with open(MINT_EARLY_BUYERS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({"mint": mint, "buyers": list(buyers)}) + "\n")
            # Soft cap: 20MB. Past that, drop the oldest half (file is replayed
            # in order so older rows are less valuable as the world drifts).
            if os.path.getsize(MINT_EARLY_BUYERS_FILE) > 20 * 1024 * 1024:
                with open(MINT_EARLY_BUYERS_FILE, encoding="utf-8") as f:
                    lines = f.readlines()
                keep = lines[len(lines) // 2:]
                tmp = MINT_EARLY_BUYERS_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(keep)
                os.replace(tmp, MINT_EARLY_BUYERS_FILE)
        except Exception as e:
            logger.debug(f"[WALLET-INTEL] mint_early_buyers append error: {e}")

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

    def has_bot_buyer(self, mint: str) -> bool:
        """
        True if any of this mint's first BUNDLE_WINDOW_S buyers is a known
        sniper-bot wallet. Tighter than is_bundled_launch — fires even when
        only ONE bot showed up (bundle threshold needs 2+).
        """
        return self._bot_buyer_mints.get(mint, False)

    def bundle_pending(self, mint: str) -> bool:
        """True if we're still inside the observation window for this mint."""
        return mint in self._observing

    def get_known_bots_count(self) -> int:
        return sum(1 for v in self._wallets.values() if v.get("buys", 0) >= BOT_WALLET_THRESHOLD)

    # ── Smart-money query API ────────────────────────────────────────────────
    def is_smart_wallet(self, addr: str) -> bool:
        """True iff this wallet has crossed the smart-money threshold
        (≥10 outcomes, ≥60% pumped ≥+50% within the counterfactual window)."""
        return bool(addr) and addr in self._smart_wallets

    def is_noise_wallet(self, addr: str) -> bool:
        """True iff this wallet has the high-volume / low-win-rate profile
        that the legacy is_bot_wallet() check was *trying* to flag, now
        sharpened with win-rate evidence."""
        return bool(addr) and addr in self._noise_wallets

    def wallet_class(self, addr: str) -> str:
        """Returns 'smart', 'noise', or 'unknown'. Use this when deciding
        whether a rejection should fire vs. a bonus should apply."""
        if not addr:
            return "unknown"
        if addr in self._smart_wallets:
            return "smart"
        if addr in self._noise_wallets:
            return "noise"
        return "unknown"

    def smart_buyers_in_window(self, mint: str) -> list[str]:
        """Return early-buyer wallets of `mint` that classify as smart."""
        buyers = self._mint_buyers.get(mint)
        if not buyers:
            return []
        return [b for b in buyers if b in self._smart_wallets]

    def noise_buyers_in_window(self, mint: str) -> list[str]:
        """Same as smart_buyers_in_window but for noise wallets."""
        buyers = self._mint_buyers.get(mint)
        if not buyers:
            return []
        return [b for b in buyers if b in self._noise_wallets]

    def get_smart_count(self) -> int:
        return len(self._smart_wallets)

    def attribute_outcome(self, mint: str, mc_delta_pct: float):
        """
        Called by counterfactual.CounterfactualLogger after a rejected mint's
        outcome resolves. Looks up every wallet that was an early buyer of
        the mint, appends the outcome to each wallet's ring-buffer, and
        triggers a reclassify if any wallet crossed the SMART_WALLET_MIN_BUYS
        boundary. Safe to call with mints we have no early-buyer record for —
        in that case it's a no-op.
        """
        buyers = self._mint_buyers.get(mint)
        if not buyers:
            return
        boundary_crossed = False
        for w in buyers:
            outs = self._outcomes.setdefault(w, [])
            prev_len = len(outs)
            outs.append(float(mc_delta_pct))
            if len(outs) > MAX_OUTCOMES_PER_WALLET:
                # ring-buffer: keep most recent
                self._outcomes[w] = outs[-MAX_OUTCOMES_PER_WALLET:]
            if prev_len < SMART_WALLET_MIN_BUYS <= prev_len + 1:
                boundary_crossed = True
        # Once a wallet hits the minimum buys threshold, classification can
        # flip. Reclassify is O(n_wallets_with_outcomes), cheap.
        if boundary_crossed:
            self._reclassify()

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

        # Persist the early-buyer set so counterfactual.attribute_outcome can
        # find it later. Also cache in-memory for fast smart_buyers_in_window
        # lookups from the scorer.
        if bundle["early_buyers"]:
            self._mint_buyers[mint] = set(bundle["early_buyers"])
            self._append_mint_buyers(mint, bundle["early_buyers"])

        # Tighter check: was any early buyer a known sniper bot? Fires even
        # when only ONE bot showed up (bundle requires 2+).
        bot_buyers = [a for a in bundle["early_buyers"] if self.is_bot_wallet(a)]
        if bot_buyers:
            self._bot_buyer_mints[mint] = True
            logger.info(
                f"[WALLET-INTEL] Bot-targeted launch: {mint[:8]}... "
                f"({len(bot_buyers)} known bot wallet(s) in {BUNDLE_WINDOW_S}s window)"
            )

        # Smart-money positive signal log. Useful for debugging the rewires.
        smart_buyers = [a for a in bundle["early_buyers"] if self.is_smart_wallet(a)]
        if smart_buyers:
            logger.info(
                f"[WALLET-INTEL] Smart-money launch: {mint[:8]}... "
                f"({len(smart_buyers)} known smart wallet(s) in {BUNDLE_WINDOW_S}s window)"
            )

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
        if len(self._bot_buyer_mints) % 25 == 0:
            self._save_bot_buyer_mints()

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
                self._save_bot_buyer_mints()
            except Exception as e:
                logger.debug(f"[WALLET-INTEL] periodic save error: {e}")

    def stop(self):
        self.running = False
        # Final save on shutdown
        self._save_wallets()
        self._save_bundles()
        self._save_bot_buyer_mints()
        # Outcomes snapshot is cheap; refresh so next startup is quick.
        if self._outcomes:
            self._save_outcomes_snapshot()


# Singleton — imported by signal_scorer
wallet_intel = WalletIntel()
