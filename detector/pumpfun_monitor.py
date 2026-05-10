"""
detector/pumpfun_monitor.py
Monitors pump.fun new token launches via PumpPortal WebSocket.

CONSOLIDATED SINGLE-CONNECTION ARCHITECTURE:
This module is now the ONLY place that opens a WS to PumpPortal. It exposes
a pub/sub-style API so PumpFunTracker and WalletIntel can register callbacks
on the SAME connection without opening their own.

Why: PumpPortal will 403-ban IPs that open multiple parallel WS connections.
Going from 3 connections -> 1 connection eliminates the trigger.

Public API:
  monitor.subscribe_create(fn)  -> fn(data: dict) called on every create event
  monitor.subscribe_trade(fn)   -> fn(data: dict) called on every trade event

Bonding curve: virtual reserves start at 1M tokens + 5 SOL. Migration triggers
at ~85 SOL deposited beyond the virtual baseline.
"""

import asyncio
import json
import time
from collections.abc import Callable

import websockets
from loguru import logger

PUMPPORTAL_WS        = "wss://pumpportal.fun/api/data"
MIGRATION_SOL_TARGET = 85.0   # ~85 SOL triggers migration to Raydium

# Disguise our user-agent so PumpPortal doesn't trivially fingerprint us
# as a Python websockets client (the default UA can trigger bot defenses).
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)
_WS_HEADERS = [
    ("User-Agent", _BROWSER_UA),
    ("Origin",     "https://pump.fun"),
]


TRADE_SUB_DURATION_S = 12  # how long to keep trade-events flowing per new mint
                            # (slightly > BUNDLE_WINDOW_S=10 in wallet_intel for
                            # slack — PumpPortal subscribeTokenTrade has
                            # noticeable server-side latency, so we need the
                            # subscription open long enough for events to
                            # actually arrive before _finalize_bundle pops
                            # the mint from observation)


class PumpFunMonitor:
    def __init__(self, token_queue: asyncio.Queue):
        self.queue       = token_queue
        self.seen_mints  = set()
        self.running     = False

        # Pub/sub: any number of callables can register for create/trade events.
        # Callbacks must be cheap and synchronous (or schedule their own tasks).
        self._create_subs: list[Callable[[dict], None]] = []
        self._trade_subs:  list[Callable[[dict], None]] = []

        # Live WS handle so we can send subscribeTokenTrade messages from
        # within event callbacks. Reset on reconnect.
        self._ws = None
        # Mints we've actively subscribed to trades for, to avoid duplicate subs
        self._trade_subbed: set[str] = set()

    # ── Public pub/sub API ────────────────────────────────────────────────────
    def subscribe_create(self, fn: Callable[[dict], None]):
        """Register a callback for create events. Called for every new token."""
        self._create_subs.append(fn)

    def subscribe_trade(self, fn: Callable[[dict], None]):
        """Register a callback for buy/sell events on subscribed mints."""
        self._trade_subs.append(fn)

    async def _subscribe_mint_trades(self, mint: str):
        """
        Tell PumpPortal we want trade events for this mint, then auto-unsub
        after TRADE_SUB_DURATION_S so we don't accumulate subscriptions
        forever. Used by the bundle/bot-buyer detector to see who buys
        in the first ~4 seconds after launch.
        """
        if self._ws is None or mint in self._trade_subbed:
            return
        self._trade_subbed.add(mint)
        try:
            await self._ws.send(json.dumps({
                "method": "subscribeTokenTrade",
                "keys":   [mint],
            }))
        except Exception as e:
            logger.debug(f"subscribeTokenTrade send error: {e}")
            self._trade_subbed.discard(mint)
            return

        await asyncio.sleep(TRADE_SUB_DURATION_S)

        # Window done — unsubscribe to keep the WS load bounded.
        self._trade_subbed.discard(mint)
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({
                    "method": "unsubscribeTokenTrade",
                    "keys":   [mint],
                }))
            except Exception as e:
                logger.debug(f"unsubscribeTokenTrade send error: {e}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    async def run(self):
        self.running = True
        logger.info("PumpFun monitor starting (single shared WS to PumpPortal)...")
        await self.stream()

    def stop(self):
        self.running = False

    async def stream(self):
        backoff = 5
        while self.running:
            try:
                async with websockets.connect(
                    PUMPPORTAL_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    additional_headers=_WS_HEADERS,
                ) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    logger.info(
                        f"PumpPortal WS subscribed | create_subs={len(self._create_subs)} "
                        f"trade_subs={len(self._trade_subs)}"
                    )
                    backoff = 5

                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            await self._handle_message(raw)
                        except Exception as e:
                            logger.debug(f"WS msg error: {e}")
                    self._ws = None
                    self._trade_subbed.clear()

            except websockets.exceptions.ConnectionClosed:
                logger.warning(f"PumpPortal WS disconnected, reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
            except TypeError:
                # `additional_headers` was renamed in some websockets versions; fall back
                logger.warning("Retrying WS without additional_headers (lib version mismatch)")
                try:
                    async with websockets.connect(
                        PUMPPORTAL_WS, ping_interval=20, ping_timeout=10
                    ) as ws:
                        self._ws = ws
                        await ws.send(json.dumps({"method": "subscribeNewToken"}))
                        backoff = 5
                        async for raw in ws:
                            if not self.running:
                                break
                            try:
                                await self._handle_message(raw)
                            except Exception:
                                pass
                        self._ws = None
                        self._trade_subbed.clear()
                except Exception as e:
                    logger.error(f"PumpPortal WS fallback error: {e}")
                    await asyncio.sleep(backoff)
            except Exception as e:
                # Exponential backoff up to 5 min on 403 rate-limit
                msg = str(e)
                if "403" in msg or "rejected" in msg.lower():
                    backoff = min(backoff * 2, 300)
                    logger.error(f"PumpPortal WS 403 rate-limit; backoff {backoff}s")
                else:
                    logger.error(f"PumpPortal WS error: {e}")
                await asyncio.sleep(backoff)

    # ── Message dispatch ──────────────────────────────────────────────────────
    async def _handle_message(self, raw):
        try:
            data = json.loads(raw)
        except Exception:
            return

        tx_type = data.get("txType", "")

        if tx_type == "create":
            await self._dispatch_create(data)
        elif tx_type == "buy" or tx_type == "sell":
            self._dispatch_trade(data)

    def _dispatch_trade(self, data: dict):
        """Fan trade events out to subscribers (wallet_intel)."""
        for fn in self._trade_subs:
            try:
                fn(data)
            except Exception as e:
                logger.debug(f"trade sub error: {e}")

    async def _dispatch_create(self, data: dict):
        """
        Build the canonical token dict, push to scorer queue, and fan out to
        any subscribers (pumpfun_tracker, wallet_intel).
        """
        mint = data.get("mint")
        if not mint or mint in self.seen_mints:
            return
        self.seen_mints.add(mint)

        # Fan out raw create event first — wallet_intel/pumpfun_tracker may want
        # to see every create immediately even before queue insertion.
        for fn in self._create_subs:
            try:
                fn(data)
            except Exception as e:
                logger.debug(f"create sub error: {e}")

        # Open a short trade-event window for this mint so the bundle/bot-buyer
        # detector can see who else jumps in. Without this PumpPortal only sends
        # us create events — buys are silent — and the bundle detector stays dead.
        # Fire-and-forget; the helper unsubscribes itself after TRADE_SUB_DURATION_S.
        asyncio.create_task(self._subscribe_mint_trades(mint))

        initial_buy    = float(data.get("solAmount", 0))
        market_cap_sol = float(data.get("marketCapSol", 0))
        v_sol_in_bc    = float(data.get("vSolInBondingCurve", 0))
        v_tokens_in_bc = float(data.get("vTokensInBondingCurve", 0))

        actual_sol_deposited = max(0.0, v_sol_in_bc - 5.0)
        bonding_curve_pct    = min(
            (actual_sol_deposited / MIGRATION_SOL_TARGET) * 100, 100
        )

        token = {
            "mint":                 mint,
            "name":                 data.get("name", "Unknown"),
            "symbol":               data.get("symbol", "???"),
            "description":          "",
            "image_uri":            data.get("uri", ""),
            "twitter":              "",
            "telegram":             "",
            "website":              "",
            "creator":              data.get("traderPublicKey", ""),
            "created_ts":           time.time(),
            "age_minutes":          0,
            "market_cap_usd":       market_cap_sol * 150,
            "market_cap_sol":       market_cap_sol,
            "reply_count":          0,
            "initial_buy_sol":      initial_buy,
            "v_sol_in_bonding":     v_sol_in_bc,
            "v_tokens_in_bonding":  v_tokens_in_bc,
            "bonding_curve_pct":    round(bonding_curve_pct, 2),
            "bonding_curve_key":    data.get("bondingCurveKey", ""),
            "pool":                 data.get("pool", "pump"),
            "source":               "pumpportal_ws",
        }

        logger.info(
            f"[NEW] {token['symbol']} | {mint[:8]}... | "
            f"curve={bonding_curve_pct:.1f}% | "
            f"creator bought {initial_buy:.3f} SOL | MC: {market_cap_sol:.2f} SOL"
        )
        await self.queue.put(token)
