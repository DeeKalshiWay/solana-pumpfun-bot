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

from config import RPC_URL, SLIPPAGE_BPS

PUMPPORTAL_LOCAL_API = "https://pumpportal.fun/api/trade-local"


class PumpPortalExecutor:
    """
    Bonding-curve execution for fresh pump.fun tokens Jupiter can't route.
    """

    def __init__(self, keypair: Keypair):
        self.keypair = keypair
        self.pubkey  = keypair.pubkey()
        self.session = None

    async def start(self):
        self.session = aiohttp.ClientSession()

    async def stop(self):
        if self.session:
            await self.session.close()

    async def _build_tx(self, action: str, mint: str, amount, denominated_in_sol: bool):
        """Request a serialized transaction from PumpPortal."""
        # PumpPortal expects total priority fee in SOL (not per-CU microlamports)
        priority_fee_sol = 0.001
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
        """Sign the PumpPortal-built transaction and submit via our RPC."""
        try:
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
                    logger.warning(f"RPC rejected (PP): {err[:200]}")
                    return None
                return result.get("result")
        except Exception as e:
            logger.warning(f"PP sign/send error: {e}")
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
                                logger.warning(f"PP tx {signature[:20]} failed: {status['err']}")
                                return False
                            return True
            except Exception:
                pass
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
            logger.success(f"[PP BUY CONFIRMED] {token_mint[:8]} | {sol_amount} SOL | sig: {sig[:20]}")
        else:
            result["error"] = "unconfirmed"
            logger.warning(f"[PP BUY UNCONFIRMED] {token_mint[:8]} | sig: {sig[:20]}")
        return result

    # ── Sell ──────────────────────────────────────────────────────────────────
    async def sell(self, token_mint: str, token_amount_or_pct, reason: str = "exit") -> dict:
        """
        token_amount_or_pct: either a raw token count (int) or a percentage string like "100%"
        """
        logger.info(f"[PP SELL] {token_mint[:8]}... | reason={reason}")

        # For sells we can use percentage strings (PumpPortal supports "100%")
        # If we got an int, pass as token count
        amount = token_amount_or_pct
        denominated_in_sol = False  # selling is always token-denominated

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
