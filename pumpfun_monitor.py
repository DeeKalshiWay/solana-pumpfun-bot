"""
detector/pumpfun_monitor.py
Monitors pump.fun for newly launched tokens in real-time.
Helius-only mode (pump.fun public API is Cloudflare-blocked in 2026).
"""

import asyncio
import aiohttp
import websockets
import json
import time
from loguru import logger
from config import PUMPFUN_POLL_INTERVAL, MAX_TOKEN_AGE_MINUTES, HELIUS_API_KEY


# pump.fun program ID on Solana mainnet
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


class PumpFunMonitor:
    """Detects new token launches on pump.fun via Helius on-chain logs."""

    def __init__(self, token_queue: asyncio.Queue):
        self.queue = token_queue
        self.seen_mints = set()
        self.running = False

    # ── Main entry point ──────────────────────────────────────────────────────
    async def run(self):
        self.running = True
        logger.info("PumpFun monitor starting (Helius mode)...")
        await self.stream_helius_logs()

    def stop(self):
        self.running = False

    # ── Helius WebSocket (on-chain detection) ─────────────────────────────────
    async def stream_helius_logs(self):
        if not HELIUS_API_KEY:
            logger.error("No HELIUS_API_KEY set — cannot detect tokens")
            return

        ws_url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        subscribe_msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [PUMP_PROGRAM_ID]},
                {"commitment": "processed"}
            ]
        }

        while self.running:
            try:
                async with websockets.connect(ws_url) as ws:
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info("Helius on-chain log stream active")

                    async for raw in ws:
                        if not self.running:
                            break
                        data = json.loads(raw)
                        if "params" in data:
                            logs = data["params"]["result"]["value"].get("logs", [])
                            signature = data["params"]["result"]["value"].get("signature", "")
                            await self._parse_onchain_logs(logs, signature)

            except Exception as e:
                logger.error(f"Helius stream error: {e}")
                await asyncio.sleep(5)

    async def _parse_onchain_logs(self, logs, signature):
        """Look for Create instruction, then fetch the tx to extract the mint."""
        for log in logs:
            if "Program log: Instruction: Create" in log:
                logger.info(f"[ONCHAIN] Tx: {signature[:20]}...")
                await self._fetch_tx_and_emit(signature)
                break

    async def _fetch_tx_and_emit(self, signature):
        """Fetch tx via Helius RPC and extract the new mint address."""
        url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
            ]
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload,
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
                    tx = data.get("result", {}) or {}
                    meta = tx.get("meta", {}) or {}
                    for b in meta.get("postTokenBalances", []):
                        mint = b.get("mint")
                        if mint and mint not in self.seen_mints:
                            self.seen_mints.add(mint)
                            token = {
                                "mint":           mint,
                                "name":           "NewToken",
                                "symbol":         "NEW",
                                "description":    "",
                                "image_uri":      "",
                                "twitter":        "",
                                "telegram":       "",
                                "website":        "",
                                "creator":        "",
                                "created_ts":     time.time(),
                                "age_minutes":    0,
                                "market_cap_usd": 0,
                                "reply_count":    0,
                                "source":         "helius_onchain",
                            }
                            logger.info(f"[HELIUS] Mint: {mint[:8]}...")
                            await self.queue.put(token)
                            return
        except Exception as e:
            logger.debug(f"tx fetch error: {e}")
