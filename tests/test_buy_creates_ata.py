"""Regression tests for the create-ATA-idempotent instruction in the
local buy tx builder.

Live observation 2026-05-17 (closed_trades.jsonl + pump_bot.log):
~30% of buys were failing with `{"InstructionError": [2, {"Custom": 3012}]}`
— Anchor error 3012 = AccountNotInitialized. Root cause: the very first
time the operator's wallet touches a given pump.fun mint, the wallet's
SPL Associated Token Account for that mint doesn't exist on chain. The
pump.fun buy instruction expects the ATA to exist (so it can credit
tokens to it) and aborts when it doesn't.

Fix: prepend `CreateAssociatedTokenAccountIdempotent` to every buy tx.
The idempotent variant is safe on every buy — if the ATA already exists
it's a ~1.5K-CU no-op; if not, it creates it (~10K CU) and the buy
proceeds in the same tx.

Pinned here:
  - Helper builds the correct ATA program ix (discriminator, accounts)
  - `build_buy_tx_bytes` emits the ATA-create BEFORE the pump.fun buy
  - Account ordering on the ATA-create ix matches the SPL spec
"""
from solders.hash import Hash
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from trader.local_tx_builder import (
    ATA_PROGRAM,
    PUMP_FUN_PROGRAM,
    SYSTEM_PROGRAM,
    TOKEN_PROGRAM,
    associated_token_address,
    build_buy_tx_bytes,
    build_create_ata_idempotent_instruction,
)

_USER = Pubkey.from_string("4tQ8PMNh3aaFs3hMXEpL3qWuM6N2eD8j5h5tKjkdEAEa")
_MINT = Pubkey.from_string("Coo7eB1ucT4MS9k1y3y4EZQU6FzbtJEABrEgQwxLpump")


class TestCreateAtaInstruction:
    def test_program_id_is_ata_program(self):
        ix = build_create_ata_idempotent_instruction(_USER, _USER, _MINT)
        assert ix.program_id == ATA_PROGRAM

    def test_discriminator_is_one_byte_0x01(self):
        """CreateIdempotent = discriminator byte 0x01 in the modern ATA
        program (v1.0.4+). Create (legacy, requires Rent sysvar) = 0x00.
        We MUST use idempotent so calling it on an already-existing ATA
        is a no-op rather than an error."""
        ix = build_create_ata_idempotent_instruction(_USER, _USER, _MINT)
        assert bytes(ix.data) == bytes([1]), (
            f"expected discriminator 0x01 (Idempotent), got {bytes(ix.data).hex()}"
        )

    def test_account_order_matches_spl_spec(self):
        """SPL ATA program account order for Create / CreateIdempotent:
            0. payer (writable, signer)
            1. associated_token_account (writable)
            2. wallet/owner (read-only, NOT signer)
            3. mint (read-only)
            4. system program
            5. token program
        Off-by-one here would make the create silently target the wrong
        account; pin it explicitly."""
        ix = build_create_ata_idempotent_instruction(_USER, _USER, _MINT)
        accts = ix.accounts
        assert len(accts) == 6
        expected_ata = associated_token_address(_USER, _MINT)
        assert accts[0].pubkey == _USER         and accts[0].is_signer   and accts[0].is_writable
        assert accts[1].pubkey == expected_ata  and not accts[1].is_signer and accts[1].is_writable
        assert accts[2].pubkey == _USER         and not accts[2].is_signer and not accts[2].is_writable
        assert accts[3].pubkey == _MINT         and not accts[3].is_signer and not accts[3].is_writable
        assert accts[4].pubkey == SYSTEM_PROGRAM
        assert accts[5].pubkey == TOKEN_PROGRAM


class TestBuyTxIncludesAtaCreate:
    """The assembled buy tx must contain a CreateIdempotent ATA ix
    ordered BEFORE the pump.fun buy ix. If this regresses, every
    first-time buy of a fresh mint fails with Anchor 3012 again."""

    def _build(self):
        # Modest realistic curve numbers; values don't matter for the
        # instruction-presence assertions below.
        return build_buy_tx_bytes(
            user=_USER,
            mint=_MINT,
            sol_amount_lamports=50_000_000,           # 0.05 SOL
            v_sol_reserves=30_000_000_000,            # 30 SOL virtual
            v_token_reserves=1_073_000_000_000_000,   # standard pf init
            slippage_bps=500,
            cu_limit=200_000,
            cu_price_micro_lamports=300_000,
            recent_blockhash=Hash.default(),
        )

    def test_ata_create_present_in_buy_tx(self):
        tx = VersionedTransaction.from_bytes(self._build())
        program_ids = [ix.program_id_index for ix in tx.message.instructions]
        program_keys = tx.message.account_keys
        program_pubkeys_used = [program_keys[i] for i in program_ids]
        assert ATA_PROGRAM in program_pubkeys_used, (
            "buy tx is missing the ATA-create instruction — every "
            "first-time mint buy will fail with Anchor 3012 again"
        )

    def test_ata_create_ordered_before_pumpfun_buy(self):
        """ATA must exist before the pump.fun ix runs. Otherwise the
        same-tx ordering doesn't help — the buy sees a missing ATA and
        aborts."""
        tx = VersionedTransaction.from_bytes(self._build())
        program_keys = tx.message.account_keys
        ata_idx = pump_idx = None
        for i, ix in enumerate(tx.message.instructions):
            prog = program_keys[ix.program_id_index]
            if prog == ATA_PROGRAM and ata_idx is None:
                ata_idx = i
            if prog == PUMP_FUN_PROGRAM and pump_idx is None:
                pump_idx = i
        assert ata_idx is not None and pump_idx is not None
        assert ata_idx < pump_idx, (
            f"ATA-create (index {ata_idx}) must come BEFORE the "
            f"pump.fun buy (index {pump_idx})"
        )
