"""probe_comments.py — round 2: find pump.fun v3 replies endpoint."""
import asyncio
import json
import sys
import websockets
import aiohttp


PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
V3 = "https://frontend-api-v3.pump.fun"

CANDIDATE_URLS = [
    f"{V3}/coins/{{mint}}",
    f"{V3}/replies/{{mint}}",
    f"{V3}/replies/{{mint}}/limit/20/offset/0",
    f"{V3}/coins/{{mint}}/replies",
    f"{V3}/coins/{{mint}}/comments",
    f"{V3}/comments/{{mint}}",
    f"{V3}/thread/{{mint}}",
    f"{V3}/threads/{{mint}}",
    f"{V3}/v2/replies/{{mint}}",
    f"{V3}/v1/replies/{{mint}}",
    # Some pump frontends use /replies with query params
    f"{V3}/replies?mint={{mint}}&limit=20&offset=0",
    f"{V3}/replies?mint={{mint}}",
]

# Try with both browser headers and minimal
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://pump.fun",
    "Referer": "https://pump.fun/",
    "Accept-Language": "en-US,en;q=0.9",
}


async def grab_active_mint():
    """Get a fresh mint AND wait 60s so it has likely accumulated some replies."""
    print(f"[probe] Connecting to {PUMPPORTAL_WS}...")
    async with websockets.connect(PUMPPORTAL_WS, ping_interval=20) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        # Wait for one with non-zero initial buy (likely to get traffic)
        for _ in range(200):
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            data = json.loads(raw)
            if data.get("txType") == "create" and float(data.get("solAmount", 0)) > 0.5:
                mint = data["mint"]
                print(f"[probe] Got mint: {mint} sym={data.get('symbol')} initial={data.get('solAmount')}")
                return mint
    return None


async def probe_urls(mint, label="pass"):
    print(f"\n[probe-{label}] mint {mint}\n")
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        for tmpl in CANDIDATE_URLS:
            url = tmpl.format(mint=mint)
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    body = await resp.text()
                    flag = "OK " if resp.status == 200 else "   "
                    print(f"{flag} [{resp.status}] {url} | len={len(body)}")
                    if resp.status == 200:
                        print(f"      BODY: {body[:600]}")
                    print()
            except Exception as e:
                print(f"   [ERR] {url} | {type(e).__name__}: {e}\n")


async def main():
    mint = await grab_active_mint()
    if not mint:
        sys.exit("no mint")
    # Pass 1 immediately
    await probe_urls(mint, "0s")
    # Wait 60s
    print("[probe] sleeping 60s for activity to build...")
    await asyncio.sleep(60)
    await probe_urls(mint, "60s")


if __name__ == "__main__":
    asyncio.run(main())
