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
