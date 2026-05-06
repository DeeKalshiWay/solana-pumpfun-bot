"""
detector/dex_monitor.py
Pulls volume, liquidity, and price action data from DexScreener and Birdeye.
Used to enrich token data after initial detection from pump.fun.
"""

import asyncio
import aiohttp
from loguru import logger
from config import BIRDEYE_API_KEY, DEX_POLL_INTERVAL


DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"
BIRDEYE_API     = "https://public-api.birdeye.so"


class DexMonitor:
    """
    Fetches on-chain market data for a given token mint.
    Called by the signal scorer to enrich pump.fun token data.
    """

    def __init__(self):
        self.session = None

    async def start(self):
        self.session = aiohttp.ClientSession()

    async def stop(self):
        if self.session:
            await self.session.close()

    # ── DexScreener ───────────────────────────────────────────────────────────
    async def get_dexscreener_data(self, mint: str) -> dict:
        """
        Fetch pair data for a token from DexScreener.
        Returns liquidity, volume, price change, and trade counts.
        """
        url = f"{DEXSCREENER_API}/tokens/{mint}"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                pairs = data.get("pairs", [])

                if not pairs:
                    return {}

                # Get the highest liquidity Solana pair
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                if not sol_pairs:
                    return {}

                best = max(sol_pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0))

                return {
                    "dex_pair_address":   best.get("pairAddress", ""),
                    "dex_name":           best.get("dexId", ""),
                    "price_usd":          float(best.get("priceUsd", 0)),
                    "price_native":       float(best.get("priceNative", 0)),
                    "liquidity_usd":      best.get("liquidity", {}).get("usd", 0),
                    "volume_5m_usd":      best.get("volume", {}).get("m5", 0),
                    "volume_1h_usd":      best.get("volume", {}).get("h1", 0),
                    "volume_6h_usd":      best.get("volume", {}).get("h6", 0),
                    "price_change_5m":    best.get("priceChange", {}).get("m5", 0),
                    "price_change_1h":    best.get("priceChange", {}).get("h1", 0),
                    "buys_5m":            best.get("txns", {}).get("m5", {}).get("buys", 0),
                    "sells_5m":           best.get("txns", {}).get("m5", {}).get("sells", 0),
                    "market_cap_dex":     best.get("marketCap", 0),
                    "fdv":                best.get("fdv", 0),
                }

        except Exception as e:
            logger.debug(f"DexScreener error for {mint[:8]}: {e}")
            return {}

    # ── Birdeye ───────────────────────────────────────────────────────────────
    async def get_birdeye_data(self, mint: str) -> dict:
        """
        Fetch token security and holder data from Birdeye.
        Helps filter rugs by checking top holder concentration.
        """
        if not BIRDEYE_API_KEY:
            return {}

        headers = {"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}

        try:
            # Token overview
            url = f"{BIRDEYE_API}/defi/token_overview"
            async with self.session.get(
                url,
                params={"address": mint},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    return {}
                data = (await resp.json()).get("data", {})

                return {
                    "holder_count":      data.get("holder", 0),
                    "unique_wallets_24h": data.get("uniqueWallet24h", 0),
                    "trade_24h":         data.get("trade24h", 0),
                    "buy_24h":           data.get("buy24h", 0),
                    "sell_24h":          data.get("sell24h", 0),
                }

        except Exception as e:
            logger.debug(f"Birdeye error for {mint[:8]}: {e}")
            return {}

    async def get_birdeye_security(self, mint: str) -> dict:
        """
        Check top holder concentration — high concentration = rug risk.
        """
        if not BIRDEYE_API_KEY:
            return {}

        headers = {"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}

        try:
            url = f"{BIRDEYE_API}/defi/token_security"
            async with self.session.get(
                url,
                params={"address": mint},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    return {}
                data = (await resp.json()).get("data", {})

                top10_pct = data.get("top10HolderPercent", 100)
                creator_pct = data.get("creatorPercentage", 0)

                return {
                    "top10_holder_pct": top10_pct,
                    "creator_pct":      creator_pct,
                    # Flag tokens where top 10 hold > 80% or creator holds > 20%
                    "rug_risk_flag":    (top10_pct > 80 or creator_pct > 20),
                }

        except Exception as e:
            logger.debug(f"Birdeye security error for {mint[:8]}: {e}")
            return {}

    # ── Combined enrichment ───────────────────────────────────────────────────
    async def enrich_token(self, token: dict) -> dict:
        """
        Fetch all DEX data for a token and merge into the token dict.
        Called by signal_scorer before scoring.
        """
        mint = token.get("mint", "")
        if not mint:
            return token

        # Run all fetches concurrently
        dex_data, birdeye_data, security_data = await asyncio.gather(
            self.get_dexscreener_data(mint),
            self.get_birdeye_data(mint),
            self.get_birdeye_security(mint),
        )

        token.update(dex_data)
        token.update(birdeye_data)
        token.update(security_data)

        return token


# ── Standalone trending scanner ───────────────────────────────────────────────
class TrendingScanner:
    """
    Independently watches DexScreener trending/boosted tokens on Solana.
    Feeds tokens into the queue that may not have been caught by pump.fun monitor.
    """

    def __init__(self, token_queue: asyncio.Queue):
        self.queue = token_queue
        self.seen = set()
        self.running = False

    async def run(self):
        self.running = True
        async with aiohttp.ClientSession() as session:
            while self.running:
                await self._scan_trending(session)
                await self._scan_boosted(session)
                await asyncio.sleep(DEX_POLL_INTERVAL)

    async def _scan_trending(self, session):
        """Fetch DexScreener trending tokens on Solana."""
        try:
            url = f"{DEXSCREENER_API}/search?q=solana"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                pairs = data.get("pairs", [])

                for pair in pairs:
                    if pair.get("chainId") != "solana":
                        continue

                    base = pair.get("baseToken", {})
                    mint = base.get("address", "")
                    if not mint or mint in self.seen:
                        continue

                    # Only grab very new pairs
                    created_at = pair.get("pairCreatedAt", 0) / 1000
                    import time
                    age_minutes = (time.time() - created_at) / 60
                    if age_minutes > 30:
                        continue

                    self.seen.add(mint)

                    token = {
                        "mint":           mint,
                        "name":           base.get("name", "Unknown"),
                        "symbol":         base.get("symbol", "???"),
                        "age_minutes":    round(age_minutes, 2),
                        "source":         "dexscreener_trending",
                        "liquidity_usd":  pair.get("liquidity", {}).get("usd", 0),
                        "volume_5m_usd":  pair.get("volume", {}).get("m5", 0),
                        "price_change_5m": pair.get("priceChange", {}).get("m5", 0),
                    }

                    logger.info(f"[DEXSCREENER] Trending: {token['symbol']} | Age: {age_minutes:.1f}min")
                    await self.queue.put(token)

        except Exception as e:
            logger.error(f"DexScreener trending scan error: {e}")

    async def _scan_boosted(self, session):
        """Fetch DexScreener boosted/advertised tokens — often signals organized promotion."""
        try:
            url = "https://api.dexscreener.com/token-boosts/latest/v1"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return
                boosts = await resp.json()

                for item in boosts:
                    if item.get("chainId") != "solana":
                        continue

                    mint = item.get("tokenAddress", "")
                    if not mint or mint in self.seen:
                        continue

                    self.seen.add(mint)

                    token = {
                        "mint":       mint,
                        "name":       item.get("description", "Unknown"),
                        "symbol":     "???",
                        "source":     "dexscreener_boosted",
                        "boost_amount": item.get("amount", 0),
                    }

                    logger.info(f"[DEXSCREENER] Boosted token: {mint[:8]}...")
                    await self.queue.put(token)

        except Exception as e:
            logger.debug(f"DexScreener boost scan error: {e}")

    def stop(self):
        self.running = False
