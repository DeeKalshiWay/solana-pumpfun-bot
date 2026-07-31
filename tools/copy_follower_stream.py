"""
tools/copy_follower_stream.py

Push-driven copy-follower using Helius Atlas WebSocket (`transactionSubscribe`).
Replaces the 15s polling loop with sub-second detection so we can also copy the
medium-hold (15-30s) edge wallets that polling can't catch in time.

Reuses every guard already proven in the polling follower:
  - fee-payer-paired price reader (fixed)
  - quote-mint filter (USDC/USDT/WSOL never treated as asset)
  - rug_memory hard-reject gate
  - -30% stop-loss with periodic price sweep
  - dynamic sizing (2% bankroll risk, 25% exposure cap, 0.05-0.50 SOL clamp)
  - sanity clamps on slippage and gross PnL

Reads `logs/streaming_roster.json` (union of copyable + edge wallets).
Writes the SAME trade log + state file as the polling follower — switch
between detection modes without losing history.

Run: python -m tools.copy_follower_stream
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import urllib.request

import websockets

from tools.copy_follower import (
    _wallet_swap, mint_last_price, _log_trade, _load_state, _save_state,
    _init_account, _size_for_trade, _stop_loss_sweep, _rug_flag, _key,
    _realized_pnl_sol, _clamp,
    STOP_LOSS_PCT, MAX_SLIP_PCT, MAX_GAIN_PCT, FEE_PCT,
    RUG_HARD_REJECT_MATCHES,
    ROOT, STATE_FILE, MIN_SIZE_SOL,
)

ROSTER = os.path.join(ROOT, "logs", "streaming_roster.json")
STOP_SWEEP_INTERVAL_S = 3.0    # how often to re-check open positions for stop-out
                                # 2026-05-28: tightened 10s -> 3s. Diagnostic showed
                                # 92% of stops overshot the -30% threshold (median exit
                                # -99%), because pump.fun rugs collapse inside one 10s
                                # window. 3s burns 3.3x Helius calls during open
                                # positions but catches more rugs at -30% to -50%
                                # instead of -95% to -99%.


def _build_subscribe(wallets: list[str]):
    return {
        "jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe",
        "params": [
            {"accountInclude": wallets},
            {"commitment": "confirmed",
             "encoding": "jsonParsed",
             "transactionDetails": "full",
             "showRewards": False,
             "maxSupportedTransactionVersion": 0},
        ],
    }


def _extract_sig(msg: dict) -> str | None:
    """Helius Atlas WebSocket pushes RAW Solana RPC txns under params.result, not
    Enhanced parsed format. We only need the signature here — the actual parsed
    fields (events/tokenTransfers/accountData/source/type) get fetched via REST
    after detection.
    """
    r = (msg.get("params") or {}).get("result") or {}
    return r.get("signature") if isinstance(r, dict) else None


def _fetch_enhanced_tx(sig: str, key: str) -> dict | None:
    """REST lookup: turn a raw signature into the Enhanced-shape parsed tx
    our parser expects (events, tokenTransfers, accountData, source, type).

    PRIMARY path: Gatekeeper RPC (`beta.helius-rpc.com`) — ~4× faster than
    the Enhanced REST API. Returns raw Solana RPC; we parse it locally via
    `tools.raw_tx_parser.parse_raw_to_enhanced`.

    FALLBACK path: Enhanced REST (`api.helius.xyz/v0/transactions`) — used
    when Gatekeeper fails or the parser can't identify the source (rare).
    """
    from dotenv import dotenv_values
    from tools.raw_tx_parser import parse_raw_to_enhanced
    gk_url = dotenv_values(os.path.join(ROOT, ".env")).get("GATEKEEPER_RPC_URL", "")

    # Try Gatekeeper first
    if gk_url:
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                "params": [sig, {"encoding": "jsonParsed",
                                  "maxSupportedTransactionVersion": 0,
                                  "commitment": "confirmed"}]
            }).encode()
            req = urllib.request.Request(gk_url, data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "cf-stream-gk"})
            with urllib.request.urlopen(req, timeout=6) as r:
                raw = json.load(r).get("result")
            parsed = parse_raw_to_enhanced(raw)
            if parsed and parsed.get("tokenTransfers"):
                return parsed
            # parser couldn't tag source / no transfers → fall through
        except Exception:
            pass  # fall through to Enhanced REST

    # Fallback: Enhanced REST (slower but always parsed)
    url = f"https://api.helius.xyz/v0/transactions?api-key={key}"
    body = json.dumps({"transactions": [sig]}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "cf-stream-enrich"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            results = json.load(r)
    except Exception:
        return None
    return results[0] if results else None


def _which_wallet(tx, watched: set[str]):
    fp = tx.get("feePayer")
    if fp in watched:
        return fp
    # fall back: any of our wallets appearing in accountData
    for a in tx.get("accountData", []) or []:
        if a.get("account") in watched:
            return a["account"]
    return None


def _handle_tx(tx, w, state, key, stop_pct):
    """Process one streamed tx. Mirrors poll_cycle's per-tx logic exactly."""
    sig = tx.get("signature")
    if not sig:
        return 0
    seen = state.setdefault("seen_sig_set", [])
    if sig in seen:
        return 0
    seen.append(sig)
    if len(seen) > 5000:
        del seen[: len(seen) - 5000]

    sw = _wallet_swap(tx, w)
    if not sw:
        return 0
    mint, direction, their_price, their_sol = sw
    ts = tx.get("timestamp", time.time())
    openpos = state["open"]

    if direction == "buy":
        if mint in openpos:
            openpos[mint]["wallets"] = list(set(openpos[mint]["wallets"] + [w]))
            return 1
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
            return 1
        # THREE PILLARS (memory/rule_paper_honesty_three_pillars.md):
        #  SLIPPAGE: MEASURED post-wallet-swap price, with modeled ENTRY_LAG_PCT
        #            as a floor (we never assume better fill than the model).
        #  LATENCY: detection_latency_ms recorded for forensic audit.
        #  FRICTION: measured/modeled slip baked into our_entry; EXIT_LAG_PCT
        #            + FEE_PCT on close.
        from tools.copy_follower import ENTRY_LAG_PCT
        from tools.measured_slip import effective_entry_price
        our_price, slip_pct, slip_source = effective_entry_price(
            mint, their_price, int(ts) if ts else int(time.time()),
            key, modeled_lag_pct=ENTRY_LAG_PCT,
        )
        rug = _rug_flag(mint, their_sol)
        detection_latency_ms = int((time.time() - ts) * 1000) if ts else None
        openpos[mint] = {"wallets": [w], "their_entry": their_price, "our_entry": our_price,
                         "size_sol": size_sol, "open_ts": ts, "rug_flag": rug}
        _log_trade({"event": "open", "mint": mint, "wallet": w, "their_entry": their_price,
                    "our_entry": our_price,
                    "entry_slip_pct": slip_pct,
                    "slip_source": slip_source,
                    "detection_latency_ms": detection_latency_ms,
                    "rug_flag": rug,
                    "size_sol": size_sol,
                    "size_basis": {"balance_sol": round(balance, 4),
                                    "committed_sol": round(committed, 4),
                                    "conviction": 1},
                    "ts": ts, "detection": "stream"})
        return 1

    # sell branch
    pos = openpos.get(mint)
    if not pos:
        return 0
    # FIX: when copying a wallet's sell, exit at THEIR sell price minus a small
    # copy-lag adverse slip. Old code used mint_last_price(mint) which sampled
    # post-wallet-exit market prices and could be wildly off — that produced
    # the phantom +998.5% paper "win".
    from tools.copy_follower import EXIT_LAG_PCT
    our_exit = their_price * (1.0 - EXIT_LAG_PCT / 100.0)
    gross_raw = (our_exit / pos["our_entry"] - 1) * 100 if pos["our_entry"] else 0.0
    gross = _clamp(gross_raw, -100.0, MAX_GAIN_PCT)
    clamped = gross_raw <= -100.0 or gross_raw >= MAX_GAIN_PCT
    net_pct = gross - FEE_PCT
    pnl_sol = pos["size_sol"] * net_pct / 100.0
    _log_trade({"event": "close", "mint": mint, "wallet": w, "our_entry": pos["our_entry"],
                "our_exit": our_exit, "gross_pct": round(gross, 2), "net_pct": round(net_pct, 2),
                "pnl_sol": round(pnl_sol, 5), "clamped": clamped,
                "hold_s": round(ts - pos["open_ts"], 0),
                "conviction": len(pos["wallets"]),
                "exit_reason": "wallet_sell", "ts": ts, "detection": "stream"})
    del openpos[mint]
    return 1


async def stream(state: dict, key: str, stop_pct: float, roster_path: str):
    """Connect-subscribe loop with HOT RELOAD of the roster file.

    Each outer iteration re-reads the roster from disk, so when wallet_scout
    rewrites streaming_roster.json the next reconnect picks up the new wallets.
    During a connection, the heartbeat checks the roster file's mtime; if
    changed, the WS is closed which falls through to a reconnect with the
    fresh wallet list. Open positions and seen-signatures survive across
    reconnects via the state file.
    """
    url = f"wss://atlas-mainnet.helius-rpc.com/?api-key={key}"
    backoff = 1.0
    events = 0
    while True:
        try:
            # Fresh roster + mtime each outer iteration
            wallets = json.load(open(roster_path))
            watched = set(wallets)
            last_roster_mtime = os.path.getmtime(roster_path)
            async with websockets.connect(url, ping_interval=20, ping_timeout=10,
                                          max_size=2**22) as ws:
                await ws.send(json.dumps(_build_subscribe(wallets)))
                ack = json.loads(await asyncio.wait_for(ws.recv(), 10))
                print(f"[STREAM] connected, sub_id={ack.get('result')}, watching {len(wallets)} wallets",
                      flush=True)
                backoff = 1.0
                loop = asyncio.get_running_loop()
                reconnect_flag = asyncio.Event()

                async def _enrich_and_handle(sig: str):
                    """Off the WS thread: REST-enrich the signature, then run
                    the existing handler against the Enhanced parsed tx."""
                    nonlocal events
                    if sig in state.get("seen_sig_set", []):
                        return
                    tx = await loop.run_in_executor(None, _fetch_enhanced_tx, sig, key)
                    if not tx:
                        return
                    w = _which_wallet(tx, watched)
                    if not w:
                        return
                    n = _handle_tx(tx, w, state, key, stop_pct)
                    events += n
                    if n:
                        print(f"[STREAM] event #{events} wallet={w[:8]} sig={sig[:12]} "
                              f"type={tx.get('type')} source={tx.get('source')} "
                              f"open={len(state['open'])}", flush=True)

                async def _sweep_loop():
                    """STOP-OVERSHOOT FIX (2026-05-28): the previous design ran
                    the stop sweep inside the WS message loop, which means it
                    only fired when new WS traffic arrived. During quiet
                    stretches (no roster wallet trading), open positions could
                    drift to -100% without the sweep ever running. We saw
                    holds of 1644s / 1739s / 12429s producing -40% / -101% /
                    +998% closes because no sweep ran for 27min / 29min / 3.5h.

                    This task runs on its own timer, independent of the WS,
                    and calls the sweep + state save every STOP_SWEEP_INTERVAL_S.
                    It also handles roster hot-reload mtime checking so reconnect
                    works when the WS is quiet.
                    """
                    nonlocal last_roster_mtime
                    try:
                        while not reconnect_flag.is_set():
                            await asyncio.sleep(STOP_SWEEP_INTERVAL_S)
                            try:
                                # Sweep runs in a thread to keep the event loop
                                # responsive — mint_last_price is blocking HTTP.
                                fired = await loop.run_in_executor(
                                    None, _stop_loss_sweep,
                                    state["open"], key, stop_pct, _log_trade
                                )
                            except Exception as e:
                                print(f"[STREAM] sweep error: {type(e).__name__}: {str(e)[:120]}",
                                      flush=True)
                                fired = 0
                            _save_state(state)
                            print(f"[STREAM] heartbeat events={events} open={len(state['open'])} "
                                  f"sweep_fired={fired} @ {time.strftime('%H:%M:%S')}",
                                  flush=True)
                            # Roster hot-reload
                            try:
                                cur_mtime = os.path.getmtime(roster_path)
                                if cur_mtime > last_roster_mtime:
                                    print(f"[STREAM] roster changed — signaling reconnect",
                                          flush=True)
                                    reconnect_flag.set()
                                    return
                            except OSError:
                                pass
                    except asyncio.CancelledError:
                        return

                sweep_task = asyncio.create_task(_sweep_loop())

                first_sig_logged = False
                try:
                    async for raw in ws:
                        if reconnect_flag.is_set():
                            await ws.close()
                            break
                        if not raw or (isinstance(raw, (bytes, bytearray)) and not raw.strip()):
                            continue
                        try:
                            msg = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if msg.get("method") == "transactionNotification":
                            sig = _extract_sig(msg)
                            if not sig:
                                continue
                            if not first_sig_logged:
                                print(f"[STREAM] first sig received: {sig[:12]} -> enriching via REST", flush=True)
                                first_sig_logged = True
                            # Fire-and-forget enrichment so the WS reader stays unblocked
                            asyncio.create_task(_enrich_and_handle(sig))
                finally:
                    sweep_task.cancel()
                    try:
                        await sweep_task
                    except (asyncio.CancelledError, Exception):
                        pass
        except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError) as e:
            print(f"[STREAM] disconnected ({type(e).__name__}); reconnecting in {backoff:.0f}s",
                  flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            print(f"[STREAM] error {type(e).__name__}: {str(e)[:200]}; reconnecting in {backoff:.0f}s",
                  flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", type=float, default=STOP_LOSS_PCT)
    ap.add_argument("--seed-usd", type=float, default=250.0)
    ap.add_argument("--sol-price", type=float, default=170.0)
    ap.add_argument("--risk-pct", type=float, default=2.0)
    ap.add_argument("--min-size", type=float, default=MIN_SIZE_SOL)
    args = ap.parse_args()
    key = _key()
    state = _load_state()
    acct = _init_account(state, args.seed_usd, args.sol_price)
    initial_roster = json.load(open(ROSTER))
    print(f"Streaming follower | watching {len(initial_roster)} wallets (hot-reloads on roster change) | "
          f"stop {args.stop:.0f}% | seed ${acct['seed_usd']:.0f} ({acct['seed_sol']:.3f} SOL "
          f"@ ${acct['sol_price_usd']:.0f}) | PAPER")
    asyncio.run(stream(state, key, args.stop, ROSTER))


if __name__ == "__main__":
    main()
