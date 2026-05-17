"""
trader/wallet.py
Solana wallet setup, balance checking, and keypair management.
Uses solders for keypair handling and aiohttp for RPC calls.
"""

import time

import aiohttp
import base58
from loguru import logger
from solders.keypair import Keypair

from config import PRIVATE_KEY, RPC_URL


class SolanaWallet:
    """Manages keypair and SOL/token balance queries."""

    def __init__(self):
        self.keypair = self._load_keypair()
        self.pubkey  = self.keypair.pubkey()
        self.session: aiohttp.ClientSession = None
        # Cache the last successful balance read so transient RPC failures
        # (timeouts, 401s, rate-limits) don't return 0 — which would trigger
        # a false −100% drawdown in the risk manager and emergency-stop the
        # bot. None until the FIRST successful read at boot.
        self._last_known_balance: float | None = None
        self._last_balance_read_ts: float = 0.0
        logger.info(f"Wallet loaded: {str(self.pubkey)[:20]}...")

    def _load_keypair(self) -> Keypair:
        """Load keypair from base58 private key string."""
        try:
            secret = base58.b58decode(PRIVATE_KEY)
            return Keypair.from_bytes(secret)
        except Exception as e:
            raise ValueError(
                f"Invalid SOLANA_PRIVATE_KEY. Expected base58-encoded 64-byte key. Error: {e}"
            )

    async def start(self):
        self.session = aiohttp.ClientSession()

    async def stop(self):
        if self.session:
            await self.session.close()

    async def _rpc(self, method: str, params: list) -> dict:
        """Generic JSON-RPC call to Solana node."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        async with self.session.post(
            RPC_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            return await resp.json()

    async def get_sol_balance(self) -> float:
        """Returns SOL balance of the wallet.

        Resilient to transient RPC failures: caches the last successful
        read and returns it if the current call fails or returns no
        result. Without this, a single timeout / 401 / rate-limit makes
        the call return 0, which makes risk_manager compute a fake
        −100% drawdown vs the starting baseline → emergency stop.

        Falls back to 0 only if we have NO prior successful read AND
        the current call fails (so a totally broken RPC at boot still
        results in graceful degradation rather than a crash).
        """
        try:
            result = await self._rpc("getBalance", [str(self.pubkey)])
            res_obj = result.get("result")
            if isinstance(res_obj, dict) and "value" in res_obj:
                sol = res_obj["value"] / 1e9
                self._last_known_balance = sol
                self._last_balance_read_ts = time.time()
                return sol
            # RPC returned an error response or unexpected shape.
            err_msg = (result.get("error") or {}).get("message", "no result field")
            logger.warning(
                f"[WALLET] getBalance RPC error: {err_msg} — "
                f"returning cached {self._last_known_balance}"
            )
        except (TimeoutError, aiohttp.ClientError) as e:
            logger.warning(
                f"[WALLET] getBalance network error: {type(e).__name__}: {e} — "
                f"returning cached {self._last_known_balance}"
            )
        except Exception as e:
            logger.warning(
                f"[WALLET] getBalance unexpected error: {type(e).__name__}: {e} — "
                f"returning cached {self._last_known_balance}"
            )

        if self._last_known_balance is not None:
            return self._last_known_balance
        # First-ever call failed; nothing to cache. 0 is the only sensible
        # default here, but it should be rare (only at boot with a broken
        # RPC). Caller should handle a 0 read at boot specially.
        logger.error(
            "[WALLET] getBalance failed on first call (no cache available) — "
            "returning 0; check RPC_URL"
        )
        return 0.0

    async def get_token_balance(self, mint: str) -> float:
        """Returns token balance for a given SPL mint."""
        result = await self._rpc("getTokenAccountsByOwner", [
            str(self.pubkey),
            {"mint": mint},
            {"encoding": "jsonParsed"}
        ])
        accounts = result.get("result", {}).get("value", [])
        if not accounts:
            return 0.0
        info = accounts[0]["account"]["data"]["parsed"]["info"]
        return float(info["tokenAmount"]["uiAmount"] or 0)

    async def get_all_token_balances(self) -> dict:
        """Returns all SPL token balances as {mint: amount}."""
        result = await self._rpc("getTokenAccountsByOwner", [
            str(self.pubkey),
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"}
        ])
        balances = {}
        for acct in result.get("result", {}).get("value", []):
            info = acct["account"]["data"]["parsed"]["info"]
            mint = info["mint"]
            amount = float(info["tokenAmount"]["uiAmount"] or 0)
            if amount > 0:
                balances[mint] = amount
        return balances

    async def get_portfolio_value_sol(self, positions: dict) -> float:
        """
        Estimate total portfolio value in SOL.
        positions = {mint: {"entry_price_sol": x, "amount": y}}
        """
        sol_bal = await self.get_sol_balance()
        # Positions value is tracked by the risk manager using current prices
        return sol_bal
