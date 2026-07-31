"""
tools/raw_tx_parser.py

Convert a raw Solana JSON-RPC `getTransaction` response (jsonParsed encoding)
into the Helius Enhanced-format dict our follower's parser already consumes.

Why this exists: the Gatekeeper RPC endpoint (beta.helius-rpc.com) is ~4×
faster than the Enhanced REST API but returns raw Solana RPC, not the parsed
Helius fields. This module reconstructs the fields `_wallet_swap` needs:

  signature, timestamp, feePayer, source, type,
  accountData       — list of {account, nativeBalanceChange}
  tokenTransfers    — list of {mint, fromUserAccount, toUserAccount, tokenAmount}

For the cases we care about (single-user pump.fun swap on the bonding curve or
PumpSwap), the mapping from preBalances/postBalances and
preTokenBalances/postTokenBalances diffs is mechanical.

Returns None if the tx isn't a pump.fun-family swap so the caller can fall
back to the Enhanced REST endpoint.
"""

from __future__ import annotations

# Known program IDs. If any of these appear in the tx's account keys, we tag
# the source accordingly. Both PumpSwap variants are included; new variants
# can be added without breaking the parser.
_PROGRAM_TO_SOURCE = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "PUMP_FUN",   # pump.fun bonding curve
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PUMP_AMM",   # PumpSwap AMM
}


def _acc_pubkey(a) -> str | None:
    """accountKeys can be list-of-strings (base58 encoding) or list-of-dicts
    (jsonParsed encoding). Normalize."""
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        return a.get("pubkey")
    return None


def _detect_source(addrs: list[str], log_messages: list[str] | None) -> str | None:
    for a in addrs:
        if a in _PROGRAM_TO_SOURCE:
            return _PROGRAM_TO_SOURCE[a]
    # Fallback: scan log messages for "Program ... invoke" lines
    if log_messages:
        for log in log_messages:
            if isinstance(log, str):
                for prog_id, src in _PROGRAM_TO_SOURCE.items():
                    if prog_id in log:
                        return src
    return None


def parse_raw_to_enhanced(raw_result: dict) -> dict | None:
    """Convert a raw `getTransaction` result to the Enhanced-shape dict.

    `raw_result` is the `result` field of a JSON-RPC getTransaction response
    (i.e., the dict with keys `blockTime`, `meta`, `slot`, `transaction`, ...).

    Returns None if not a pump.fun-family swap so the caller falls back.
    """
    if not raw_result or not isinstance(raw_result, dict):
        return None
    meta = raw_result.get("meta") or {}
    tx = raw_result.get("transaction") or {}
    msg = tx.get("message") or {}

    # accountKeys
    raw_keys = msg.get("accountKeys") or []
    addrs = [_acc_pubkey(a) for a in raw_keys]
    addrs = [a for a in addrs if a]

    # Detect source (skip early if not pump.fun-family)
    source = _detect_source(addrs, meta.get("logMessages"))
    if source is None:
        return None

    # Identify fee payer (first signer in jsonParsed = accountKeys[0])
    fee_payer = addrs[0] if addrs else None
    signature = (tx.get("signatures") or [None])[0]
    timestamp = raw_result.get("blockTime")

    # accountData: native balance change per account
    pre_bal = meta.get("preBalances") or []
    post_bal = meta.get("postBalances") or []
    account_data = []
    for i, addr in enumerate(addrs):
        if i < len(pre_bal) and i < len(post_bal):
            account_data.append({
                "account": addr,
                "nativeBalanceChange": post_bal[i] - pre_bal[i],
            })

    # tokenTransfers: synthesize from pre/postTokenBalances diffs
    pre_tb = meta.get("preTokenBalances") or []
    post_tb = meta.get("postTokenBalances") or []
    # Index by (accountIndex, mint) so we can pair pre and post
    def _index(tbs):
        out = {}
        for tb in tbs:
            key = (tb.get("accountIndex"), tb.get("mint"))
            out[key] = tb
        return out

    pre_idx = _index(pre_tb)
    post_idx = _index(post_tb)
    all_keys = set(pre_idx) | set(post_idx)

    # Aggregate by (owner, mint): positive delta = received, negative = sent
    by_owner_mint: dict[tuple[str, str], float] = {}
    for key in all_keys:
        pre = pre_idx.get(key) or {}
        post = post_idx.get(key) or {}
        pre_ui = float((pre.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        post_ui = float((post.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        delta = post_ui - pre_ui
        if delta == 0:
            continue
        owner = post.get("owner") or pre.get("owner")
        mint = key[1]
        if not owner or not mint:
            continue
        by_owner_mint[(owner, mint)] = by_owner_mint.get((owner, mint), 0.0) + delta

    # Synthesize transfer entries by pairing receivers (delta > 0) with senders
    # (delta < 0) on the same mint.
    receivers = [(o, m, d) for (o, m), d in by_owner_mint.items() if d > 0]
    senders = [(o, m, -d) for (o, m), d in by_owner_mint.items() if d < 0]
    token_transfers = []
    used_senders = set()
    for r_owner, r_mint, r_amt in receivers:
        # find sender with same mint (largest first)
        match = None
        for i, (s_owner, s_mint, s_amt) in enumerate(senders):
            if i in used_senders or s_mint != r_mint:
                continue
            if match is None or s_amt > senders[match][2]:
                match = i
        if match is not None:
            s_owner, s_mint, s_amt = senders[match]
            used_senders.add(match)
            token_transfers.append({
                "mint": r_mint,
                "fromUserAccount": s_owner,
                "toUserAccount": r_owner,
                "tokenAmount": min(r_amt, s_amt),
            })
        else:
            # one-sided receive (rare in pump.fun trades)
            token_transfers.append({
                "mint": r_mint,
                "fromUserAccount": None,
                "toUserAccount": r_owner,
                "tokenAmount": r_amt,
            })
    # leftover senders without matching receivers
    for i, (s_owner, s_mint, s_amt) in enumerate(senders):
        if i not in used_senders:
            token_transfers.append({
                "mint": s_mint,
                "fromUserAccount": s_owner,
                "toUserAccount": None,
                "tokenAmount": s_amt,
            })

    return {
        "signature": signature,
        "timestamp": timestamp,
        "feePayer": fee_payer,
        "source": source,
        "type": "SWAP",
        "accountData": account_data,
        "tokenTransfers": token_transfers,
        "nativeTransfers": [],  # not consumed by _wallet_swap; left empty
    }
