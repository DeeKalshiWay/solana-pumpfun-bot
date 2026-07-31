"""
tools/winner_early_buyers.py  (Discovery 2)

Reverse smart-money search: start from OUTCOMES, not wallets. Take pump.fun
tokens that actually pumped, pull each token's EARLIEST buyers from chain, and
surface wallets that appear early across MANY winners. Recurring early-in-winner
wallets are the canonical "smart money" signal — and unlike our accidental
bot_wallets sample, this finds wallets we've never observed.

Winning mints come from the archived counterfactual feed (mc_delta_pct >=
WIN_PCT). For each, we paginate the mint's history to the OLDEST txns (the first
buyers) and collect their wallets. Output: logs/_candidates_winner_buyers2.json
(wallets early in >= MIN_WINNERS distinct winners) for the PnL scan.

Run: python -m tools.winner_early_buyers --mints 50 --min-winners 2
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
CF = os.path.join(ROOT, "logs", "_archive", "OLD_c16", "counterfactual.jsonl")
OUT = os.path.join(ROOT, "logs", "_candidates_winner_buyers2.json")
WSOL = "So11111111111111111111111111111111111111112"
WIN_PCT = 100.0
EARLY_BUYERS_PER_MINT = 40   # how many of the earliest buyers to keep per winner


def _key():
    return dotenv_values(os.path.join(ROOT, ".env")).get("HELIUS_API_KEY", "")


def winning_mints(limit):
    best = {}
    for line in open(CF, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        m, p = r.get("mint"), r.get("mc_delta_pct")
        if m and p is not None:
            best[m] = max(best.get(m, -1e9), float(p))
    winners = sorted(((m, p) for m, p in best.items() if p >= WIN_PCT), key=lambda kv: kv[1], reverse=True)
    return [m for m, _ in winners[:limit]]


def earliest_buyers(mint, key, max_pages=20, sleep=0.06):
    """Paginate to the oldest txns; return the earliest distinct buyer wallets."""
    before, pages = None, []
    for _ in range(max_pages):
        url = f"https://api.helius.xyz/v0/addresses/{mint}/transactions?api-key={key}&limit=100&type=SWAP"
        if before:
            url += f"&before={before}"
        try:
            page = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "web"}), timeout=40))
        except Exception:
            break
        if not page:
            break
        pages.append(page)
        before = page[-1].get("signature")
        time.sleep(sleep)
        if len(page) < 100:
            break
    # oldest first = last page reversed, walking backward
    flat = [tx for page in pages for tx in page]
    flat.sort(key=lambda t: t.get("timestamp", 0))   # ascending
    buyers = []
    for tx in flat:
        for tt in tx.get("tokenTransfers", []) or []:
            if tt.get("mint") == mint and tt.get("toUserAccount"):
                b = tt["toUserAccount"]
                if b not in buyers:
                    buyers.append(b)
        if len(buyers) >= EARLY_BUYERS_PER_MINT:
            break
    return buyers[:EARLY_BUYERS_PER_MINT]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mints", type=int, default=50)
    ap.add_argument("--min-winners", type=int, default=2)
    args = ap.parse_args()
    key = _key()
    mints = winning_mints(args.mints)
    print(f"Winners (>=+{WIN_PCT:.0f}%): scanning earliest buyers of {len(mints)} mints")
    counts = collections.Counter()
    for i, m in enumerate(mints, 1):
        for b in earliest_buyers(m, key):
            counts[b] += 1
        if i % 10 == 0:
            print(f"  [{i}/{len(mints)}] unique early-buyers so far: {len(counts)}", flush=True)
    cands = [w for w, c in counts.items() if c >= args.min_winners]
    cands.sort(key=lambda w: counts[w], reverse=True)
    json.dump(cands, open(OUT, "w"), indent=1)
    print(f"\nWallets early in >= {args.min_winners} winners: {len(cands)} -> {OUT}")
    print("top:", [(w[:8], counts[w]) for w in cands[:15]])


if __name__ == "__main__":
    main()
