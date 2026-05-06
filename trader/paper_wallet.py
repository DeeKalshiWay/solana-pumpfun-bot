"""
trader/paper_wallet.py
Virtual wallet for paper trading. Tracks a simulated SOL balance.
Persists to disk so the equity curve survives bot restarts.
"""

import json
import os
from loguru import logger

PAPER_STATE_FILE = "logs/paper_wallet.json"


class PaperWallet:
    def __init__(self, starting_balance: float):
        self._starting_balance = starting_balance
        self._balance = self._load_or_init(starting_balance)
        self.pubkey = "PAPER_WALLET_SIMULATED"

    def _load_or_init(self, starting_balance: float) -> float:
        """Restore balance from disk if present, else seed with the configured start."""
        os.makedirs("logs", exist_ok=True)
        if os.path.exists(PAPER_STATE_FILE):
            try:
                with open(PAPER_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                bal = float(data.get("balance_sol", starting_balance))
                logger.info(f"[PAPER] Restored balance from disk: {bal:.6f} SOL")
                return bal
            except Exception as e:
                logger.warning(f"[PAPER] Could not load state ({e}); seeding fresh")
        logger.info(f"[PAPER] Initialised fresh balance: {starting_balance} SOL")
        return starting_balance

    def _save(self):
        try:
            with open(PAPER_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "balance_sol":      self._balance,
                    "starting_balance": self._starting_balance,
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"[PAPER] Save error: {e}")

    async def start(self):
        pass

    async def stop(self):
        self._save()

    async def get_sol_balance(self) -> float:
        return self._balance

    async def get_token_balance(self, mint: str) -> float:
        return 0.0

    async def get_all_token_balances(self) -> dict:
        return {}

    def deduct(self, amount: float):
        self._balance = max(0.0, self._balance - amount)
        self._save()

    def credit(self, amount: float):
        self._balance += amount
        self._save()

    def reset(self, starting_balance: float = None):
        """Wipe and re-seed. Call manually if you want a fresh paper run."""
        self._balance = starting_balance if starting_balance is not None else self._starting_balance
        self._save()
        logger.warning(f"[PAPER] Balance reset to {self._balance} SOL")
