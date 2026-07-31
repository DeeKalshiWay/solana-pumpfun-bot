"""
tools/birdeye_top_traders.py  (Discovery 3)

Pull candidate wallets from Birdeye's per-token "top traders" (by volume/PnL)
across trending + known-winning tokens. An external candidate source
independent of our accidental bot_wallets sample.

Output: logs/_candidates_birdeye3.json (wallets appearing as top traders on
>= MIN_TOKENS distinct tokens) for the PnL scan.

Birdeye's standard tier is rate-limited, so calls are throttled with 429 retry.

Run: python -m tools.birdeye_top_traders --tokens 40
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import time
import urllib.request
import urllib.error

from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CF = os.path.join(ROOT, "logs", "_archive", "OLD_c16", "counterfactual.jsonl")
OUT = os.path.join(ROOT, "logs", "_candidates_birdeye3.json")
SLEEP = 1.3


def _key():
    return dotenv_values(os.path.join(ROOT, ".env")).get("BIRDEYE_API_KEY", "")


def _get(url, key, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"X-API-KEY": key, "x-chain": "solana", "User-Agent": "bd"})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0 * (i + 1)); continue
            return None
        except Exception:
            return None
    return None


def trending(key, limit=50):
    d = _get(f"https://public-api.birdeye.so/defi/token_trending?sort_by=rank&sort_type=asc&offset=0&limit={limit}", key)
    return [t.get("address") for t in ((d or {}).get("data", {}).get("tokens") or []) if t.get("address")]


def winning_mints(limit=40):
    best = {}
    if not os.path.exists(CF):
        return []
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
    return [m for m, _ in sorted(((m, p) for m, p in best.items() if p >= 100), key=lambda kv: kv[1], reverse=True)[:limit]]


def top_traders(token, key, limit=10):
    d = _get(f"https://public-api.birdeye.so/defi/v2/tokens/top_traders?address={token}&time_frame=24h&sort_type=desc&sort_by=volume&offset=0&limit={limit}", key)
    items = (d or {}).get("data", {}).get("items") or []
    out = []
    for it in items:
        w = it.get("owner") or it.get("address") or it.get("wallet")
        # bias toward profitable traders when PnL is available
        rp = it.get("realizedPnl")
        if w and (rp is None or rp > 0):
            out.append(w)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokens", type=int, default=40)
    ap.add_argument("--min-tokens", type=int, default=2)
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()
    key = _key()
    if not key:
        raise SystemExit("No BIRDEYE_API_KEY in .env")

    toks = trending(key, args.tokens)
    time.sleep(SLEEP)
    if not toks:
        toks = winning_mints(args.tokens)
    if args.probe:
        if not toks:
            print("no tokens (rate-limited?) — retry shortly"); return
        d = _get(f"https://public-api.birdeye.so/defi/v2/tokens/top_traders?address={toks[0]}&time_frame=24h&sort_type=desc&sort_by=volume&offset=0&limit=3", key)
        items = (d or {}).get("data", {}).get("items") or []
        print("probe item fields:", list(items[0].keys()) if items else "none returned")
        return

    toks = list(dict.fromkeys(toks + winning_mints(args.tokens)))[: args.tokens * 2]
    print(f"Birdeye: scanning top traders of {len(toks)} tokens (trending + winners)")
    counts = collections.Counter()
    for i, t in enumerate(toks, 1):
        for w in top_traders(t, key):
            counts[w] += 1
        time.sleep(SLEEP)
        if i % 15 == 0:
            print(f"  [{i}/{len(toks)}] unique top-traders: {len(counts)}", flush=True)
    cands = [w for w, c in counts.items() if c >= args.min_tokens]
    cands.sort(key=lambda w: counts[w], reverse=True)
    json.dump(cands, open(OUT, "w"), indent=1)
    print(f"\nTop-traders on >= {args.min_tokens} tokens: {len(cands)} -> {OUT}")
    print("top:", [(w[:8], counts[w]) for w in cands[:15]])


if __name__ == "__main__":
    main()
