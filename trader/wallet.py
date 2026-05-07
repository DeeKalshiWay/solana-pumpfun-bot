"""
trader/wallet.py
Solana wallet setup, balance checking, and keypair management.
Uses solders for keypair handling and aiohttp for RPC calls.
"""

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
        """Returns SOL balance of the wallet."""
        result = await self._rpc("getBalance", [str(self.pubkey)])
        lamports = result.get("result", {}).get("value", 0)
        return lamports / 1e9  # lamports to SOL

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
