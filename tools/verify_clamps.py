"""
tools/verify_clamps.py

For every trade in copy_follower_trades.jsonl that hit the MAX_GAIN_PCT clamp,
pull the wallet's actual sell tx from Helius, decode the real their_exit
price, and compare to what the follower recorded. Settles the long-running
"real moonshot vs price-parse bug" question.

Approach for each clamped trade:
  1. Read the close event: wallet, mint, our_entry, our_exit, clamped flag
  2. Read the matching open event: our_entry, their_entry
  3. Fetch the wallet's recent pump.fun swap history via Helius Enhanced
  4. Find the SELL of this mint near the close ts
  5. Extract the wallet's actual sell price from tokenTransfers
  6. Compare:
       reported_gross = (our_exit / our_entry - 1) * 100   (what the follower used)
       true_gross     = (true_their_exit*(1-EXIT_LAG) / our_entry - 1) * 100
       (or however the follower computes it — match the math exactly)

Run:  python -m tools.verify_clamps
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass

from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_LOG = os.path.join(ROOT, "logs", "copy_follower_trades.jsonl")

WSOL = "So11111111111111111111111111111111111111112"
QUOTE = {WSOL, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
         "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
EXIT_LAG_PCT = 3.0   # must match copy_follower.py
FEE_PCT = 2.0


def _key():
    return dotenv_values(os.path.join(ROOT, ".env")).get("HELIUS_API_KEY", "")


def _fetch_wallet_history(wallet: str, key: str, limit: int = 100):
    url = (f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
           f"?api-key={key}&limit={limit}")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "verify"}), timeout=30) as r:
            return json.load(r)
    except Exception as e:
        print(f"  fetch error: {e}")
        return []


def _wallet_sell_price(tx: dict, wallet: str, mint: str) -> float | None:
    """For a tx where the wallet SOLD `mint`, return price in SOL per token.

    Strategy: find the token transfer where wallet sent `mint` away, sum the
    SOL the wallet received in the same tx. price = SOL_received / tokens_sent.
    """
    sold = 0.0
    sol_in = 0.0
    for tt in tx.get("tokenTransfers", []) or []:
        amt = tt.get("tokenAmount") or 0
        if tt.get("mint") == mint and tt.get("fromUserAccount") == wallet:
            sold += float(amt)
    # native SOL change
    for ad in tx.get("accountData", []) or []:
        if ad.get("account") == wallet:
            sol_in = max(0.0, (ad.get("nativeBalanceChange") or 0) / 1e9)
            break
    if sold > 0 and sol_in > 0:
        return sol_in / sold
    return None


def main():
    key = _key()
    if not key:
        raise SystemExit("No HELIUS_API_KEY in .env")

    # Pair opens and closes
    opens_chrono: dict = {}
    closes: list = []
    for line in open(TRADES_LOG, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("event") == "open":
            opens_chrono.setdefault((r.get("wallet",""), r.get("mint","")), []).append(r)
        elif r.get("event") == "close":
            closes.append(r)

    clamped = [c for c in closes if c.get("clamped")]
    if not clamped:
        print("no clamped trades in the log — nothing to verify")
        return

    print(f"verifying {len(clamped)} clamped trade(s) against on-chain truth...\n")
    print(f"{'mint':<12} {'wallet':<12} {'our_entry':>14} {'reported their_exit':>22} {'TRUE their_exit':>20} {'reported gross':>16} {'TRUE gross':>14}  verdict")
    print("-" * 140)

    for c in clamped:
        w, m = c.get("wallet",""), c.get("mint","")
        # Find matching open
        opens = opens_chrono.get((w, m), [])
        op = opens.pop(0) if opens else None
        our_entry = (op or c).get("our_entry") or c.get("our_entry")
        reported_our_exit = c.get("our_exit") or 0
        reported_their_exit = reported_our_exit / (1 - EXIT_LAG_PCT/100) if reported_our_exit else 0
        reported_gross = c.get("gross_pct") or 0

        # Pull wallet history and find the matching sell
        history = _fetch_wallet_history(w, key, limit=100)
        close_ts = c.get("ts") or 0
        # Find sells of this mint within ±2min of our close ts
        candidate = None
        for tx in history:
            if abs((tx.get("timestamp") or 0) - close_ts) > 120:
                continue
            for tt in (tx.get("tokenTransfers") or []):
                if tt.get("mint") == m and tt.get("fromUserAccount") == w:
                    candidate = tx
                    break
            if candidate:
                break

        if not candidate:
            print(f"{m[:10]:<12} {w[:10]:<12}  could not find matching wallet sell on-chain (history may not be deep enough)")
            continue

        true_their_exit = _wallet_sell_price(candidate, w, m)
        if not true_their_exit:
            print(f"{m[:10]:<12} {w[:10]:<12}  wallet sell tx found but couldn't decode price")
            continue

        # Match the follower's exit-pricing: our_exit = their_exit × (1 - EXIT_LAG_PCT/100)
        true_our_exit = true_their_exit * (1 - EXIT_LAG_PCT/100)
        true_gross = (true_our_exit / our_entry - 1) * 100 if our_entry else 0

        diff_pct = ((reported_their_exit / true_their_exit) - 1) * 100 if true_their_exit else 0
        verdict = "OK (real moonshot)" if abs(diff_pct) < 20 else f"DRIFT {diff_pct:+.1f}% — bug suspected"

        print(f"{m[:10]:<12} {w[:10]:<12} {our_entry:>14.3e} {reported_their_exit:>22.3e} {true_their_exit:>20.3e} {reported_gross:>15.1f}% {true_gross:>13.1f}%  {verdict}")


if __name__ == "__main__":
    main()
