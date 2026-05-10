"""
trader/pumpportal_executor.py
Executes buys/sells on pump.fun bonding curve via PumpPortal Local Transaction API.
No API key required — we sign locally and send via our own RPC.
Free aside from Solana network fees + pump.fun's 1% bonding curve fee.
"""

import asyncio
import base64
import time

import aiohttp
from loguru import logger
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from config import PRIORITY_FEE_SOL, RPC_URL, RPC_URLS, SELL_PRIORITY_FEE_SOL, SLIPPAGE_BPS

PUMPPORTAL_LOCAL_API = "https://pumpportal.fun/api/trade-local"


class PumpPortalExecutor:
    """
    Bonding-curve execution for fresh pump.fun tokens Jupiter can't route.
    """

    def __init__(self, keypair: Keypair):
        self.keypair = keypair
        self.pubkey  = keypair.pubkey()
        self.session = None
        # Per-mint cache of how much SOL was spent on the buy. Used to scale
        # the sell priority fee so it never exceeds ~5% of position value.
        # Without this, a 0.005 SOL priority fee on a 0.007 SOL position is
        # 71% drag — fees alone burn the trade.
        self._buy_size_for_mint: dict[str, float] = {}

    async def start(self):
        self.session = aiohttp.ClientSession()

    async def stop(self):
        if self.session:
            await self.session.close()

    async def _build_tx(self, action: str, mint: str, amount, denominated_in_sol: bool):
        """Request a serialized transaction from PumpPortal."""
        # PumpPortal expects total priority fee in SOL (not per-CU microlamports).
        # Sells need higher priority during dumps; but at small trade sizes a
        # fixed 0.005 SOL fee becomes 50-70% drag. Scale it so it's always at
        # most ~5% of position value. For buys, position value = sol_amount.
        # For sells, look up the cached buy size for this mint.
        if action == "sell":
            position_value = self._buy_size_for_mint.get(mint, 0)
            if position_value > 0:
                priority_fee_sol = min(SELL_PRIORITY_FEE_SOL, position_value * 0.05)
            else:
                # No cached buy size (e.g. dump_orphans script with stale ATA);
                # fall back to a conservative cap so we don't burn an orphan.
                priority_fee_sol = min(SELL_PRIORITY_FEE_SOL, 0.001)
        else:  # buy
            sol_amount = amount if denominated_in_sol else 0
            priority_fee_sol = min(PRIORITY_FEE_SOL, max(sol_amount * 0.05, 0.0005))
        # Convert slippage bps to percent (PumpPortal uses percent, not bps)
        slippage_pct = SLIPPAGE_BPS / 100

        payload = {
            "publicKey":         str(self.pubkey),
            "action":            action,          # "buy" or "sell"
            "mint":              mint,
            "denominatedInSol":  "true" if denominated_in_sol else "false",
            "amount":            amount,
            "slippage":          slippage_pct,
            "priorityFee":       priority_fee_sol,
            "pool":              "auto",          # lets pump-amm/raydium auto-select
        }

        try:
            async with self.session.post(
                PUMPPORTAL_LOCAL_API,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"PumpPortal {resp.status}: {text[:200]}")
                    return None
                return await resp.read()  # raw bytes
        except TimeoutError:
            logger.warning("PumpPortal timeout")
            return None
        except Exception as e:
            logger.warning(f"PumpPortal exception: {e}")
            return None

    async def _sign_and_send(self, tx_bytes: bytes) -> str | None:
        """
        Sign the PumpPortal-built tx and race-submit it across every
        configured RPC. The same signature is valid on all of them — first
        successful response wins, the rest are harmless duplicate sends
        that the network deduplicates by signature.

        Drops Stage-3 tail latency: single-RPC submission stalls were
        the long pole when the leader RPC was congested or slow.
        """
        try:
            raw_tx = VersionedTransaction.from_bytes(tx_bytes)
            signed = VersionedTransaction(raw_tx.message, [self.keypair])
            signed_bytes = bytes(signed)
        except Exception as e:
            logger.warning(f"PP sign error: {e}")
            return None

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(signed_bytes).decode(),
                {
                    "encoding": "base64",
                    "skipPreflight": True,
                    "preflightCommitment": "processed",
                    "maxRetries": 3,
                }
            ]
        }

        async def _send_one(url: str) -> str | None:
            try:
                async with self.session.post(
                    url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    result = await resp.json()
                    if "error" in result:
                        err = result['error'].get('message', str(result['error']))
                        logger.debug(f"RPC rejected ({url[:30]}): {err[:120]}")
                        return None
                    return result.get("result")
            except Exception as e:
                logger.debug(f"RPC send error ({url[:30]}): {e}")
                return None

        urls = RPC_URLS or [RPC_URL]
        if len(urls) == 1:
            sig = await _send_one(urls[0])
            if not sig:
                logger.warning(f"RPC rejected (PP): single-RPC send failed")
            return sig

        # Race: first non-None signature wins, others are still in-flight
        # but harmless — the network dedupes by signature.
        tasks = [asyncio.create_task(_send_one(u)) for u in urls]
        winner_sig = None
        try:
            while tasks:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    # _send_one already swallows exceptions and returns None,
                    # but stay defensive — t.result() must never crash the race.
                    if t.cancelled() or t.exception() is not None:
                        continue
                    sig = t.result()
                    if sig:
                        winner_sig = sig
                        break
                if winner_sig:
                    for p in pending:
                        p.cancel()
                    # Drain cancelled tasks so asyncio doesn't warn about
                    # unawaited coroutines in some Python versions.
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    break
                tasks = list(pending)
        except Exception as e:
            logger.debug(f"PP multi-RPC race error: {e}")

        if not winner_sig:
            logger.warning(f"PP RPC race: all {len(urls)} endpoints failed")
        return winner_sig

    async def _confirm(self, signature: str, max_wait: int = 30) -> bool:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignatureStatuses",
            "params": [[signature], {"searchTransactionHistory": True}]
        }

        async def _poll_one(url: str) -> dict | None:
            """Single-RPC poll. Returns the status dict if the tx is confirmed
            or finalized (with or without error); None otherwise."""
            try:
                async with self.session.post(
                    url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    result = await resp.json()
                    statuses = result.get("result", {}).get("value", [])
                    if statuses and statuses[0]:
                        s = statuses[0]
                        if s.get("confirmationStatus") in ("confirmed", "finalized"):
                            return s
            except Exception:
                pass
            return None

        urls = RPC_URLS or [RPC_URL]
        start = time.time()
        while time.time() - start < max_wait:
            # Race all RPCs in parallel — first one to see "confirmed" wins.
            # Hot RPCs see the slot first; this drops confirm tail latency.
            tasks = [asyncio.create_task(_poll_one(u)) for u in urls]
            try:
                done, pending = await asyncio.wait(
                    tasks, timeout=5, return_when=asyncio.FIRST_COMPLETED,
                )
            except Exception:
                done, pending = set(), set(tasks)

            status = None
            for t in done:
                # Defensive: _poll_one swallows exceptions and returns None,
                # but never let a stray exception crash the confirm loop.
                if t.cancelled() or t.exception() is not None:
                    continue
                s = t.result()
                if s:
                    status = s
                    break

            for p in pending:
                p.cancel()
            # Drain cancelled tasks so asyncio doesn't warn about unawaited
            # coroutines on shutdown / some Python versions.
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            if status is not None:
                if status.get("err"):
                    logger.warning(f"PP tx {signature[:20]} failed: {status['err']}")
                    return False
                return True

            await asyncio.sleep(2)
        return False

    # ── Buy ───────────────────────────────────────────────────────────────────
    async def buy(self, token_mint: str, sol_amount: float) -> dict:
        logger.info(f"[PP BUY] {token_mint[:8]}... | {sol_amount} SOL")

        tx_bytes = await self._build_tx("buy", token_mint, sol_amount, True)
        if not tx_bytes:
            return {"success": False, "error": "pp_build_failed"}

        sig = await self._sign_and_send(tx_bytes)
        if not sig:
            return {"success": False, "error": "pp_send_failed"}

        confirmed = await self._confirm(sig)

        # Rough token estimate — we don't know exact output until we query post-trade
        # Use bondingCurveKey + virtual reserves if available on the token; otherwise defer
        result = {
            "success":         confirmed,
            "signature":       sig,
            "type":            "buy",
            "mint":            token_mint,
            "sol_spent":       sol_amount,
            "tokens_expected": 0,  # filled after position query
            "venue":           "pumpportal",
            "timestamp":       time.time(),
        }

        if confirmed:
            # Cache buy size so the matching sell can scale its priority fee.
            self._buy_size_for_mint[token_mint] = sol_amount
            logger.success(f"[PP BUY CONFIRMED] {token_mint[:8]} | {sol_amount} SOL | sig: {sig[:20]}")
        else:
            result["error"] = "unconfirmed"
            logger.warning(f"[PP BUY UNCONFIRMED] {token_mint[:8]} | sig: {sig[:20]}")
        return result

    async def prebuild_sell_tx(self, token_mint: str) -> bytes | None:
        """
        Public entry to pre-construct a sell-100% tx. Called by main right after
        a buy confirms so the bytes are sitting in memory waiting for any
        emergency exit (rug, stop-loss). The caller stashes the result on the
        Position; risk_manager passes it back to .sell() to skip _build_tx.
        Returns None on failure — caller falls back to normal sell path.
        """
        try:
            return await self._build_tx("sell", token_mint, "100%", False)
        except Exception as e:
            logger.debug(f"[PP PREBUILD] {token_mint[:8]} failed: {e}")
            return None

    # ── Sell ──────────────────────────────────────────────────────────────────
    async def sell(
        self,
        token_mint: str,
        token_amount_or_pct,
        reason: str = "exit",
        prebuilt_tx: bytes | None = None,
    ) -> dict:
        """
        token_amount_or_pct: either a raw token count (int) or a percentage string like "100%"

        prebuilt_tx: optional pre-built tx bytes (from a prior _build_tx call).
        When provided, skips the PumpPortal API roundtrip — used by risk_manager
        for emergency exits where the ~200-500ms _build_tx call matters.
        Solana blockhashes expire ~60s, so caller is responsible for freshness.
        """
        logger.info(f"[PP SELL] {token_mint[:8]}... | reason={reason}{' (prebuilt)' if prebuilt_tx else ''}")

        # For sells we can use percentage strings (PumpPortal supports "100%")
        # If we got an int, pass as token count
        amount = token_amount_or_pct
        denominated_in_sol = False  # selling is always token-denominated

        if prebuilt_tx is not None:
            tx_bytes = prebuilt_tx
        else:
            tx_bytes = await self._build_tx("sell", token_mint, amount, denominated_in_sol)
        if not tx_bytes:
            return {"success": False, "error": "pp_build_failed", "reason": reason}

        sig = await self._sign_and_send(tx_bytes)
        if not sig:
            return {"success": False, "error": "pp_send_failed", "reason": reason}

        confirmed = await self._confirm(sig)

        result = {
            "success":       confirmed,
            "signature":     sig,
            "type":          "sell",
            "mint":          token_mint,
            "reason":        reason,
            "sol_received":  0,  # estimate unknown pre-confirm
            "venue":         "pumpportal",
            "timestamp":     time.time(),
        }
        if confirmed:
            logger.success(f"[PP SELL CONFIRMED] {token_mint[:8]} | reason={reason} | sig: {sig[:20]}")
        else:
            result["error"] = "unconfirmed"
        return result
