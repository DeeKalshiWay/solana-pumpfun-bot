"""
tools/rpc_pool.py

Free-tier Solana RPC rotator with automatic cooldown on rate-limit responses.

Why this exists: pump.bot's primary RPC (Helius paid) ran out of credit. To
keep the polling follower trading, we round-robin requests across a pool of
free public Solana RPC endpoints listed in .env as FREE_RPC_URLS. Each endpoint
has its own cooldown timer so a 429/timeout from one doesn't poison the rest.

Endpoints return standard Solana JSON-RPC (not Helius Enhanced); pair this
with tools/raw_tx_parser.py to recover Enhanced-shape transaction dicts.

API:
    pool = RpcPool.from_env()
    res = pool.call("getSignaturesForAddress", [wallet, {"limit": 25}])
    tx  = pool.call("getTransaction", [sig, {"encoding":"jsonParsed",
                                              "maxSupportedTransactionVersion": 0}])

Returns the JSON-RPC `result` field on success, or None if every endpoint
failed within the retry budget. Each call randomizes the starting endpoint to
spread load.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request

from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# How long an endpoint sits in cooldown after a 429 / timeout / 5xx.
COOLDOWN_S = 30.0
# Per-request timeout. Free RPCs are slow; don't get too aggressive.
REQUEST_TIMEOUT_S = 15.0
# Max endpoints to try per call before giving up.
MAX_ATTEMPTS = 4
# Tiny pause between attempts to avoid hammering when most endpoints are cold.
RETRY_BACKOFF_S = 0.25


class RpcPool:
    def __init__(self, urls: list[str]):
        self.urls = [u.strip() for u in urls if u.strip()]
        if not self.urls:
            raise ValueError("RpcPool: no URLs provided (set FREE_RPC_URLS in .env)")
        # endpoint -> earliest unix-ts at which it can be used again
        self._cooldown_until: dict[str, float] = {u: 0.0 for u in self.urls}
        # endpoint -> (n_calls, n_failures) for diagnostics
        self._stats: dict[str, tuple[int, int]] = {u: (0, 0) for u in self.urls}
        # round-robin cursor
        self._cursor = random.randrange(len(self.urls))

    @classmethod
    def from_env(cls) -> "RpcPool":
        env = dotenv_values(os.path.join(ROOT, ".env"))
        urls_raw = env.get("FREE_RPC_URLS", "")
        urls = [u.strip() for u in urls_raw.split(",") if u.strip()]
        return cls(urls)

    def _available(self) -> list[str]:
        now = time.time()
        return [u for u in self.urls if self._cooldown_until[u] <= now]

    def _next_url(self) -> str | None:
        avail = self._available()
        if not avail:
            # All cold — pick the one that recovers soonest, wait briefly, return it
            soonest = min(self.urls, key=lambda u: self._cooldown_until[u])
            wait = max(0.0, self._cooldown_until[soonest] - time.time())
            if wait > 0:
                time.sleep(min(wait, 5.0))
            return soonest
        # Round-robin among available
        self._cursor = (self._cursor + 1) % len(avail)
        return avail[self._cursor]

    def _mark_fail(self, url: str, reason: str):
        n, f = self._stats[url]
        self._stats[url] = (n + 1, f + 1)
        self._cooldown_until[url] = time.time() + COOLDOWN_S

    def _mark_ok(self, url: str):
        n, f = self._stats[url]
        self._stats[url] = (n + 1, f)

    def call(self, method: str, params: list) -> dict | list | None:
        """Issue a Solana JSON-RPC call. Returns the `result` field or None.

        On rate-limit / timeout / 5xx, the failing endpoint is parked for
        COOLDOWN_S seconds and the call retries on the next endpoint.
        """
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params
        }).encode("utf-8")
        attempts = 0
        last_error = None
        while attempts < MAX_ATTEMPTS:
            attempts += 1
            url = self._next_url()
            if not url:
                return None
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": "pump-bot-rpc-pool"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                    body = resp.read()
                data = json.loads(body)
                if "error" in data:
                    err = data["error"]
                    msg = (err.get("message") or "").lower()
                    # Rate-limit-ish errors -> cool down
                    if "rate" in msg or "limit" in msg or "throttle" in msg or "exceed" in msg:
                        self._mark_fail(url, f"rpc-error:{msg[:60]}")
                        last_error = f"{url}: {msg[:60]}"
                        time.sleep(RETRY_BACKOFF_S)
                        continue
                    # Other JSON-RPC errors (method not found etc) — try next
                    self._mark_fail(url, f"rpc-error:{msg[:60]}")
                    last_error = f"{url}: {msg[:60]}"
                    time.sleep(RETRY_BACKOFF_S)
                    continue
                self._mark_ok(url)
                return data.get("result")
            except urllib.error.HTTPError as e:
                # 429 = too many requests; 5xx = endpoint-side problem
                self._mark_fail(url, f"http-{e.code}")
                last_error = f"{url}: HTTP {e.code}"
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                self._mark_fail(url, f"{type(e).__name__}")
                last_error = f"{url}: {type(e).__name__}"
            except Exception as e:
                self._mark_fail(url, f"{type(e).__name__}")
                last_error = f"{url}: {type(e).__name__}: {str(e)[:80]}"
            time.sleep(RETRY_BACKOFF_S)
        # All attempts failed
        return None

    def stats(self) -> dict[str, dict]:
        now = time.time()
        out = {}
        for u, (n, f) in self._stats.items():
            cd = max(0.0, self._cooldown_until[u] - now)
            out[u] = {"calls": n, "fails": f, "cooldown_s": round(cd, 1)}
        return out


# Module-level singleton so consumers can `from rpc_pool import POOL` without
# instantiating their own. Lazy-init to avoid env read at import time.
_POOL: RpcPool | None = None


def pool() -> RpcPool:
    global _POOL
    if _POOL is None:
        _POOL = RpcPool.from_env()
    return _POOL


def get_signatures(addr: str, limit: int = 25, before: str | None = None) -> list[dict]:
    """List recent signatures for a wallet/address."""
    cfg = {"limit": limit}
    if before:
        cfg["before"] = before
    res = pool().call("getSignaturesForAddress", [addr, cfg])
    return res if isinstance(res, list) else []


def get_transaction(sig: str) -> dict | None:
    """Fetch a transaction by signature. Returns raw RPC result dict or None."""
    res = pool().call("getTransaction", [sig, {
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0,
    }])
    return res if isinstance(res, dict) else None


if __name__ == "__main__":
    # Smoke test: list endpoints + try a benign call
    p = RpcPool.from_env()
    print(f"endpoints ({len(p.urls)}):")
    for u in p.urls:
        print(f"  {u}")
    print("\nsmoke test: getSlot")
    r = p.call("getSlot", [])
    print(f"result: {r}")
    print("\nstats:")
    for u, s in p.stats().items():
        print(f"  {u:<55} {s}")
