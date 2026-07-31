"""
tools/copy_follower_laserstream.py

Laserstream (Helius gRPC, Yellowstone protocol) version of the copy follower.

The Atlas WebSocket version sits at ~305ms median enrich latency:
  WS push of signature → REST enrich → Enhanced shape → handler

Laserstream pushes the *parsed transaction* over a streaming gRPC connection
in a single hop — typical wire-time <50ms. This replaces the enrich step
entirely.

This follower:
  1. Opens a gRPC channel to laserstream-mainnet-ewr.helius-rpc.com:443 with
     the new Helius API key as a metadata header (x-token).
  2. Sends a SubscribeRequest filtered on account_include = [roster wallets]
     so we only get pushes when one of our 8 proven wallets is in a tx.
  3. For each SubscribeUpdate.transaction, maps the protobuf message into
     the Enhanced-shape dict our existing _handle_tx already consumes, then
     calls _handle_tx (reusing ALL the bug fixes from 2026-05-28 — measured
     slip, rug-survivable sizing, bounded loss math, etc).
  4. Runs the stop-loss sweep on the same 3s timer the Atlas version uses.
  5. Hot-reloads the roster file on mtime change (same pattern).

Run: python -m tools.copy_follower_laserstream
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

import grpc
from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROTO_DIR = os.path.join(ROOT, "proto")
if PROTO_DIR not in sys.path:
    sys.path.insert(0, PROTO_DIR)

# Generated protobuf bindings from yellowstone-grpc-proto
import geyser_pb2  # noqa: E402
import geyser_pb2_grpc  # noqa: E402

from tools.copy_follower import (
    _wallet_swap, _log_trade, _load_state, _save_state, _init_account,
    _stop_loss_sweep, _size_for_trade, _realized_pnl_sol, _rug_flag,
    _clamp, STOP_LOSS_PCT, MAX_GAIN_PCT, FEE_PCT, ENTRY_LAG_PCT, EXIT_LAG_PCT,
    MIN_SIZE_SOL,
)
from tools.measured_slip import effective_entry_price


ROSTER = os.path.join(ROOT, "logs", "streaming_roster.json")
STATE_FILE = os.path.join(ROOT, "logs", "copy_follower_state.json")

# Helius Laserstream endpoint (mainnet, US-east — closest to NA validators)
LASERSTREAM_HOST = "laserstream-mainnet-ewr.helius-rpc.com:443"
STOP_SWEEP_INTERVAL_S = 3.0  # matches copy_follower_stream — runs on its own timer

PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def _read_roster() -> list[str]:
    try:
        return list(json.load(open(ROSTER)))
    except Exception:
        return []


def _build_subscribe_request(wallets: list[str]) -> geyser_pb2.SubscribeRequest:
    """Yellowstone subscription: any tx whose accounts include one of our roster
    wallets AND touches pump.fun or PumpSwap. Two filters because we don't want
    every SOL transfer the wallet does, just pump.fun activity.
    """
    req = geyser_pb2.SubscribeRequest()
    flt = req.transactions["pumpfun_roster"]
    flt.vote = False
    flt.failed = False
    flt.account_include.extend(wallets)
    flt.account_required.extend([PUMP_FUN_PROGRAM])  # must include pump.fun
    # CommitmentLevel.PROCESSED = 0 — fastest, accepts orphan risk
    req.commitment = geyser_pb2.CommitmentLevel.PROCESSED
    return req


def _pubkey_b58(bytes_pk: bytes) -> str:
    """Convert raw 32-byte pubkey to base58 string."""
    import base58
    return base58.b58encode(bytes_pk).decode()


def _grpc_tx_to_enhanced(update_tx, watched: set[str]) -> tuple[dict | None, str | None]:
    """Map Yellowstone SubscribeUpdateTransaction → Enhanced-shape dict.

    Returns (enhanced_tx, matched_wallet) or (None, None) if not a pump.fun
    swap we care about.

    Builds the same fields _wallet_swap expects:
      signature, timestamp, feePayer, source, type='SWAP',
      accountData=[{account, nativeBalanceChange}],
      tokenTransfers=[{mint, fromUserAccount, toUserAccount, tokenAmount}]
    """
    tx_info = update_tx.transaction  # SubscribeUpdateTransactionInfo
    if not tx_info:
        return None, None
    sig_bytes = tx_info.signature
    if not sig_bytes:
        return None, None
    sig = _pubkey_b58(sig_bytes)

    # Walk the message keys; identify the fee payer (first signer)
    tx = tx_info.transaction
    if not tx:
        return None, None
    msg = tx.message
    keys = [_pubkey_b58(k) for k in msg.account_keys]
    if not keys:
        return None, None
    fee_payer = keys[0]

    # Detect source from program IDs in keys + log messages
    meta = tx_info.meta
    addrs = list(keys)
    # Add loaded addresses from address-lookup tables
    if meta and meta.loaded_writable_addresses:
        addrs.extend(_pubkey_b58(k) for k in meta.loaded_writable_addresses)
    if meta and meta.loaded_readonly_addresses:
        addrs.extend(_pubkey_b58(k) for k in meta.loaded_readonly_addresses)
    source = None
    if PUMP_FUN_PROGRAM in addrs:
        source = "PUMP_FUN"
    elif PUMP_AMM_PROGRAM in addrs:
        source = "PUMP_AMM"
    if not source:
        return None, None

    # Match a watched wallet
    matched = next((a for a in addrs if a in watched), None)
    if not matched:
        return None, None

    # Build accountData: nativeBalanceChange per account = post - pre
    account_data = []
    pre_bal = list(meta.pre_balances) if meta else []
    post_bal = list(meta.post_balances) if meta else []
    for i, addr in enumerate(addrs):
        if i < len(pre_bal) and i < len(post_bal):
            account_data.append({
                "account": addr,
                "nativeBalanceChange": post_bal[i] - pre_bal[i],
            })

    # Build tokenTransfers from pre/postTokenBalances diffs
    pre_tb = {(tb.account_index, tb.mint): tb for tb in (meta.pre_token_balances if meta else [])}
    post_tb = {(tb.account_index, tb.mint): tb for tb in (meta.post_token_balances if meta else [])}
    token_transfers = []
    all_keys = set(pre_tb) | set(post_tb)
    for k in all_keys:
        idx, mint = k
        pre = pre_tb.get(k)
        post = post_tb.get(k)
        pre_amt = float(pre.ui_token_amount.ui_amount_string) if pre else 0.0
        post_amt = float(post.ui_token_amount.ui_amount_string) if post else 0.0
        delta = post_amt - pre_amt
        if abs(delta) < 1e-12:
            continue
        owner = (post or pre).owner if (post or pre) else (addrs[idx] if idx < len(addrs) else "")
        # delta>0: this account RECEIVED tokens → from=?, to=owner
        # delta<0: this account SENT tokens → from=owner, to=?
        if delta > 0:
            token_transfers.append({
                "mint": mint,
                "fromUserAccount": "",
                "toUserAccount": owner,
                "tokenAmount": delta,
            })
        else:
            token_transfers.append({
                "mint": mint,
                "fromUserAccount": owner,
                "toUserAccount": "",
                "tokenAmount": -delta,
            })

    enhanced = {
        "signature": sig,
        "timestamp": int(time.time()),  # gRPC update doesn't carry blockTime directly
        "feePayer": fee_payer,
        "source": source,
        "type": "SWAP",
        "accountData": account_data,
        "tokenTransfers": token_transfers,
    }
    return enhanced, matched


def _handle_enhanced(tx: dict, w: str, state: dict, key: str, stop_pct: float) -> int:
    """Reuse copy_follower_stream's handler logic exactly, but inline so we
    don't duplicate. The handler updates state['open'] and writes trade events.
    """
    from tools.copy_follower_stream import _handle_tx
    return _handle_tx(tx, w, state, key, stop_pct)


async def _stop_sweep_loop(state: dict, key: str, stop_pct: float):
    """Independent 3-second timer for stop-loss checks. Same fix as Atlas WS."""
    while True:
        try:
            _stop_loss_sweep(state["open"], key, stop_pct, _log_trade)
            _save_state(state)
            print(f"[LSTREAM] heartbeat open={len(state['open'])} @ {time.strftime('%H:%M:%S')}", flush=True)
        except Exception as e:
            print(f"[LSTREAM] sweep error: {type(e).__name__}: {str(e)[:120]}", flush=True)
        await asyncio.sleep(STOP_SWEEP_INTERVAL_S)


async def _stream_loop(state: dict, key: str, helius_api_key: str, stop_pct: float):
    """Connect → subscribe → process. Hot-reloads roster on mtime change."""
    backoff = 1.0
    events = 0
    while True:
        try:
            wallets = _read_roster()
            if not wallets:
                print("[LSTREAM] roster empty — sleeping 30s", flush=True)
                await asyncio.sleep(30)
                continue
            watched = set(wallets)
            roster_mtime = os.path.getmtime(ROSTER)

            # gRPC channel with TLS + auth header
            credentials = grpc.ssl_channel_credentials()
            call_creds = grpc.metadata_call_credentials(
                lambda ctx, cb: cb([("x-token", helius_api_key)], None)
            )
            composite = grpc.composite_channel_credentials(credentials, call_creds)
            channel = grpc.aio.secure_channel(LASERSTREAM_HOST, composite)
            stub = geyser_pb2_grpc.GeyserStub(channel)

            req = _build_subscribe_request(wallets)
            print(f"[LSTREAM] connecting {LASERSTREAM_HOST}, watching {len(wallets)} wallets", flush=True)

            async def request_iter():
                yield req
                # Keep alive — no further requests needed for the basic flow
                while True:
                    await asyncio.sleep(3600)

            call = stub.Subscribe(request_iter())
            backoff = 1.0

            async for update in call:
                # update is a SubscribeUpdate
                if update.HasField("transaction"):
                    enhanced, matched = _grpc_tx_to_enhanced(update.transaction, watched)
                    if not enhanced or not matched:
                        continue
                    sig = enhanced["signature"]
                    if sig in state.get("seen_sig_set", []):
                        continue
                    n = _handle_enhanced(enhanced, matched, state, helius_api_key, stop_pct)
                    if n:
                        events += n
                        print(f"[LSTREAM] event #{events} wallet={matched[:8]} sig={sig[:12]} "
                              f"open={len(state['open'])}", flush=True)

                # Periodic roster hot-reload check
                try:
                    cur_mtime = os.path.getmtime(ROSTER)
                    if cur_mtime > roster_mtime:
                        print(f"[LSTREAM] roster changed — reconnecting", flush=True)
                        await channel.close()
                        break
                except OSError:
                    pass

        except grpc.aio.AioRpcError as e:
            print(f"[LSTREAM] gRPC error {e.code()}: {e.details()[:120]} — reconnecting in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            print(f"[LSTREAM] error {type(e).__name__}: {str(e)[:160]} — reconnecting in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _main_async(state, key, helius_api_key, stop_pct):
    await asyncio.gather(
        _stream_loop(state, key, helius_api_key, stop_pct),
        _stop_sweep_loop(state, key, stop_pct),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", type=float, default=STOP_LOSS_PCT)
    ap.add_argument("--seed-usd", type=float, default=500.0)
    ap.add_argument("--sol-price", type=float, default=85.0)
    ap.add_argument("--risk-pct", type=float, default=2.0)
    args = ap.parse_args()

    env = dotenv_values(os.path.join(ROOT, ".env"))
    helius_api_key = env.get("HELIUS_API_KEY", "")
    if not helius_api_key:
        raise SystemExit("HELIUS_API_KEY missing from .env")

    state = _load_state()
    acct = _init_account(state, args.seed_usd, args.sol_price)
    wallets = _read_roster()
    print(f"Laserstream follower (gRPC) | watching {len(wallets)} wallets | "
          f"stop {args.stop:.0f}% | seed ${acct['seed_usd']:.0f} "
          f"({acct['seed_sol']:.3f} SOL @ ${acct['sol_price_usd']:.0f}) | PAPER", flush=True)

    asyncio.run(_main_async(state, "", helius_api_key, args.stop))


if __name__ == "__main__":
    main()
