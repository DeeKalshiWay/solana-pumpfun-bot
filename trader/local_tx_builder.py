"""
trader/local_tx_builder.py

Build pump.fun buy/sell transactions locally instead of round-tripping
through PumpPortal's /api/trade-local HTTP endpoint. Skips 200-500ms
per buy and per sell.

⚠️  EXPERIMENTAL — default OFF (`USE_LOCAL_TX_BUILD=false`).

WHY EXPERIMENTAL
----------------
The pump.fun program account layout for the buy/sell instruction is
encoded against an Anchor IDL we don't have local access to from the
sandbox where this was written. The account order below matches what
multiple open-source pump.fun bots use as of early 2026, but a bad
encoding here = lost gas (or worse, lost SOL on a successful but
wrong-amount swap) on every trade.

VALIDATION BEFORE ENABLING
--------------------------
Before flipping `USE_LOCAL_TX_BUILD=true`, run this script — it calls
PumpPortal's API with the same params as the local builder and diffs
the resulting tx bytes. If they match, the local builder is correct.

    python -m tools.validate_local_tx --mint <real-mint> --sol 0.001

(Tool not yet written — track in NEXT_SESSION_TRIGGERS.md.)

WHAT'S DETERMINISTIC
--------------------
- Pump.fun program ID:           6EF8rrec... (verified in main.py:212)
- Bonding-curve PDA seed:        b"bonding-curve" || mint (verified)
- Global PDA seed:               b"global"
- Event authority PDA seed:      b"__event_authority"
- Anchor discriminator:          sha256(f"global:{ix_name}")[:8]
- Associated token account:      standard SPL derivation
- Argument encoding:             u64 little-endian

WHAT'S A BEST GUESS
-------------------
- Account ORDER in the buy/sell instruction (matches public pumpfun bots)
- Fee recipient address (CebN... — well-known but could change; pull from
  global account once we add the global-fetch caching path)
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

# ── Well-known Solana program IDs ───────────────────────────────────────────
PUMP_FUN_PROGRAM   = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
TOKEN_PROGRAM      = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROGRAM        = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM     = Pubkey.from_string("11111111111111111111111111111111")
RENT_SYSVAR        = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# Pump.fun fee recipient. This is the well-known recipient as of early
# 2026. The authoritative source is the `fee_recipient` field of the
# global PDA — fetch it from chain and cache once we add that path.
FEE_RECIPIENT      = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")


# ── Anchor instruction discriminators ───────────────────────────────────────
def _anchor_discriminator(ix_name: str) -> bytes:
    """First 8 bytes of sha256('global:<ix_name>'). Anchor convention."""
    return hashlib.sha256(f"global:{ix_name}".encode()).digest()[:8]


BUY_DISCRIMINATOR  = _anchor_discriminator("buy")    # [102, 6, 61, 18, 1, 218, 235, 234]
SELL_DISCRIMINATOR = _anchor_discriminator("sell")   # [51, 230, 133, 164, 1, 127, 131, 173]


# ── PDA derivations ─────────────────────────────────────────────────────────

def bonding_curve_pda(mint: Pubkey) -> Pubkey:
    """[b'bonding-curve', mint]. Matches main.py:_bonding_curve_price."""
    addr, _bump = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint)],
        PUMP_FUN_PROGRAM,
    )
    return addr


def global_pda() -> Pubkey:
    """[b'global']. Holds program config + current fee_recipient."""
    addr, _bump = Pubkey.find_program_address([b"global"], PUMP_FUN_PROGRAM)
    return addr


def event_authority_pda() -> Pubkey:
    """[b'__event_authority']. Anchor convention for emit! events."""
    addr, _bump = Pubkey.find_program_address(
        [b"__event_authority"], PUMP_FUN_PROGRAM,
    )
    return addr


def associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """Standard SPL associated token account derivation."""
    addr, _bump = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM), bytes(mint)],
        ATA_PROGRAM,
    )
    return addr


def build_create_ata_idempotent_instruction(
    payer: Pubkey,
    owner: Pubkey,
    mint:  Pubkey,
) -> Instruction:
    """SPL Associated Token Account program's `CreateIdempotent` ix.

    Why this exists: the very first time a wallet touches a given pump.fun
    mint, the wallet's ATA for that mint doesn't exist on chain. The
    pump.fun buy instruction then fails with Anchor error 3012
    (`AccountNotInitialized`) when it tries to credit tokens to a missing
    ATA. Solution: prepend a create-ATA instruction. The Idempotent
    variant is safe to include on EVERY buy — if the ATA already exists,
    it's a no-op (~1.5K CU). If it doesn't, it creates it (~10K CU).
    Either way the buy proceeds in the same tx.

    Account layout (SPL ATA program v1.0.4+):
      0. [writable, signer] funding account (pays rent)
      1. [writable]          associated_token_account (the ATA to create)
      2. [                ]  wallet (owner of the ATA, NOT signer)
      3. [                ]  mint
      4. [                ]  system program
      5. [                ]  SPL token program
    Data: single byte 0x01 (CreateIdempotent discriminator).
    """
    ata = associated_token_address(owner, mint)
    return Instruction(
        program_id = ATA_PROGRAM,
        data       = bytes([1]),
        accounts   = [
            AccountMeta(pubkey=payer,          is_signer=True,  is_writable=True),
            AccountMeta(pubkey=ata,            is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner,          is_signer=False, is_writable=False),
            AccountMeta(pubkey=mint,           is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=TOKEN_PROGRAM,  is_signer=False, is_writable=False),
        ],
    )


# ── Instruction builders ────────────────────────────────────────────────────

@dataclass(frozen=True)
class SwapAccounts:
    """Resolved PDAs + ATAs that both buy and sell instructions need.
    Computed once per (user, mint) pair."""
    global_pda:               Pubkey
    fee_recipient:            Pubkey
    mint:                     Pubkey
    bonding_curve:            Pubkey
    associated_bonding_curve: Pubkey
    associated_user:          Pubkey
    user:                     Pubkey
    event_authority:          Pubkey


def resolve_swap_accounts(user: Pubkey, mint: Pubkey) -> SwapAccounts:
    bc = bonding_curve_pda(mint)
    return SwapAccounts(
        global_pda               = global_pda(),
        fee_recipient            = FEE_RECIPIENT,
        mint                     = mint,
        bonding_curve            = bc,
        associated_bonding_curve = associated_token_address(bc, mint),
        associated_user          = associated_token_address(user, mint),
        user                     = user,
        event_authority          = event_authority_pda(),
    )


def _swap_account_metas(accts: SwapAccounts) -> list[AccountMeta]:
    """Account list for both BUY and SELL — same order, same writable
    flags. Matches the layout used by multiple open-source pump.fun
    sniper bots (D3AD-E, chainstacklabs, slycompiler).

    ⚠️  Order is the highest-risk part of this file. If pump.fun's
    on-chain program expects a different order, the instruction will
    fail (best case: lost gas + clear error) or succeed with the wrong
    accounts (worst case: lost SOL on a wrong-amount swap). MUST be
    validated against PumpPortal API output before enabling.
    """
    return [
        AccountMeta(pubkey=accts.global_pda,               is_signer=False, is_writable=False),
        AccountMeta(pubkey=accts.fee_recipient,            is_signer=False, is_writable=True),
        AccountMeta(pubkey=accts.mint,                     is_signer=False, is_writable=False),
        AccountMeta(pubkey=accts.bonding_curve,            is_signer=False, is_writable=True),
        AccountMeta(pubkey=accts.associated_bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=accts.associated_user,          is_signer=False, is_writable=True),
        AccountMeta(pubkey=accts.user,                     is_signer=True,  is_writable=True),
        AccountMeta(pubkey=SYSTEM_PROGRAM,                 is_signer=False, is_writable=False),
        AccountMeta(pubkey=TOKEN_PROGRAM,                  is_signer=False, is_writable=False),
        AccountMeta(pubkey=RENT_SYSVAR,                    is_signer=False, is_writable=False),
        AccountMeta(pubkey=accts.event_authority,          is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_FUN_PROGRAM,               is_signer=False, is_writable=False),
    ]


def build_buy_instruction(
    user:         Pubkey,
    mint:         Pubkey,
    token_amount: int,          # raw token units to receive
    max_sol_cost: int,          # max lamports willing to spend (slippage cap)
) -> Instruction:
    """Anchor buy(amount: u64, max_sol_cost: u64).

    Slippage: caller pre-computes `token_amount` from a quote, then
    `max_sol_cost` is the slippage-padded SOL spend ceiling.
    """
    if token_amount < 0 or max_sol_cost < 0:
        raise ValueError("token_amount and max_sol_cost must be non-negative")
    data = (
        BUY_DISCRIMINATOR
        + struct.pack("<Q", int(token_amount))
        + struct.pack("<Q", int(max_sol_cost))
    )
    return Instruction(
        program_id = PUMP_FUN_PROGRAM,
        data       = data,
        accounts   = _swap_account_metas(resolve_swap_accounts(user, mint)),
    )


def build_sell_instruction(
    user:           Pubkey,
    mint:           Pubkey,
    token_amount:   int,        # raw token units to sell
    min_sol_output: int,        # min lamports willing to accept (slippage cap)
) -> Instruction:
    """Anchor sell(amount: u64, min_sol_output: u64)."""
    if token_amount < 0 or min_sol_output < 0:
        raise ValueError("token_amount and min_sol_output must be non-negative")
    data = (
        SELL_DISCRIMINATOR
        + struct.pack("<Q", int(token_amount))
        + struct.pack("<Q", int(min_sol_output))
    )
    return Instruction(
        program_id = PUMP_FUN_PROGRAM,
        data       = data,
        accounts   = _swap_account_metas(resolve_swap_accounts(user, mint)),
    )


# ── Bonding-curve math (pump.fun constant-product) ──────────────────────────
#
# pump.fun uses virtual reserves (v_sol, v_token). At buy time you put in
# `sol_in` and you receive `tokens_out` such that the product stays constant:
#
#   (v_sol + sol_in) * (v_token - tokens_out) = v_sol * v_token
#
# Solving for tokens_out:
#
#   tokens_out = v_token - (v_sol * v_token) / (v_sol + sol_in)
#              = v_token * sol_in / (v_sol + sol_in)
#
# Symmetric on the sell side:
#
#   sol_out = v_sol * tokens_in / (v_token + tokens_in)
#
# Caller passes virtual reserves read from the bonding curve PDA. These
# units are the same as what the program stores: SOL is in lamports
# (u64), tokens are in raw u64 (account for decimals — pump.fun mints
# use 6 decimals so 1 token = 1_000_000 raw units).


def expected_tokens_for_sol(v_sol: int, v_token: int, sol_in: int) -> int:
    """Pump.fun buy math. Returns raw token units the swap will yield
    given the current curve state, BEFORE slippage padding.

    Pure function; deterministic; no I/O. Integer math (floor-div) —
    matches the on-chain Anchor program's integer arithmetic.
    """
    if v_sol <= 0 or v_token <= 0 or sol_in <= 0:
        return 0
    return (v_token * sol_in) // (v_sol + sol_in)


def expected_sol_for_tokens(v_sol: int, v_token: int, tokens_in: int) -> int:
    """Pump.fun sell math. Returns raw lamports the swap will yield
    given current curve state, BEFORE slippage padding."""
    if v_sol <= 0 or v_token <= 0 or tokens_in <= 0:
        return 0
    return (v_sol * tokens_in) // (v_token + tokens_in)


def apply_buy_slippage(expected_tokens: int, slippage_bps: int) -> int:
    """Min tokens to accept = expected * (1 - slippage). Floor-div for
    safety — never round in a direction that lets the program reject."""
    if expected_tokens <= 0:
        return 0
    bps_floor = max(0, 10_000 - max(0, slippage_bps))
    return (expected_tokens * bps_floor) // 10_000


def apply_buy_max_sol(sol_amount_lamports: int, slippage_bps: int) -> int:
    """Max lamports willing to spend = sol_amount * (1 + slippage). Ceil
    so the program doesn't reject on rounding."""
    if sol_amount_lamports <= 0:
        return 0
    bps_ceil = 10_000 + max(0, slippage_bps)
    # Ceiling integer division: (a*b + d - 1) // d
    return (sol_amount_lamports * bps_ceil + 10_000 - 1) // 10_000


def apply_sell_min_sol(expected_sol: int, slippage_bps: int) -> int:
    """Min lamports to accept = expected_sol * (1 - slippage). Floor."""
    if expected_sol <= 0:
        return 0
    bps_floor = max(0, 10_000 - max(0, slippage_bps))
    return (expected_sol * bps_floor) // 10_000


# ── ComputeBudget instructions ──────────────────────────────────────────────
# Prepended to every swap tx so we (a) cap CU usage so the tx fits in a
# block, (b) tip the validator a per-CU priority fee. solders ships
# helpers for both.

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price  # noqa: E402


def compute_budget_instructions(cu_limit: int, cu_price_micro_lamports: int) -> list[Instruction]:
    """Returns [set_compute_unit_limit, set_compute_unit_price] in the
    order that needs to land in the tx (limit first, then price)."""
    return [
        set_compute_unit_limit(int(cu_limit)),
        set_compute_unit_price(int(cu_price_micro_lamports)),
    ]


# ── Full transaction assembly ───────────────────────────────────────────────
# Returns serialized v0 VersionedTransaction bytes WITHOUT a real signature
# — _sign_and_send in pumpportal_executor deserializes the bytes, signs with
# the operator's keypair, and re-serializes. Same shape as what PumpPortal's
# /api/trade-local returns.

from solders.hash import Hash  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.null_signer import NullSigner  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402


def _assemble_tx_bytes(
    payer:            Pubkey,
    instructions:     list[Instruction],
    recent_blockhash: Hash,
) -> bytes:
    """Build a v0 VersionedTransaction, placeholder-signed with NullSigner,
    serialized to bytes. Compatible with pumpportal_executor._sign_and_send
    which expects to be able to deserialize, re-sign, and submit."""
    msg = MessageV0.try_compile(
        payer=payer,
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=recent_blockhash,
    )
    # NullSigner produces a 64-byte zero signature — same as what an
    # unsigned tx looks like. _sign_and_send re-signs with the real key.
    tx = VersionedTransaction(msg, [NullSigner(payer)])
    return bytes(tx)


def build_buy_tx_bytes(
    *,
    user:                    Pubkey,
    mint:                    Pubkey,
    sol_amount_lamports:     int,
    v_sol_reserves:          int,
    v_token_reserves:        int,
    slippage_bps:            int,
    cu_limit:                int,
    cu_price_micro_lamports: int,
    recent_blockhash:        Hash,
) -> bytes:
    """End-to-end buy tx assembly. Takes curve reserves so the caller
    can either read them fresh from the PDA or use a cached snapshot.

    Returns serialized unsigned v0 tx bytes."""
    expected = expected_tokens_for_sol(v_sol_reserves, v_token_reserves, sol_amount_lamports)
    if expected <= 0:
        raise ValueError(
            f"buy math gave 0 tokens: v_sol={v_sol_reserves} v_token={v_token_reserves} "
            f"sol_in={sol_amount_lamports}",
        )
    min_tokens   = apply_buy_slippage(expected, slippage_bps)
    max_sol_cost = apply_buy_max_sol(sol_amount_lamports, slippage_bps)
    # Create the user's ATA for this mint if it doesn't already exist.
    # Idempotent — cheap no-op when it does. Without this every first-time
    # buy of a fresh pump.fun mint fails with Anchor error 3012
    # (AccountNotInitialized). Live observation 2026-05-17: ~30% of buys
    # were failing with Custom: 3012 because of this missing instruction.
    create_ata_ix = build_create_ata_idempotent_instruction(user, user, mint)
    buy_ix = build_buy_instruction(user, mint, min_tokens, max_sol_cost)
    return _assemble_tx_bytes(
        payer=user,
        instructions=(
            compute_budget_instructions(cu_limit, cu_price_micro_lamports)
            + [create_ata_ix, buy_ix]
        ),
        recent_blockhash=recent_blockhash,
    )


def build_sell_tx_bytes(
    *,
    user:                    Pubkey,
    mint:                    Pubkey,
    token_amount:            int,
    v_sol_reserves:          int,
    v_token_reserves:        int,
    slippage_bps:            int,
    cu_limit:                int,
    cu_price_micro_lamports: int,
    recent_blockhash:        Hash,
) -> bytes:
    """End-to-end sell tx assembly. Returns serialized unsigned v0 tx bytes."""
    if token_amount <= 0:
        raise ValueError(f"sell with non-positive token_amount: {token_amount}")
    expected_sol  = expected_sol_for_tokens(v_sol_reserves, v_token_reserves, token_amount)
    min_sol_output = apply_sell_min_sol(expected_sol, slippage_bps)
    sell_ix = build_sell_instruction(user, mint, token_amount, min_sol_output)
    return _assemble_tx_bytes(
        payer=user,
        instructions=compute_budget_instructions(cu_limit, cu_price_micro_lamports) + [sell_ix],
        recent_blockhash=recent_blockhash,
    )
