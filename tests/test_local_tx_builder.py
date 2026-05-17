"""Tests for trader.local_tx_builder.

These pin every deterministic part: discriminators, PDA derivations,
ATA derivation, argument encoding, full instruction byte assembly.

What's NOT covered (and why):
  - Whether the constructed instruction is accepted by the pump.fun
    program. That needs a live chain, which the test environment can't
    reach. The PR description documents a one-shot validation
    procedure: build the same buy/sell via PumpPortal API and diff
    the bytes against this builder's output.
"""
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from trader.local_tx_builder import (
    ATA_PROGRAM,
    BUY_DISCRIMINATOR,
    FEE_RECIPIENT,
    PUMP_FUN_PROGRAM,
    RENT_SYSVAR,
    SELL_DISCRIMINATOR,
    SYSTEM_PROGRAM,
    TOKEN_PROGRAM,
    SwapAccounts,
    _anchor_discriminator,
    associated_token_address,
    bonding_curve_pda,
    build_buy_instruction,
    build_sell_instruction,
    event_authority_pda,
    global_pda,
    resolve_swap_accounts,
)

# ── Fixed test vectors ─────────────────────────────────────────────────────
# These keys are well-known Solana program/sysvar IDs or test wallets used
# across the Solana ecosystem — no real funds involved.

# A real-looking 32-byte pubkey for the test wallet
_USER  = Pubkey.from_string("4tQ8PMNh3aaFs3hMXEpL3qWuM6N2eD8j5h5tKjkdEAEa")
# A real-looking mint pubkey (not a real mint — just used for PDA tests)
_MINT  = Pubkey.from_string("Coo7eB1ucT4MS9k1y3y4EZQU6FzbtJEABrEgQwxLpump")


# ── Anchor discriminators ──────────────────────────────────────────────────

class TestAnchorDiscriminators:
    """Discriminator MUST be sha256('global:<ix_name>')[:8]. If pump.fun
    ever changes their instruction name (very unlikely on a deployed
    program), regenerate from the IDL."""

    def test_buy_discriminator_matches_anchor_convention(self):
        expected = hashlib.sha256(b"global:buy").digest()[:8]
        assert BUY_DISCRIMINATOR == expected
        # Pin the exact bytes so a refactor of _anchor_discriminator
        # can't silently change them.
        assert list(BUY_DISCRIMINATOR) == [102, 6, 61, 18, 1, 218, 235, 234]

    def test_sell_discriminator_matches_anchor_convention(self):
        expected = hashlib.sha256(b"global:sell").digest()[:8]
        assert SELL_DISCRIMINATOR == expected
        assert list(SELL_DISCRIMINATOR) == [51, 230, 133, 164, 1, 127, 131, 173]

    def test_buy_and_sell_discriminators_differ(self):
        assert BUY_DISCRIMINATOR != SELL_DISCRIMINATOR

    def test_helper_handles_arbitrary_names(self):
        # Sanity: the helper is generic, not hardcoded to buy/sell.
        d = _anchor_discriminator("initialize")
        assert len(d) == 8
        assert d == hashlib.sha256(b"global:initialize").digest()[:8]


# ── PDA derivations ────────────────────────────────────────────────────────

class TestPDAs:
    def test_bonding_curve_pda_is_deterministic(self):
        a = bonding_curve_pda(_MINT)
        b = bonding_curve_pda(_MINT)
        assert a == b
        # Different mint → different PDA.
        other = Pubkey.from_string("So11111111111111111111111111111111111111112")
        assert bonding_curve_pda(_MINT) != bonding_curve_pda(other)

    def test_bonding_curve_pda_matches_main_py_derivation(self):
        """main.py:_bonding_curve_price uses the exact same seed +
        program; if they diverge, on-chain price reads break."""
        # Re-derive exactly the way main.py does it.
        expected, _bump = Pubkey.find_program_address(
            [b"bonding-curve", bytes(_MINT)],
            PUMP_FUN_PROGRAM,
        )
        assert bonding_curve_pda(_MINT) == expected

    def test_global_pda_is_seed_global(self):
        expected, _ = Pubkey.find_program_address([b"global"], PUMP_FUN_PROGRAM)
        assert global_pda() == expected

    def test_event_authority_pda(self):
        expected, _ = Pubkey.find_program_address(
            [b"__event_authority"], PUMP_FUN_PROGRAM,
        )
        assert event_authority_pda() == expected


class TestAssociatedTokenAccount:
    def test_ata_matches_spl_derivation(self):
        # Re-derive the same way: PDA of [owner, token_program, mint]
        # against the ATA program.
        expected, _ = Pubkey.find_program_address(
            [bytes(_USER), bytes(TOKEN_PROGRAM), bytes(_MINT)],
            ATA_PROGRAM,
        )
        assert associated_token_address(_USER, _MINT) == expected

    def test_different_owners_different_atas(self):
        other = Pubkey.from_string("So11111111111111111111111111111111111111112")
        assert (
            associated_token_address(_USER, _MINT)
            != associated_token_address(other, _MINT)
        )


# ── Swap account resolution ────────────────────────────────────────────────

class TestResolveSwapAccounts:
    def test_resolved_accounts_have_expected_fields(self):
        accts = resolve_swap_accounts(_USER, _MINT)
        assert isinstance(accts, SwapAccounts)
        assert accts.user == _USER
        assert accts.mint == _MINT
        assert accts.global_pda == global_pda()
        assert accts.fee_recipient == FEE_RECIPIENT
        assert accts.bonding_curve == bonding_curve_pda(_MINT)
        assert accts.associated_bonding_curve == associated_token_address(
            accts.bonding_curve, _MINT,
        )
        assert accts.associated_user == associated_token_address(_USER, _MINT)
        assert accts.event_authority == event_authority_pda()


# ── Instruction encoding ───────────────────────────────────────────────────

class TestBuyInstruction:
    def test_buy_data_layout(self):
        """data = 8-byte discriminator || u64 token_amount LE || u64 max_sol_cost LE"""
        ix = build_buy_instruction(_USER, _MINT, token_amount=1_000_000, max_sol_cost=50_000_000)
        data = bytes(ix.data)
        assert len(data) == 8 + 8 + 8
        assert data[:8] == BUY_DISCRIMINATOR
        tok = struct.unpack_from("<Q", data, 8)[0]
        max_sol = struct.unpack_from("<Q", data, 16)[0]
        assert tok == 1_000_000
        assert max_sol == 50_000_000

    def test_buy_program_id_is_pump_fun(self):
        ix = build_buy_instruction(_USER, _MINT, 1, 1)
        assert ix.program_id == PUMP_FUN_PROGRAM

    def test_buy_account_order_matches_spec(self):
        """Pin the exact 12-account ordering. If pump.fun reorders these
        on a program upgrade, this test fails and forces an update."""
        ix = build_buy_instruction(_USER, _MINT, 1, 1)
        pubkeys = [a.pubkey for a in ix.accounts]
        assert pubkeys == [
            global_pda(),
            FEE_RECIPIENT,
            _MINT,
            bonding_curve_pda(_MINT),
            associated_token_address(bonding_curve_pda(_MINT), _MINT),
            associated_token_address(_USER, _MINT),
            _USER,
            SYSTEM_PROGRAM,
            TOKEN_PROGRAM,
            RENT_SYSVAR,
            event_authority_pda(),
            PUMP_FUN_PROGRAM,
        ]

    def test_buy_signer_and_writable_flags(self):
        ix = build_buy_instruction(_USER, _MINT, 1, 1)
        flags = [(a.is_signer, a.is_writable) for a in ix.accounts]
        # user is the only signer; fee_recipient + bonding_curve +
        # both ATAs + user are writable.
        expected = [
            (False, False),  # global
            (False, True),   # fee_recipient
            (False, False),  # mint
            (False, True),   # bonding_curve
            (False, True),   # associated_bonding_curve
            (False, True),   # associated_user
            (True,  True),   # user (signer)
            (False, False),  # system program
            (False, False),  # token program
            (False, False),  # rent
            (False, False),  # event_authority
            (False, False),  # program
        ]
        assert flags == expected

    def test_buy_rejects_negative_amounts(self):
        with pytest.raises(ValueError):
            build_buy_instruction(_USER, _MINT, -1, 0)
        with pytest.raises(ValueError):
            build_buy_instruction(_USER, _MINT, 0, -1)


class TestSellInstruction:
    def test_sell_data_layout(self):
        ix = build_sell_instruction(_USER, _MINT, token_amount=2_000_000, min_sol_output=100_000_000)
        data = bytes(ix.data)
        assert len(data) == 8 + 8 + 8
        assert data[:8] == SELL_DISCRIMINATOR
        tok = struct.unpack_from("<Q", data, 8)[0]
        min_sol = struct.unpack_from("<Q", data, 16)[0]
        assert tok == 2_000_000
        assert min_sol == 100_000_000

    def test_sell_uses_same_account_list_as_buy(self):
        """The pump.fun program uses the same account layout for buy
        and sell — both go through the same swap path."""
        buy  = build_buy_instruction(_USER, _MINT, 1, 1)
        sell = build_sell_instruction(_USER, _MINT, 1, 1)
        buy_keys  = [a.pubkey for a in buy.accounts]
        sell_keys = [a.pubkey for a in sell.accounts]
        assert buy_keys == sell_keys
        # Same writable/signer flags too.
        assert [(a.is_signer, a.is_writable) for a in buy.accounts] == \
               [(a.is_signer, a.is_writable) for a in sell.accounts]

    def test_sell_rejects_negative_amounts(self):
        with pytest.raises(ValueError):
            build_sell_instruction(_USER, _MINT, -1, 0)
        with pytest.raises(ValueError):
            build_sell_instruction(_USER, _MINT, 0, -1)


class TestGoldenBytes:
    """Lock in the exact byte output for a known (user, mint, amount)
    combination. If anything in the encoding chain changes, this fails
    loudly. Update by re-running with a real PumpPortal-API diff in hand."""

    def test_buy_golden_data(self):
        ix = build_buy_instruction(_USER, _MINT, token_amount=12345, max_sol_cost=67890)
        # 102, 6, 61, 18, 1, 218, 235, 234 | 12345 LE u64 | 67890 LE u64
        expected = (
            bytes([102, 6, 61, 18, 1, 218, 235, 234])
            + struct.pack("<Q", 12345)
            + struct.pack("<Q", 67890)
        )
        assert bytes(ix.data) == expected

    def test_sell_golden_data(self):
        ix = build_sell_instruction(_USER, _MINT, token_amount=99999, min_sol_output=11111)
        expected = (
            bytes([51, 230, 133, 164, 1, 127, 131, 173])
            + struct.pack("<Q", 99999)
            + struct.pack("<Q", 11111)
        )
        assert bytes(ix.data) == expected


# ── Bonding-curve math ─────────────────────────────────────────────────────

from trader.local_tx_builder import (  # noqa: E402
    apply_buy_max_sol,
    apply_buy_slippage,
    apply_sell_min_sol,
    build_buy_tx_bytes,
    build_sell_tx_bytes,
    compute_budget_instructions,
    expected_sol_for_tokens,
    expected_tokens_for_sol,
)


class TestBondingCurveMath:
    """Pump.fun uses constant-product (v_sol + sol_in)(v_token - tokens_out) =
    v_sol * v_token. Integer math, floor-div, matches the on-chain
    Anchor program."""

    def test_expected_tokens_for_sol_basic(self):
        # v_sol=30 SOL, v_token=1B raw, sol_in=0.1 SOL
        # tokens_out = 1e9 * 1e8 / (3e10 + 1e8) = ~3,322,259
        v_sol   = 30 * 10**9      # 30 SOL in lamports
        v_token = 1_000_000_000   # 1B raw
        sol_in  = 100_000_000     # 0.1 SOL
        out = expected_tokens_for_sol(v_sol, v_token, sol_in)
        # Floor-div formula: (v_token * sol_in) // (v_sol + sol_in)
        assert out == (v_token * sol_in) // (v_sol + sol_in)
        assert out > 0

    def test_expected_tokens_returns_zero_on_bad_inputs(self):
        assert expected_tokens_for_sol(0, 100, 100) == 0
        assert expected_tokens_for_sol(100, 0, 100) == 0
        assert expected_tokens_for_sol(100, 100, 0) == 0

    def test_expected_sol_for_tokens_symmetric(self):
        v_sol   = 30 * 10**9
        v_token = 1_000_000_000
        out = expected_sol_for_tokens(v_sol, v_token, 100_000)
        assert out == (v_sol * 100_000) // (v_token + 100_000)


class TestSlippageMath:
    def test_buy_slippage_floor(self):
        # 1000 expected tokens, 15% slippage → min 850 accepted
        assert apply_buy_slippage(1000, 1500) == 850
        # 0% slippage = pass-through
        assert apply_buy_slippage(1000, 0) == 1000

    def test_buy_max_sol_ceiling(self):
        # 1000 lamports, 15% slippage → max 1150
        assert apply_buy_max_sol(1000, 1500) == 1150
        # 0% slippage = pass-through (with ceiling round)
        assert apply_buy_max_sol(1000, 0) == 1000

    def test_sell_min_sol_floor(self):
        assert apply_sell_min_sol(1000, 1500) == 850
        assert apply_sell_min_sol(1000, 0) == 1000

    def test_slippage_zero_edge_cases(self):
        assert apply_buy_slippage(0, 1500) == 0
        assert apply_buy_max_sol(0, 1500) == 0
        assert apply_sell_min_sol(0, 1500) == 0


class TestComputeBudgetInstructions:
    def test_returns_two_instructions_in_order(self):
        ixs = compute_budget_instructions(cu_limit=200_000, cu_price_micro_lamports=300_000)
        assert len(ixs) == 2
        # Both target the ComputeBudget program. solders' helpers set this
        # internally; just sanity-check we got two distinct ix.
        from solders.pubkey import Pubkey
        cb_program = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
        assert ixs[0].program_id == cb_program
        assert ixs[1].program_id == cb_program
        assert ixs[0].data != ixs[1].data


# ── Full tx assembly ───────────────────────────────────────────────────────

class TestBuildBuyTxBytes:
    def test_returns_non_empty_bytes(self):
        from solders.hash import Hash
        b = build_buy_tx_bytes(
            user                    = _USER,
            mint                    = _MINT,
            sol_amount_lamports     = 10_000_000,        # 0.01 SOL
            v_sol_reserves          = 30 * 10**9,
            v_token_reserves        = 1_000_000_000_000,
            slippage_bps            = 1500,
            cu_limit                = 200_000,
            cu_price_micro_lamports = 300_000,
            recent_blockhash        = Hash.default(),
        )
        assert isinstance(b, bytes)
        assert len(b) > 100   # serialized tx is non-trivial

    def test_round_trips_through_versioned_tx(self):
        """Bytes returned must deserialize back to a VersionedTransaction
        — pumpportal_executor._sign_and_send needs this so it can re-sign."""
        from solders.hash import Hash
        from solders.transaction import VersionedTransaction
        b = build_buy_tx_bytes(
            user=_USER, mint=_MINT,
            sol_amount_lamports=10_000_000,
            v_sol_reserves=30 * 10**9, v_token_reserves=1_000_000_000_000,
            slippage_bps=1500, cu_limit=200_000, cu_price_micro_lamports=300_000,
            recent_blockhash=Hash.default(),
        )
        tx = VersionedTransaction.from_bytes(b)
        # 4 instructions: CU limit, CU price, ATA create-idempotent,
        # pump.fun buy. The ATA create was added 2026-05-17 to fix
        # ~30% of buys failing with Anchor 3012 (AccountNotInitialized).
        assert len(tx.message.instructions) == 4

    def test_raises_on_zero_expected_tokens(self):
        from solders.hash import Hash
        with pytest.raises(ValueError):
            build_buy_tx_bytes(
                user=_USER, mint=_MINT,
                sol_amount_lamports=0,                          # zero in
                v_sol_reserves=30 * 10**9, v_token_reserves=1_000_000_000_000,
                slippage_bps=1500, cu_limit=200_000, cu_price_micro_lamports=300_000,
                recent_blockhash=Hash.default(),
            )


class TestBuildSellTxBytes:
    def test_returns_non_empty_bytes(self):
        from solders.hash import Hash
        b = build_sell_tx_bytes(
            user=_USER, mint=_MINT,
            token_amount=1_000_000,
            v_sol_reserves=30 * 10**9, v_token_reserves=1_000_000_000_000,
            slippage_bps=1500, cu_limit=200_000, cu_price_micro_lamports=300_000,
            recent_blockhash=Hash.default(),
        )
        assert isinstance(b, bytes)
        assert len(b) > 100

    def test_raises_on_non_positive_amount(self):
        from solders.hash import Hash
        with pytest.raises(ValueError):
            build_sell_tx_bytes(
                user=_USER, mint=_MINT,
                token_amount=0,
                v_sol_reserves=30 * 10**9, v_token_reserves=1_000_000_000_000,
                slippage_bps=1500, cu_limit=200_000, cu_price_micro_lamports=300_000,
                recent_blockhash=Hash.default(),
            )

    def test_sell_tx_includes_ata_create(self):
        """Defensive: sell path must also prepend CreateIdempotent ATA ix.
        Same instruction count as buy (4): CU limit, CU price, ATA-create,
        pump.fun sell. Protects against recovery flows where the operator's
        ATA was closed between buy and sell."""
        from solders.hash import Hash
        from solders.transaction import VersionedTransaction
        b = build_sell_tx_bytes(
            user=_USER, mint=_MINT,
            token_amount=1_000_000,
            v_sol_reserves=30 * 10**9, v_token_reserves=1_000_000_000_000,
            slippage_bps=1500, cu_limit=200_000, cu_price_micro_lamports=300_000,
            recent_blockhash=Hash.default(),
        )
        tx = VersionedTransaction.from_bytes(b)
        assert len(tx.message.instructions) == 4, (
            "sell tx should have 4 ixs: CU limit, CU price, ATA create, "
            "pump.fun sell"
        )
        program_keys = tx.message.account_keys
        used = [program_keys[ix.program_id_index] for ix in tx.message.instructions]
        assert ATA_PROGRAM in used, "sell tx missing the ATA-create ix"


class TestLocalTxEligibility:
    """The fallback gate that decides whether the local builder can
    handle a given (action, amount) shape. Conservative — anything
    unfamiliar falls through to the PumpPortal HTTP path."""

    def test_off_by_default(self, monkeypatch):
        # config.USE_LOCAL_TX_BUILD is loaded at module import; eligibility
        # function reads it lazily from the imported namespace. Patch the
        # value in pumpportal_executor.
        import trader.pumpportal_executor as pp
        monkeypatch.setattr(pp, "USE_LOCAL_TX_BUILD", False)
        assert pp.PumpPortalExecutor._local_tx_eligible("buy", 0.1, True) is False

    def test_enabled_buy(self, monkeypatch):
        import trader.pumpportal_executor as pp
        monkeypatch.setattr(pp, "USE_LOCAL_TX_BUILD", True)
        assert pp.PumpPortalExecutor._local_tx_eligible("buy", 0.1, True) is True

    def test_enabled_sell_integer(self, monkeypatch):
        import trader.pumpportal_executor as pp
        monkeypatch.setattr(pp, "USE_LOCAL_TX_BUILD", True)
        assert pp.PumpPortalExecutor._local_tx_eligible("sell", 1_000_000, False) is True

    def test_enabled_sell_percent_string_falls_back(self, monkeypatch):
        """Risk_manager passes '100%' for force-sells. Local path can't
        resolve that → must say not eligible so the HTTP path handles it."""
        import trader.pumpportal_executor as pp
        monkeypatch.setattr(pp, "USE_LOCAL_TX_BUILD", True)
        assert pp.PumpPortalExecutor._local_tx_eligible("sell", "100%", False) is False

    def test_buy_with_zero_amount_not_eligible(self, monkeypatch):
        import trader.pumpportal_executor as pp
        monkeypatch.setattr(pp, "USE_LOCAL_TX_BUILD", True)
        assert pp.PumpPortalExecutor._local_tx_eligible("buy", 0, True) is False
