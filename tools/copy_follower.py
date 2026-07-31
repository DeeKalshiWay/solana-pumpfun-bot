"""
tools/copy_follower.py

LIVE PAPER copy-trade follower. Watches the validated "proven" wallets
(logs/proven_wallets.json), and when one of them buys/sells a pump.fun token,
mirrors the trade into a PAPER ledger — measuring the make-or-break number the
historical backtest can't give us: REAL entry slippage (our realistic fill vs
the wallet's entry price).

NO real money. NO live execution. Polls Helius; writes a paper trade log and a
running summary. Run it for hours/days to accumulate a forward, out-of-sample
track record before deciding on real-money go-live.

Design notes:
- Detection = polling each proven wallet's recent txns every POLL_S seconds.
  This honestly reflects the latency disadvantage we'd have without a Rust hot
  path, which is exactly the risk the backtest flagged.
- Our paper fill price = the mint's latest on-chain trade price at the moment we
  detect their buy (i.e. after our detection lag). slippage = (ours/theirs - 1).
- Exit: when a proven holder sells, we close our paper position at the current
  price and book realized PnL net of a fee assumption.
- Conviction: positions bought by >=2 proven wallets are tagged (the backtest
  suggested co-buys may carry higher win rate).
- Rug flag: logs rug_memory match count for the mint's features (informational
  gate; the full scorer gate applies when wired into main.py).

Run: python -m tools.copy_follower            # default 0.10 SOL/trade, 4s poll
     python -m tools.copy_follower --once      # single poll cycle (smoke test)
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request

from dotenv import dotenv_values

from tools.helius_compat import get_address_transactions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Roster preference order:
#   1. streaming_roster.json — the live, audited, scout-managed roster
#      (this is what the streaming follower watches and what the scout writes).
#   2. copyable_wallets.json — legacy curated subset.
#   3. proven_wallets.json — original full proven list.
# Since Helius credits ran out and we're using polling now, streaming_roster
# is the source of truth so both follower modes agree on the wallet set.
_STREAMING = os.path.join(ROOT, "logs", "streaming_roster.json")
_COPYABLE = os.path.join(ROOT, "logs", "copyable_wallets.json")
PROVEN = (_STREAMING if os.path.exists(_STREAMING)
          else (_COPYABLE if os.path.exists(_COPYABLE)
                else os.path.join(ROOT, "logs", "proven_wallets.json")))
TRADES_LOG = os.path.join(ROOT, "logs", "copy_follower_trades.jsonl")
STATE_FILE = os.path.join(ROOT, "logs", "copy_follower_state.json")

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
# Quote/stable tokens — when the wallet receives USDC after selling a meme token,
# `_wallet_swap` would mistakenly treat USDC as the asset they "bought" and the
# follower would open a paper position on USDC. These are NEVER the copy target.
QUOTE_MINTS = {WSOL, USDC, USDT}
LAMPORTS = 1_000_000_000
RUG_HARD_REJECT_MATCHES = 3   # mirror main bot's signal_scorer gate
SIZE_SOL = 0.10         # legacy / fallback only; live sizing is dynamic via _size_for_trade

# --- THE THREE PILLARS (per memory/rule_paper_honesty_three_pillars.md) -----
# Friction (modeled): calibrated from `tools/friction_analysis.py` for our
# trade-size regime (~0.10-0.25 SOL on a ~30-SOL curve).
#   total all-in ~= 10%  =  ENTRY_LAG_PCT (5)  +  EXIT_LAG_PCT (3)  +  FEE_PCT (2)
# Drop these into the price computations so every PnL number bakes them in.
ENTRY_LAG_PCT = 5.0     # adverse copy-slip on buy: we fill ~5% worse than wallet
FEE_PCT = 2.0           # both-side priority fees on a ~0.1 SOL trade (0.001+0.001)
POLL_S = 4.0
RUG_FEATURES_SCORE = 50 # neutral placeholder for rug signature when scorer isn't in the loop
STOP_LOSS_PCT = 30.0    # hard exit on any position down >= this (rug/dump protection)
SEED_USD = 500.0        # paper account starting bankroll (USD)
SOL_PRICE_USD = 85.0    # reference SOL price for USD<->SOL conversion (operator-set; CLI override)

# --- Dynamic sizing knobs (operator-set 2026-05-22, rug-survivable 2026-05-28)
# RISK_PCT_PER_TRADE = target max loss per trade as % of balance.
#
# The size formula used to be: size = balance × risk_pct / (STOP_LOSS_PCT/100)
# which assumed max loss == 30% (the stop). Empirically pump.fun rugs are
# ~99-100% events that drop through the 10s sweep window faster than we can
# catch. Real max loss per trade is the full position, not 30% of it.
#
# So we now size against RUG_WORST_CASE_PCT (effectively "assume full loss")
# while keeping STOP_LOSS_PCT for the actual exit threshold. This produces a
# ~3.3× smaller position size for the same 2% risk-per-trade target — fewer
# big wins to the upside, but a single rug no longer eats 8% of bankroll.
RUG_WORST_CASE_PCT   = 90.0   # treat each trade as if its worst case is -90% loss
RISK_PCT_PER_TRADE   = 0.02   # 2% bankroll risk per trade (of RUG_WORST_CASE_PCT)
MIN_SIZE_SOL         = 0.05   # below this fixed fees dominate — skip the trade
MAX_SIZE_SOL         = 0.50   # above this our own price impact starts to hurt
MAX_OPEN_EXPOSURE    = 0.25   # cap total committed across open positions to 25% of balance
CONVICTION_BUMP      = {1: 1.0, 2: 1.25, 3: 1.5}   # ×size for co-buyer count


def _key():
    """Legacy: returned the Helius API key. Helius paid is out of credit so
    we now use free RPCs via tools/rpc_pool.py. The key argument is still
    threaded through call signatures for compatibility (unused). If a paid
    Helius key reappears in .env, the polling path will silently still work.
    """
    return dotenv_values(os.path.join(ROOT, ".env")).get("HELIUS_API_KEY", "")


def _http(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "cf"}), timeout=30))


def _native(tx, w):
    for a in tx.get("accountData", []):
        if a.get("account") == w:
            return a.get("nativeBalanceChange", 0) or 0
    return 0


def _wallet_swap(tx, w):
    """For a wallet's pump.fun swap: (asset_mint, 'buy'|'sell', price_sol_per_token, sol) or None.

    Filters QUOTE_MINTS (WSOL/USDC/USDT) — when a wallet sells a meme token TO
    USDC, USDC appears in tokenTransfers but it isn't the asset. We pick the
    non-quote mint as the asset. If both legs are non-quote (rare meme-for-meme
    swap), pick the largest by amount.
    """
    if tx.get("source") not in ("PUMP_FUN", "PUMP_AMM", "PUMPSWAP") or tx.get("type") != "SWAP":
        return None
    candidates = []
    for tt in tx.get("tokenTransfers", []) or []:
        mint = tt.get("mint")
        if not mint or mint in QUOTE_MINTS:
            continue
        amt = float(tt.get("tokenAmount") or 0)
        if amt <= 0:
            continue
        if tt.get("toUserAccount") == w:
            candidates.append((amt, mint, "buy"))
        elif tt.get("fromUserAccount") == w:
            candidates.append((amt, mint, "sell"))
    if not candidates:
        return None
    # Pick the largest non-quote leg as the asset of interest
    candidates.sort(reverse=True)
    _, mint, direction = candidates[0]
    amt = candidates[0][0]
    sol = abs(_native(tx, w)) / LAMPORTS
    if sol <= 0:
        return None
    return mint, direction, sol / amt, sol


def _price_from_tx(tx, mint):
    """Robust SOL-per-UI-token price for `mint` from a single SWAP tx.

    Pairs the FEE PAYER'S native balance change with the FEE PAYER'S
    tokenTransfer for `mint`. The fee payer is the actual trader in a pump.fun
    tx, so their two legs are guaranteed to be the matching pair. The old
    'max nativeTransfer × max tokenTransfer' heuristic was picking unrelated
    legs (ATA rent, protocol fee account, multi-instruction tx noise),
    producing prices on the wrong scale → 65% slip-suspect, 75% clamped exits.
    """
    fp = tx.get("feePayer")
    if not fp:
        return None
    sol_lamports = 0
    for a in tx.get("accountData", []) or []:
        if a.get("account") == fp:
            sol_lamports = abs(a.get("nativeBalanceChange", 0) or 0)
            break
    if sol_lamports <= 0:
        return None
    sol = sol_lamports / LAMPORTS
    # Largest tokenTransfer of `mint` where the fee payer is on either side
    amt = 0.0
    for tt in tx.get("tokenTransfers", []) or []:
        if tt.get("mint") != mint:
            continue
        if tt.get("toUserAccount") == fp or tt.get("fromUserAccount") == fp:
            a = float(tt.get("tokenAmount") or 0)
            if a > amt:
                amt = a
    if amt <= 0:
        return None
    return sol / amt


def mint_last_price(mint, key):
    """Current price ~ most recent valid on-chain swap price for the mint.

    Free-RPC path: helius_compat fetches sigs via rpc_pool and parses raw
    txs into Enhanced shape. `key` is kept for signature compatibility but
    no longer used (pool reads FREE_RPC_URLS from .env directly).
    """
    try:
        d = get_address_transactions(mint, limit=3)
    except Exception:
        return None
    for tx in (d or []):
        p = _price_from_tx(tx, mint)
        if p:
            return p
    return None


# Sanity bounds so a bad price read can never book a fake moonshot / impossible
# loss. A copyable slow-hold trade realistically lands within these.
# Tightened 2026-05-28 from 1000% to 500% — 10× clamps were firing twice in one
# session (4TWm7LhQ over 3.5hr, 73a7UCnQ in 221s). At 5× the clamp still allows
# real scalper edges (most copyable wins are 5-50%) but starts flagging suspect
# reads earlier so we can audit. The close record carries `gross_raw` for
# audit when clamp fires.
MAX_GAIN_PCT = 500.0    # +5x cap; above this we suspect bad price read
MAX_SLIP_PCT = 60.0     # |entry slippage| beyond this = unreliable read, flag it

# When we copy a wallet's SELL we exit at approximately THEIR sell price, with
# a small adverse slip to model the cost of selling slightly after they did
# (we land a tx in the next block or so; price tends to be a touch lower).
# Using mint_last_price after their sell pulled phantom prices off post-exit
# market activity — that's where the +998.5% bug came from.
EXIT_LAG_PCT = 3.0

# Stop-loss sanity guard: if mint_last_price returns a price more than this
# multiple from our_entry, treat as a corrupt read and skip the stop check this
# cycle rather than booking a fake -100%.
STOP_PRICE_SANITY_RATIO = 10.0


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


def _rug_flag(mint, their_buy_sol):
    try:
        from analyzer.rug_memory import rug_memory
        feats = {"initial_buy_sol": their_buy_sol, "bonding_curve_pct": 20.0, "score": RUG_FEATURES_SCORE}
        return int(rug_memory.matched_count(feats))
    except Exception:
        return -1


def _log_trade(rec):
    with open(TRADES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _size_for_trade(balance_sol, committed_sol, conviction,
                    risk_pct=RISK_PCT_PER_TRADE, stop_pct=STOP_LOSS_PCT,
                    min_size=MIN_SIZE_SOL, max_size=MAX_SIZE_SOL,
                    max_exposure=MAX_OPEN_EXPOSURE,
                    worst_case_pct=RUG_WORST_CASE_PCT):
    """Return (size_sol, reason). reason is 'ok' / 'min_floor' / 'exposure_cap' / 'no_funds'.

    Sizing rule (updated 2026-05-28 for rug-survivability):
      base = balance × risk_pct / (worst_case_pct/100)
        - worst_case_pct=90 means "assume each loser is a -90% rug, not -30% stop"
        - implies max loss == ~risk_pct of balance per trade in the rug scenario
      size = clamp(base × conviction_bump, min_size, max_size)
      cap to remaining bankroll AND remaining exposure room
      reject if final size < min_size

    stop_pct still drives the actual stop-loss exit; this just makes sizing
    assume the stop can't catch a 1-second rug (because empirically it can't).
    """
    if balance_sol <= 0:
        return 0.0, "no_funds"
    bump = CONVICTION_BUMP.get(min(conviction, max(CONVICTION_BUMP)), max(CONVICTION_BUMP.values()))
    base = balance_sol * risk_pct / (worst_case_pct / 100.0)
    target = max(min_size, min(max_size, base * bump))
    # Available capital: balance minus already-committed open positions
    free = balance_sol - committed_sol
    if free < min_size:
        return 0.0, "no_funds"
    # Open-exposure cap: total committed cannot exceed max_exposure × balance
    room = max(0.0, balance_sol * max_exposure - committed_sol)
    if room < min_size:
        return 0.0, "exposure_cap"
    size = min(target, free, room)
    if size < min_size:
        return 0.0, "min_floor"
    return round(size, 4), "ok"


def _realized_pnl_sol():
    """Sum of pnl_sol across all logged close events. Used for the bankroll check."""
    if not os.path.exists(TRADES_LOG):
        return 0.0
    total = 0.0
    for line in open(TRADES_LOG, encoding="utf-8"):
        line = line.strip()
        if not line or '"close"' not in line:
            continue
        try:
            r = json.loads(line)
            if r.get("event") == "close":
                total += float(r.get("pnl_sol") or 0)
        except Exception:
            pass
    return total


def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"seen_sigs": {}, "open": {}}


def _init_account(state, seed_usd, sol_price_usd):
    """Seed the paper account on first launch; preserve across restarts."""
    if "account" not in state:
        state["account"] = {
            "seed_usd": seed_usd,
            "sol_price_usd": sol_price_usd,
            "seed_sol": round(seed_usd / sol_price_usd, 5),
            "skipped_insufficient": 0,
        }
    return state["account"]


def _save_state(s):
    tmp = STATE_FILE + ".tmp"
    json.dump(s, open(tmp, "w"))
    os.replace(tmp, STATE_FILE)


def _stop_loss_sweep(openpos, key, stop_pct, log_close):
    """For each open position, fetch current price and close if down >= stop_pct.

    Returns the number of stop-outs. Stop fires BEFORE the proven wallet sells —
    this is the production-style protection against rugs / deep dumps where
    waiting on their signal would let the position go to ~0.
    """
    fired = 0
    for mint in list(openpos.keys()):
        pos = openpos[mint]
        cur = mint_last_price(mint, key)
        if not cur or not pos.get("our_entry"):
            continue
        # NOTE: previously had a 10x/1/10x sanity guard that skipped the stop
        # check when the price reading was wildly off entry. That was too tight
        # for pump.fun reality (12 orders of magnitude of price range, real rugs
        # routinely drop 99%+, real moonshots routinely go 100x+) — and it
        # silently held real rugs instead of stopping them. Removed.
        unreal = (cur / pos["our_entry"] - 1) * 100
        if unreal <= -stop_pct:
            gross = _clamp(unreal, -100.0, MAX_GAIN_PCT)
            # IMPOSSIBLE-LOSS FIX (2026-05-28): a paper position can lose at most
            # its principal. Subtracting FEE_PCT after gross hit the -100 floor
            # was producing pnl_sol < -size_sol (e.g. -0.51 on a 0.50 SOL stake
            # → reported as -101%). You can't pay a fee on a zero balance.
            net_pct = max(-100.0, gross - FEE_PCT)
            pnl_sol = max(-pos["size_sol"], pos["size_sol"] * net_pct / 100.0)
            log_close({
                "event": "close", "mint": mint, "wallet": pos["wallets"][-1],
                "our_entry": pos["our_entry"], "our_exit": cur,
                "gross_pct": round(gross, 2), "net_pct": round(net_pct, 2),
                "pnl_sol": round(pnl_sol, 5), "clamped": False,
                "hold_s": round(time.time() - pos["open_ts"], 0),
                "conviction": len(pos["wallets"]),
                "exit_reason": "stop_loss",
                "ts": time.time(),
            })
            del openpos[mint]
            fired += 1
    return fired


def poll_cycle(wallets, key, state, stop_pct=STOP_LOSS_PCT):
    seen = state["seen_sigs"]
    openpos = state["open"]
    new_events = 0
    # Run stop-loss BEFORE polling wallet events: protects against rugs that
    # happen between our open and the next wallet-driven update.
    new_events += _stop_loss_sweep(openpos, key, stop_pct, _log_trade)
    for w in wallets:
        try:
            # Free-RPC path: rpc_pool round-robins free Solana RPCs,
            # raw_tx_parser converts to the Enhanced shape the rest of this
            # follower already consumes.
            txns = get_address_transactions(w, limit=25)
        except Exception:
            continue
        last = seen.get(w)
        if txns:
            seen[w] = txns[0].get("signature")
        if last is None:
            # first time seeing this wallet — set baseline, don't copy stale history
            continue
        fresh = []
        for tx in txns:
            if tx.get("signature") == last:
                break
            fresh.append(tx)
        for tx in reversed(fresh):  # oldest first
            sw = _wallet_swap(tx, w)
            if not sw:
                continue
            mint, direction, their_price, their_sol = sw
            ts = tx.get("timestamp", time.time())
            if direction == "buy":
                if mint in openpos:
                    openpos[mint]["wallets"] = list(set(openpos[mint]["wallets"] + [w]))
                    new_events += 1
                    continue
                # --- Dynamic sizing: bot decides size from bankroll + risk + conviction
                acct = state.get("account") or {}
                realized = _realized_pnl_sol()
                balance = (acct.get("seed_sol", 0) or 0) + realized
                committed = sum(p.get("size_sol", 0) for p in openpos.values())
                size_sol, reason = _size_for_trade(balance, committed, conviction=1)
                if reason != "ok":
                    if reason == "no_funds" and acct:
                        acct["skipped_insufficient"] = acct.get("skipped_insufficient", 0) + 1
                    _log_trade({"event": "skip", "reason": reason, "mint": mint, "wallet": w,
                                "balance_sol": round(balance, 4),
                                "committed_sol": round(committed, 4), "ts": ts})
                    continue
                # THREE PILLARS (memory/rule_paper_honesty_three_pillars.md):
                #  SLIPPAGE: modeled, never read from mint_last_price (that was
                #    the source of the phantom -7.72% medians and the +998.5%
                #    paper-win bug). Paper mode is HONEST about modeling.
                #  LATENCY: detection_latency_ms = (now - tx_timestamp) * 1000.
                #  FRICTION: ENTRY_LAG_PCT baked into our_entry; EXIT_LAG_PCT
                #    + FEE_PCT applied on close. Total ~10% all-in.
                our_price = their_price * (1.0 + ENTRY_LAG_PCT / 100.0)
                rug = _rug_flag(mint, their_sol)
                detection_latency_ms = int((time.time() - ts) * 1000) if ts else None
                openpos[mint] = {"wallets": [w], "their_entry": their_price, "our_entry": our_price,
                                 "size_sol": size_sol, "open_ts": ts, "rug_flag": rug}
                _log_trade({"event": "open", "mint": mint, "wallet": w, "their_entry": their_price,
                            "our_entry": our_price,
                            "entry_slip_pct": ENTRY_LAG_PCT,
                            "slip_source": "modeled",
                            "detection_latency_ms": detection_latency_ms,
                            "rug_flag": rug,
                            "size_sol": size_sol,
                            "size_basis": {"balance_sol": round(balance, 4),
                                           "committed_sol": round(committed, 4),
                                           "conviction": 1},
                            "ts": ts})
                new_events += 1
            else:  # sell
                pos = openpos.get(mint)
                if not pos:
                    continue
                # When the wallet sells, OUR exit ~= THEIR sell price minus a
                # small adverse copy-lag slip. Using mint_last_price after
                # their sell pulled post-exit market prices that could be
                # arbitrarily off (that was the +998.5% phantom-win bug).
                our_exit = their_price * (1.0 - EXIT_LAG_PCT / 100.0)
                gross_raw = (our_exit / pos["our_entry"] - 1) * 100 if pos["our_entry"] else 0.0
                # Clamp to [-100, +MAX_GAIN]: a paper position can't lose more
                # than the size invested, and a >10x read on a copyable trade is
                # treated as a bad price feed rather than a real moonshot.
                gross = _clamp(gross_raw, -100.0, MAX_GAIN_PCT)
                clamped = gross_raw <= -100.0 or gross_raw >= MAX_GAIN_PCT
                # IMPOSSIBLE-LOSS FIX (2026-05-28): same as in _stop_loss_sweep.
                net_pct = max(-100.0, gross - FEE_PCT)
                pnl_sol = max(-pos["size_sol"], pos["size_sol"] * net_pct / 100.0)
                _log_trade({"event": "close", "mint": mint, "wallet": w, "our_entry": pos["our_entry"],
                            "our_exit": our_exit, "gross_pct": round(gross, 2), "net_pct": round(net_pct, 2),
                            "gross_raw": round(gross_raw, 2),   # audit trail when clamp fires
                            "pnl_sol": round(pnl_sol, 5), "clamped": clamped,
                            "hold_s": round(ts - pos["open_ts"], 0),
                            "conviction": len(pos["wallets"]),
                            "exit_reason": "wallet_sell", "ts": ts})
                del openpos[mint]
                new_events += 1
    return new_events


def _summary():
    if not os.path.exists(TRADES_LOG):
        print("no trades yet"); return
    closes = [json.loads(l) for l in open(TRADES_LOG, encoding="utf-8") if l.strip() and '"close"' in l]
    opens = sum(1 for l in open(TRADES_LOG, encoding="utf-8") if '"open"' in l)
    if not closes:
        print(f"opens: {opens} | closed: 0 (positions still open or none yet)"); return
    net = sum(c["pnl_sol"] for c in closes)
    win = sum(1 for c in closes if c["pnl_sol"] > 0) / len(closes)
    slips = [json.loads(l)["entry_slip_pct"] for l in open(TRADES_LOG, encoding="utf-8") if '"open"' in l and 'entry_slip_pct' in l]
    import statistics as st
    print(f"opens {opens} | closed {len(closes)} | paper net {net:+.3f} SOL | win {win*100:.0f}% | "
          f"median entry-slip {st.median(slips):.1f}% (mean {st.mean(slips):.1f}%)" if slips else "")


def main():
    global RISK_PCT_PER_TRADE, MIN_SIZE_SOL, MAX_SIZE_SOL, MAX_OPEN_EXPOSURE
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single poll cycle then exit (smoke test)")
    ap.add_argument("--poll", type=float, default=POLL_S)
    ap.add_argument("--stop", type=float, default=STOP_LOSS_PCT,
                    help=f"hard stop-loss percent (default {STOP_LOSS_PCT})")
    ap.add_argument("--seed-usd", type=float, default=SEED_USD,
                    help=f"paper account starting bankroll in USD (default {SEED_USD})")
    ap.add_argument("--sol-price", type=float, default=SOL_PRICE_USD,
                    help=f"reference SOL price in USD (default {SOL_PRICE_USD})")
    ap.add_argument("--risk-pct", type=float, default=RISK_PCT_PER_TRADE * 100,
                    help=f"bankroll risk per trade in %% (default {RISK_PCT_PER_TRADE*100})")
    ap.add_argument("--min-size", type=float, default=MIN_SIZE_SOL)
    ap.add_argument("--max-size", type=float, default=MAX_SIZE_SOL)
    ap.add_argument("--max-exposure-pct", type=float, default=MAX_OPEN_EXPOSURE * 100,
                    help=f"cap on total open exposure as %% of balance (default {MAX_OPEN_EXPOSURE*100})")
    args = ap.parse_args()
    # propagate to module-level so _size_for_trade picks them up
    RISK_PCT_PER_TRADE = args.risk_pct / 100.0
    MIN_SIZE_SOL = args.min_size
    MAX_SIZE_SOL = args.max_size
    MAX_OPEN_EXPOSURE = args.max_exposure_pct / 100.0
    key = _key()
    wallets = json.load(open(PROVEN))
    state = _load_state()
    acct = _init_account(state, args.seed_usd, args.sol_price)
    print(f"Following {len(wallets)} proven wallets | DYNAMIC sizing "
          f"(risk {args.risk_pct:.1f}%/trade, clamp [{MIN_SIZE_SOL}, {MAX_SIZE_SOL}] SOL, "
          f"exposure cap {args.max_exposure_pct:.0f}%) | poll {args.poll}s | stop {args.stop:.0f}% | "
          f"seed ${acct['seed_usd']:.0f} ({acct['seed_sol']:.3f} SOL @ ${acct['sol_price_usd']:.0f}) | PAPER")
    if args.once:
        n = poll_cycle(wallets, key, state, stop_pct=args.stop)
        _save_state(state)
        print(f"poll cycle done: {n} new events | open positions: {len(state['open'])}")
        _summary()
        return
    cycles = 0
    while True:
        try:
            n = poll_cycle(wallets, key, state, stop_pct=args.stop)
            _save_state(state)
            cycles += 1
            if n or cycles % 15 == 0:
                print(f"[cycle {cycles}] new events: {n} | open: {len(state['open'])}", flush=True)
                _summary()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("cycle error:", e, flush=True)
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
