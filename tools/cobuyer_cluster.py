"""
tools/cobuyer_cluster.py  (Discovery 4)

Find the profitable PEERS of wallets we already trust. For each token the
copyable proven wallets bought (from cached history), pull the token's other
buyers from chain and count co-occurrence. Wallets that repeatedly buy the same
tokens as our proven wallets are candidate peers.

Output: logs/_candidates_cobuyers4.json (wallets co-buying >= MIN_SHARED of the
proven wallets' tokens) for the PnL scan.

Run: python -m tools.cobuyer_cluster --min-shared 3
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import time
import urllib.request

from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_CACHE = os.path.join(ROOT, "logs", "_raw_txns")
COPYABLE = os.path.join(ROOT, "logs", "copyable_wallets.json")
OUT = os.path.join(ROOT, "logs", "_candidates_cobuyers4.json")
WSOL = "So11111111111111111111111111111111111111112"


def _key():
    return dotenv_values(os.path.join(ROOT, ".env")).get("HELIUS_API_KEY", "")


def proven_mints():
    """Mints the copyable proven wallets bought, from cache."""
    wallets = json.load(open(COPYABLE))
    mints = set()
    for w in wallets:
        cp = os.path.join(RAW_CACHE, w + ".json")
        if not os.path.exists(cp):
            continue
        for t in json.load(open(cp)):
            if t.get("source") not in ("PUMP_FUN", "PUMP_AMM", "PUMPSWAP") or t.get("type") != "SWAP":
                continue
            for tt in t.get("tokenTransfers", []) or []:
                if tt.get("mint") and tt.get("mint") != WSOL and tt.get("toUserAccount") == w:
                    mints.add(tt["mint"])
    return wallets, list(mints)


def mint_buyers(mint, key, pages=3, sleep=0.06):
    before, buyers = None, set()
    for _ in range(pages):
        url = f"https://api.helius.xyz/v0/addresses/{mint}/transactions?api-key={key}&limit=100&type=SWAP"
        if before:
            url += f"&before={before}"
        try:
            page = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "web"}), timeout=40))
        except Exception:
            break
        if not page:
            break
        for tx in page:
            for tt in tx.get("tokenTransfers", []) or []:
                if tt.get("mint") == mint and tt.get("toUserAccount"):
                    buyers.add(tt["toUserAccount"])
        before = page[-1].get("signature")
        time.sleep(sleep)
        if len(page) < 100:
            break
    return buyers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-shared", type=int, default=3)
    ap.add_argument("--max-mints", type=int, default=80)
    args = ap.parse_args()
    key = _key()
    proven, mints = proven_mints()
    mints = mints[: args.max_mints]
    proven_set = set(proven)
    print(f"Co-buyer scan: {len(mints)} tokens bought by {len(proven)} proven wallets")
    counts = collections.Counter()
    for i, m in enumerate(mints, 1):
        for b in mint_buyers(m, key):
            if b not in proven_set:
                counts[b] += 1
        if i % 20 == 0:
            print(f"  [{i}/{len(mints)}] unique co-buyers: {len(counts)}", flush=True)
    cands = [w for w, c in counts.items() if c >= args.min_shared]
    cands.sort(key=lambda w: counts[w], reverse=True)
    json.dump(cands, open(OUT, "w"), indent=1)
    print(f"\nCo-buyers sharing >= {args.min_shared} of the proven wallets' tokens: {len(cands)} -> {OUT}")
    print("top:", [(w[:8], counts[w]) for w in cands[:15]])


if __name__ == "__main__":
    main()
