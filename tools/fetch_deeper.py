"""
tools/fetch_deeper.py

Pull DEEPER on-chain history for the copyable wallets so the rolling-window
out-of-sample test isn't dominated by one wallet in one window.

For each wallet in copyable_wallets.json, fetches up to MAX_PAGES of swap
history (Helius limit 100/page) and OVERWRITES the per-wallet raw cache. Reads
old cache to skip if it already has >= MAX_PAGES worth of txns.

Run: python -m tools.fetch_deeper --pages 25
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_CACHE = os.path.join(ROOT, "logs", "_raw_txns")
COPYABLE = os.path.join(ROOT, "logs", "copyable_wallets.json")


def _key():
    k = dotenv_values(os.path.join(ROOT, ".env")).get("HELIUS_API_KEY", "")
    if not k:
        sys.exit("No HELIUS_API_KEY")
    return k


def fetch_deep(wallet: str, key: str, max_pages: int, sleep: float):
    out, before = [], None
    for i in range(max_pages):
        url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={key}&limit=100"
        if before:
            url += f"&before={before}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "deep"})
            with urllib.request.urlopen(req, timeout=40) as r:
                page = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0); continue
            break
        except Exception:
            break
        if not page:
            break
        out.extend(page)
        before = page[-1].get("signature")
        time.sleep(sleep)
        if len(page) < 100:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=20)
    ap.add_argument("--sleep", type=float, default=0.08)
    args = ap.parse_args()
    key = _key()
    wallets = json.load(open(COPYABLE))
    os.makedirs(RAW_CACHE, exist_ok=True)
    print(f"Deepening cache for {len(wallets)} copyable wallets, up to {args.pages} pages each")
    for w in wallets:
        cp = os.path.join(RAW_CACHE, w + ".json")
        old_n = 0
        if os.path.exists(cp):
            try:
                old_n = len(json.load(open(cp)))
            except Exception:
                old_n = 0
        t0 = time.time()
        txns = fetch_deep(w, key, args.pages, args.sleep)
        if len(txns) >= old_n:
            json.dump(txns, open(cp, "w"))
        print(f"  {w[:12]:<14}  {old_n:>5} -> {len(txns):>5} txns  ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
