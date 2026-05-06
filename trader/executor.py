"""
trader/executor.py
Smart router: Jupiter for Raydium/graduated tokens, PumpPortal for bonding-curve.
"""

import asyncio
import aiohttp
import base64
import time
from typing import Optional
from loguru import logger
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from config import (
    JUPITER_API_URL, RPC_URL, SLIPPAGE_BPS,
    PRIORITY_FEE_MICROLAMPORTS, SOL_MINT
)
from trader.pumpportal_executor import PumpPortalExecutor


MAX_PRICE_IMPACT_PCT = 40.0


class TradeExecutor:
    def __init__(self, keypair: Keypair):
        self.keypair = keypair
        self.pubkey  = keypair.pubkey()
        self.session = None
        self.pumpportal = PumpPortalExecutor(keypair)

    async def start(self):
        self.session = aiohttp.ClientSession()
        await self.pumpportal.start()

    async def stop(self):
        if self.session:
            await self.session.close()
        await self.pumpportal.stop()

    # ── Jupiter low-level ─────────────────────────────────────────────────────
    async def get_quote(self, input_mint, output_mint, amount_lamports, slippage_bps=None):
        slippage = slippage_bps or SLIPPAGE_BPS
        params = {
            "inputMint":   input_mint,
            "outputMint":  output_mint,
            "amount":      str(amount_lamports),
            "slippageBps": str(slippage),
            "onlyDirectRoutes": "false",
            "maxAccounts": "64",
        }
        try:
            async with self.session.get(
                f"{JUPITER_API_URL}/quote",
                params=params,
                timeout=aiohttp.ClientTimeout(total=6)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if "error" in data:
                    return None
                return data
        except Exception:
            return None

    async def _get_swap_transaction(self, quote):
        payload = {
            "quoteResponse":                quote,
            "userPublicKey":                str(self.pubkey),
            "wrapAndUnwrapSol":             True,
            "computeUnitPriceMicroLamports": PRIORITY_FEE_MICROLAMPORTS,
            "dynamicComputeUnitLimit":      True,
            "asLegacyTransaction":          False,
        }
        try:
            async with self.session.post(
                f"{JUPITER_API_URL}/swap",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("swapTransaction")
        except Exception:
            return None

    async def _sign_and_send(self, tx_base64: str) -> Optional[str]:
        try:
            tx_bytes = base64.b64decode(tx_base64)
            raw_tx = VersionedTransaction.from_bytes(tx_bytes)
            signed = VersionedTransaction(raw_tx.message, [self.keypair])
            signed_bytes = bytes(signed)

            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    base64.b64encode(signed_bytes).decode(),
                    {
                        "encoding": "base64",
                        "skipPreflight": False,
                        "preflightCommitment": "processed",
                        "maxRetries": 3,
                    }
                ]
            }
            async with self.session.post(
                RPC_URL, json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                result = await resp.json()
                if "error" in result:
                    err = result['error'].get('message', str(result['error']))
                    logger.warning(f"RPC rejected: {err[:200]}")
                    return None
                return result.get("result")
        except Exception as e:
            logger.warning(f"Sign/send error: {e}")
            return None

    async def _confirm(self, signature: str, max_wait: int = 30) -> bool:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignatureStatuses",
            "params": [[signature], {"searchTransactionHistory": True}]
        }
        start = time.time()
        while time.time() - start < max_wait:
            try:
                async with self.session.post(
                    RPC_URL, json=payload,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    result = await resp.json()
                    statuses = result.get("result", {}).get("value", [])
                    if statuses and statuses[0]:
                        status = statuses[0]
                        if status.get("confirmationStatus") in ("confirmed", "finalized"):
                            if status.get("err"):
                                return False
                            return True
            except Exception:
                pass
            await asyncio.sleep(2)
        return False

    async def _jupiter_buy(self, token_mint: str, sol_amount: float, quote: dict) -> dict:
        expected_out = int(quote.get("outAmount", 0))
        price_impact = float(quote.get("priceImpactPct", 0))

        tx_b64 = await self._get_swap_transaction(quote)
        if not tx_b64:
            return {"success": False, "error": "build_failed", "venue": "jupiter"}

        sig = await self._sign_and_send(tx_b64)
        if not sig:
            return {"success": False, "error": "send_failed", "venue": "jupiter"}

        confirmed = await self._confirm(sig)
        result = {
            "success":         confirmed,
            "signature":       sig,
            "type":            "buy",
            "mint":            token_mint,
            "sol_spent":       sol_amount,
            "tokens_expected": expected_out,
            "price_impact":    price_impact,
            "venue":           "jupiter",
            "timestamp":       time.time(),
        }
        if confirmed:
            logger.success(f"[BUY OK/Jup] {token_mint[:8]} | {sol_amount} SOL | impact {price_impact:.1f}%")
        else:
            result["error"] = "unconfirmed"
        return result

    async def _jupiter_sell(self, token_mint: str, token_amount_raw: int, reason: str) -> dict:
        quote = await self.get_quote(token_mint, SOL_MINT, token_amount_raw)
        if not quote or int(quote.get("outAmount", 0)) == 0:
            return {"success": False, "error": "no_route", "reason": reason}

        expected_sol = int(quote.get("outAmount", 0)) / 1e9
        tx_b64 = await self._get_swap_transaction(quote)
        if not tx_b64:
            return {"success": False, "error": "build_failed", "reason": reason}

        sig = await self._sign_and_send(tx_b64)
        if not sig:
            return {"success": False, "error": "send_failed", "reason": reason}

        confirmed = await self._confirm(sig)
        result = {
            "success":       confirmed,
            "signature":     sig,
            "type":          "sell",
            "mint":          token_mint,
            "reason":        reason,
            "sol_received":  expected_sol,
            "venue":         "jupiter",
            "timestamp":     time.time(),
        }
        if confirmed:
            logger.success(f"[SELL OK/Jup] {token_mint[:8]} | {reason} | ~{expected_sol:.4f} SOL")
        else:
            result["error"] = "unconfirmed"
        return result

    # ── High-level smart API ──────────────────────────────────────────────────
    async def has_route(self, token_mint: str) -> bool:
        """PumpPortal can trade bonding-curve tokens, so we assume yes."""
        return True

    async def buy(self, token_mint: str, sol_amount: float, token: dict = None) -> dict:
        """Smart buy: try Jupiter first, fall back to PumpPortal."""
        logger.info(f"[BUY] {token_mint[:8]}... | {sol_amount} SOL")

        lamports = int(sol_amount * 1e9)
        jup_quote = await self.get_quote(SOL_MINT, token_mint, lamports)

        if jup_quote and int(jup_quote.get("outAmount", 0)) > 0:
            price_impact = float(jup_quote.get("priceImpactPct", 0))
            if price_impact <= MAX_PRICE_IMPACT_PCT:
                return await self._jupiter_buy(token_mint, sol_amount, jup_quote)
            logger.info(f"  -> Jupiter impact {price_impact:.1f}% too high, using PumpPortal")

        return await self.pumpportal.buy(token_mint, sol_amount)

    async def sell(self, token_mint: str, token_amount_raw, reason: str = "exit") -> dict:
        """Smart sell: try Jupiter first, fall back to PumpPortal."""
        logger.info(f"[SELL] {token_mint[:8]}... | reason={reason}")

        # Only try Jupiter if we have a raw count (not a % string)
        if isinstance(token_amount_raw, int) and token_amount_raw > 0:
            jup_result = await self._jupiter_sell(token_mint, token_amount_raw, reason)
            if jup_result["success"]:
                return jup_result
            # If Jupiter fails with no_route, fall back
            if jup_result.get("error") in ("no_route", "build_failed"):
                logger.info(f"  -> Jupiter failed ({jup_result.get('error')}), using PumpPortal")
                return await self.pumpportal.sell(token_mint, token_amount_raw, reason)
            return jup_result

        # Percentage string or unknown amount → PumpPortal directly
        return await self.pumpportal.sell(token_mint, token_amount_raw, reason)
