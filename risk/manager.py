"""
risk/manager.py
Full risk management layer — upgraded with open-source research:

  - Stop loss tightened to -10% (research: prevents capital bleed)
  - Trailing stop at -10% from peak
  - Take-profits at +25% (sell 50%) and +50% (sell 75%) — TreeCityWes model
  - Time exit reduced to 30 min
  - Creator tracker integration: records trade outcomes for leaderboard
"""

import asyncio
import datetime
import json
import os
import time
from dataclasses import dataclass, field

from loguru import logger

from analyzer.rug_memory import RUG_PNL_THRESHOLD, rug_memory
from config import (
    ADAPTIVE_COLD_MULT,
    ADAPTIVE_COLD_WR,
    ADAPTIVE_HARD_CAP_MULT,
    ADAPTIVE_HOT_MULT,
    ADAPTIVE_HOT_WR,
    ADAPTIVE_LOOKBACK,
    ADAPTIVE_SIZING_ENABLED,
    DAILY_LOSS_LIMIT_PCT,
    DAILY_PROFIT_LOCK_PCT,
    EARLY_RUG_PCT,
    EARLY_RUG_WINDOW_SEC,
    EMERGENCY_STOP_DRAWDOWN_PCT,
    LOSS_STREAK_LIMIT,
    LOSS_STREAK_PAUSE_MIN,
    MAX_OPEN_POSITIONS,
    MAX_POSITION_PCT,
    MAX_SOL_PER_TRADE,
    MAX_SYMBOL_LIFETIME_DEPLOY_PCT,
    MAX_TOTAL_EXPOSURE_SOL,
    MOMENTUM_STALL_ENABLED,
    MOMENTUM_STALL_MIN_PROFIT,
    MOMENTUM_STALL_PCT_BAND,
    MOMENTUM_STALL_WINDOW_SEC,
    NO_MOVEMENT_BAND_PCT,
    NO_MOVEMENT_EXIT_SECONDS,
    PANIC_STOP_LOSS_PCT,
    STOP_LOSS_PCT,
    TAKE_PROFIT_LEVELS,
    TIME_EXIT_MINUTES,
    TRAILING_STOP_ENABLED,
    TRAILING_STOP_MIN_PROFIT,
    TRAILING_STOP_MOONSHOT_PCT,
    TRAILING_STOP_MOONSHOT_TRIGGER,
    TRAILING_STOP_PCT,
)
from detector.creator_tracker import creator_tracker
from detector.wallet_intel import wallet_intel
from logger.telegram_alerts import send_alert
from logger.trade_db import get_trade_db

CLOSED_TRADES_FILE   = "logs/closed_trades.jsonl"
RISK_STATE_FILE      = "logs/risk_state.json"
SYMBOL_DEPLOYED_FILE = "logs/symbol_deployed.json"
OPEN_POSITIONS_FILE  = "logs/open_positions.json"

# Force-sell escalation. If a position fails to close after this many
# attempts the bot logs CRITICAL and drops it from self.positions so the
# emergency_force_sell loop isn't permanently stuck retrying the same
# zombie mint. Tokens may still be sitting in the wallet — recover via
# dump_orphans.py.
MAX_FORCE_SELL_ATTEMPTS = 10

# Held-position market-value calc treats a position as worth 0 if its
# current_price hasn't been refreshed within this many seconds. Without
# this guard a stale price (price_monitor lag) inflates equity during
# rapid drops and the emergency drawdown trigger fires late.
STALE_PRICE_SEC = 10.0


@dataclass
class Position:
    """Represents an open trade position."""
    mint:            str
    symbol:          str
    creator:         str
    entry_price_sol: float
    entry_time:      float
    sol_invested:    float
    tokens_held:     int
    current_price:   float = 0
    highest_price:   float = 0
    tp_levels_hit:   list  = field(default_factory=list)
    score:           int   = 0
    # Pre-rug-penalty score — needed so rug_memory record/lookup use the SAME
    # bucket key. Without this the feature was silently broken: records went
    # in at post-penalty bins, lookups fired at pre-penalty bins, no matches.
    raw_score:       int   = 0
    # Snapshot of features at entry — needed at close to feed rug_memory
    # without re-fetching state we already had at open.
    init_buy_sol:      float = 0
    bonding_curve_pct: float = 0
    # Pre-built sell-100% tx (bytes) populated async right after buy. Lets us
    # skip the PumpPortal _build_tx call (~200-500ms) on emergency exits like
    # early-rug and stop-loss. Solana blockhashes expire after ~150 slots
    # (~60s), so the prebuilt is only valid for fast exits — past that we
    # fall back to building fresh.
    prebuilt_sell_tx:    bytes | None = None
    prebuilt_sell_ts:    float = 0
    # Rolling (timestamp, pnl_pct) tuples for momentum-stall detection.
    price_history:   list  = field(default_factory=list)
    # Last time current_price was refreshed. Used by _check_emergency_stop
    # to ignore stale prices in the held-value calc so the drawdown
    # trigger isn't fooled by lagged price ticks during a fast dump.
    price_updated_at:    float = 0
    # Count of force-sell attempts on this mint. Reset to 0 on every
    # successful state change. After MAX_FORCE_SELL_ATTEMPTS the position
    # is logged CRITICAL and dropped — see _force_sell.
    force_sell_attempts: int   = 0

    @property
    def pnl_pct(self) -> float:
        if self.entry_price_sol == 0:
            return 0
        return ((self.current_price - self.entry_price_sol) / self.entry_price_sol) * 100

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60


class RiskManager:
    def __init__(self, wallet, executor):
        self.wallet                  = wallet
        self.executor                = executor
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[dict]      = []
        self.starting_sol_balance    = 0
        self.running                 = False
        self.emergency_stop_active   = False
        # Separate flag: only the auto-drawdown trigger sets this to also
        # force-sell. The manual dashboard button sets emergency_stop_active
        # alone — buys are blocked but open positions exit naturally on TP/SL.
        self.emergency_force_sell    = False

        # ── Tier 2 circuit breakers ─────────────────────────────────────────
        self.consecutive_losses     = 0
        self.loss_streak_pause_until = 0   # epoch sec
        self.day_baseline_balance   = 0    # captured at UTC midnight
        self.day_baseline_date      = None
        self.day_paused             = False
        self.pause_reason           = ""

        # ── Per-symbol lifetime deploy tracker (concentration audit fix) ─────
        # Sum of SOL ever deployed into each symbol, capped at
        # MAX_SYMBOL_LIFETIME_DEPLOY_PCT * starting_sol_balance. Persisted to
        # logs/symbol_deployed.json so it survives restarts; without that the
        # cap would reset every time the bot is restarted and the
        # robustness check would be useless.
        self._symbol_deployed: dict[str, float] = {}
        self._load_symbol_deployed()

    async def initialize(self):
        os.makedirs("logs", exist_ok=True)

        # Load historical closed trades from JSONL so the dashboard doesn't
        # forget what's happened across bot restarts.
        self._load_closed_trades()

        # Restore any open positions that were in-flight when the bot last
        # exited. Without this a crash silently loses every open position
        # — SPL tokens stay in the wallet but the bot can't TP/stop them.
        self._load_open_positions()

        # Restore the ORIGINAL starting balance — the very first balance
        # we ever recorded on this bot install. Without this, every restart
        # would zero out the cumulative PnL display.
        current_balance = await self.wallet.get_sol_balance()
        self.starting_sol_balance = self._load_or_seed_starting_balance(current_balance)

        logger.info(
            f"Risk manager initialized | "
            f"Starting (orig): {self.starting_sol_balance:.4f} SOL | "
            f"Current: {current_balance:.4f} SOL | "
            f"Loaded {len(self.closed_trades)} historical trades | "
            f"Stop loss: -{STOP_LOSS_PCT}% | "
            f"Time exit: {TIME_EXIT_MINUTES}min"
        )

    # ── Persistence helpers ───────────────────────────────────────────────────
    def _load_closed_trades(self):
        """Restore closed_trades for the TRADES tab. Prefers the sqlite db
        (logs/trades.db); falls back to the legacy JSONL when the db is
        empty or missing — keeps the bot bootable on a fresh checkout
        before the migrator has been run."""
        try:
            db_rows = get_trade_db().load_all()
        except Exception as e:
            logger.warning(f"[RISK] trade db read failed, falling back to jsonl: {e}")
            db_rows = []

        if db_rows:
            self.closed_trades.extend(db_rows)
            return

        if not os.path.exists(CLOSED_TRADES_FILE):
            return
        try:
            with open(CLOSED_TRADES_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.closed_trades.append(json.loads(line))
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[RISK] Could not load closed trades: {e}")

    def _append_closed_trade(self, record: dict):
        """Dual-write: JSONL (legacy, append-only, crash-proven) + sqlite.
        JSONL stays authoritative during the transition — losing a db
        write is recoverable from the JSONL via the migrator. The
        reverse is not true, so JSONL writes first."""
        try:
            with open(CLOSED_TRADES_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.warning(f"[RISK] JSONL trade append failed: {e}")
        try:
            get_trade_db().insert(record)
        except Exception as e:
            logger.warning(f"[RISK] sqlite trade insert failed: {e}")

    def _load_symbol_deployed(self):
        """Restore per-symbol deploy totals so the lifetime cap survives
        restarts. Without this every restart would zero the counter and the
        robustness guard wouldn't actually constrain anything."""
        try:
            if os.path.exists(SYMBOL_DEPLOYED_FILE):
                with open(SYMBOL_DEPLOYED_FILE, encoding="utf-8") as f:
                    self._symbol_deployed = {k: float(v) for k, v in json.load(f).items()}
        except Exception as e:
            logger.warning(f"[RISK] Could not load symbol_deployed: {e}")
            self._symbol_deployed = {}

    def _save_symbol_deployed(self):
        try:
            tmp = SYMBOL_DEPLOYED_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._symbol_deployed, f)
            os.replace(tmp, SYMBOL_DEPLOYED_FILE)
        except Exception as e:
            logger.debug(f"[RISK] symbol_deployed save err: {e}")

    def _symbol_cap_sol(self) -> float:
        """Hard cap = MAX_SYMBOL_LIFETIME_DEPLOY_PCT% of original starting capital.
        Kept off current balance on purpose — a strategy that grows the
        bankroll shouldn't be allowed to grow its single-ticker exposure
        proportionally; the audit specifically caught that concentration is
        the killer, not absolute deploy size."""
        return self.starting_sol_balance * (MAX_SYMBOL_LIFETIME_DEPLOY_PCT / 100.0)

    def _would_breach_symbol_cap(self, symbol: str, sol_amount: float) -> bool:
        if MAX_SYMBOL_LIFETIME_DEPLOY_PCT >= 999:
            return False
        cap = self._symbol_cap_sol()
        if cap <= 0:
            return False
        already = self._symbol_deployed.get(symbol, 0.0)
        return (already + sol_amount) > cap

    def _load_or_seed_starting_balance(self, current_balance: float) -> float:
        """
        First time we run, persist the current balance as the original
        starting capital. Every subsequent run, load that same value so PnL
        is measured against the bot's original seed, not whatever's left.

        Also loads emergency flags so a process restart doesn't wipe an
        active emergency-stop state. Without this, the 40% drawdown
        trigger reset to inactive on every restart and the bot resumed
        trading at the depleted balance — the single biggest factor in
        the −99.85% live wipeout.
        """
        try:
            if os.path.exists(RISK_STATE_FILE):
                with open(RISK_STATE_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                v = float(data.get("original_starting_sol", current_balance))
                # Rehydrate emergency state. New keys; older files silently
                # default to False (safe — at worst we miss one trigger
                # and the drawdown check will re-arm on the next tick).
                self.emergency_stop_active = bool(data.get("emergency_stop_active", False))
                self.emergency_force_sell  = bool(data.get("emergency_force_sell",  False))
                if self.emergency_stop_active:
                    logger.warning(
                        "[RISK] Resuming with persisted emergency_stop_active=True. "
                        "Clear via the dashboard 'Resume Trading' button if intentional."
                    )
                if v > 0:
                    return v
        except Exception as e:
            logger.warning(f"[RISK] state load error ({e}); seeding fresh")
        # Seed it
        try:
            self._save_risk_state(starting_sol=current_balance)
        except Exception as e:
            logger.warning(f"[RISK] Could not seed starting balance: {e}")
        return current_balance

    # ── Open-position persistence ────────────────────────────────────────
    # Without this a crash mid-run silently loses every open position —
    # SPL tokens remain in the wallet but the bot doesn't know they're
    # there, so no force-sell, no TP, no stop. dump_orphans.py was the
    # documented manual recovery, which is too slow during a fast dump.
    def _save_open_positions(self) -> None:
        """Atomic JSON dump of every currently-open position. Called
        from open_position, close_position, _record_partial_close, and
        _force_sell so disk state never drifts more than one mutation.
        prebuilt_sell_tx (bytes) is intentionally excluded — it's
        cheap to rebuild and bytes-in-JSON would need encoding."""
        rows = []
        for mint, pos in self.positions.items():
            rows.append({
                "mint":              mint,
                "symbol":            pos.symbol,
                "creator":           pos.creator,
                "entry_price_sol":   pos.entry_price_sol,
                "entry_time":        pos.entry_time,
                "sol_invested":      pos.sol_invested,
                "tokens_held":       pos.tokens_held,
                "current_price":     pos.current_price,
                "highest_price":     pos.highest_price,
                "tp_levels_hit":     list(pos.tp_levels_hit),
                "score":             pos.score,
                "raw_score":         pos.raw_score,
                "init_buy_sol":      pos.init_buy_sol,
                "bonding_curve_pct": pos.bonding_curve_pct,
                "price_updated_at":  pos.price_updated_at,
                "force_sell_attempts": pos.force_sell_attempts,
            })
        tmp = OPEN_POSITIONS_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"positions": rows, "saved_at": time.time()}, f, indent=2)
            os.replace(tmp, OPEN_POSITIONS_FILE)
        except Exception as e:
            logger.warning(f"[RISK] open-positions save failed: {e}")

    def _load_open_positions(self) -> None:
        """Restore open positions from disk on startup. Reconciliation
        against on-chain ATA balances is deferred — at worst the bot
        believes it owns tokens it sold while down, and will get a
        clean error from PumpPortal on the next force-sell attempt."""
        if not os.path.exists(OPEN_POSITIONS_FILE):
            return
        try:
            with open(OPEN_POSITIONS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"[RISK] open-positions load failed: {e}")
            return
        rows = data.get("positions", []) if isinstance(data, dict) else []
        restored = 0
        for row in rows:
            try:
                mint = row["mint"]
                self.positions[mint] = Position(
                    mint              = mint,
                    symbol            = row.get("symbol", "???"),
                    creator           = row.get("creator", ""),
                    entry_price_sol   = float(row.get("entry_price_sol", 0)),
                    entry_time        = float(row.get("entry_time", time.time())),
                    sol_invested      = float(row.get("sol_invested", 0)),
                    tokens_held       = int(row.get("tokens_held", 0)),
                    current_price     = float(row.get("current_price", 0)),
                    highest_price     = float(row.get("highest_price", 0)),
                    tp_levels_hit     = list(row.get("tp_levels_hit", [])),
                    score             = int(row.get("score", 0)),
                    raw_score         = int(row.get("raw_score", 0)),
                    init_buy_sol      = float(row.get("init_buy_sol", 0)),
                    bonding_curve_pct = float(row.get("bonding_curve_pct", 0)),
                    price_updated_at  = float(row.get("price_updated_at", 0)),
                    force_sell_attempts = int(row.get("force_sell_attempts", 0)),
                )
                restored += 1
            except Exception as e:
                logger.warning(f"[RISK] skipping malformed position row: {e}")
        if restored:
            logger.warning(
                f"[RISK] Restored {restored} open position(s) from disk. "
                "Verify wallet ATA balances match (dump_orphans.py)."
            )

    def _save_risk_state(self, starting_sol: float | None = None) -> None:
        """Atomic write of the full risk-state snapshot.

        Saves: original starting balance, emergency flags. Called on every
        emergency-flag transition so a crash mid-emergency doesn't let
        the bot resume trading on restart.
        """
        payload = {
            "original_starting_sol": starting_sol if starting_sol is not None
                                     else float(self.starting_sol_balance or 0),
            "emergency_stop_active": bool(self.emergency_stop_active),
            "emergency_force_sell":  bool(self.emergency_force_sell),
            "saved_at":              time.time(),
        }
        tmp = RISK_STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, RISK_STATE_FILE)
        except Exception as e:
            logger.warning(f"[RISK] state save failed: {e}")

    # ── Tier 2: pause guards ──────────────────────────────────────────────────
    async def _is_paused(self) -> bool:
        """Return True if any circuit breaker is currently active."""
        now = time.time()

        # Loss-streak cooldown
        if now < self.loss_streak_pause_until:
            mins = (self.loss_streak_pause_until - now) / 60
            self.pause_reason = f"loss_streak_pause_{mins:.0f}min"
            return True

        # Daily baseline reset on UTC date change. Baseline is total EQUITY
        # (liquid SOL + market value of any positions held over midnight) so
        # we can compare like-to-like to current equity below.
        #
        # IMPORTANT: don't reset if we're underwater against the prior
        # baseline. Without this guard, a drawdown that straddles UTC
        # midnight silently rebases the daily-loss circuit breaker to the
        # post-crash equity — the bot gets a clean slate to lose another
        # DAILY_LOSS_LIMIT_PCT% in the new day. Live wipeouts confirmed
        # this was happening. Keep the old baseline until equity recovers
        # above 80% of it.
        today = datetime.datetime.now(datetime.UTC).date()
        if self.day_baseline_date != today:
            sol = await self.wallet.get_sol_balance()
            held = sum(p.current_price * p.tokens_held for p in self.positions.values())
            new_baseline = sol + held
            if self.day_baseline_balance > 0 and new_baseline < self.day_baseline_balance * 0.80:
                logger.warning(
                    f"[RISK] UTC midnight rolled but equity {new_baseline:.4f} is "
                    f"<80% of prior baseline {self.day_baseline_balance:.4f}; "
                    "preserving baseline so the daily-loss breaker stays armed."
                )
                # keep old day_baseline_balance; still bump the date so
                # we don't re-evaluate every loop iteration.
                self.day_baseline_date = today
            else:
                self.day_baseline_balance = new_baseline
                self.day_baseline_date    = today
                self.day_paused           = False

        # Daily PnL bands — measure on total equity, not liquid SOL alone.
        # Otherwise opening a position registers as an immediate "loss" because
        # the SOL has left the wallet but the position market value isn't counted.
        if self.day_baseline_balance > 0:
            sol = await self.wallet.get_sol_balance()
            held = sum(p.current_price * p.tokens_held for p in self.positions.values())
            current_equity = sol + held
            day_pnl_pct = ((current_equity - self.day_baseline_balance) / self.day_baseline_balance) * 100
            if day_pnl_pct <= -DAILY_LOSS_LIMIT_PCT:
                self.day_paused = True
                self.pause_reason = f"daily_loss_{day_pnl_pct:.1f}pct"
                return True
            if day_pnl_pct >= DAILY_PROFIT_LOCK_PCT:
                self.day_paused = True
                self.pause_reason = f"daily_profit_lock_{day_pnl_pct:.1f}pct"
                return True

        self.pause_reason = ""
        return False

    # ── Position sizing ───────────────────────────────────────────────────────
    async def calculate_position_size(
        self, score: int, symbol: str | None = None,
    ) -> tuple[float, str]:
        """Return (size_sol, reject_reason).

        On success, reject_reason is "". On rejection, size is 0 and the
        reason is a stable token the dashboard can group on:
          emergency_stop, paused_<sub>, max_positions, symbol_cap,
          max_exposure, size_below_min.
        """
        if self.emergency_stop_active:
            logger.warning("Emergency stop active — no new positions")
            return 0.0, "emergency_stop"

        if await self._is_paused():
            logger.warning(f"Trading paused: {self.pause_reason}")
            # pause_reason already disambiguates loss_streak / daily_loss /
            # daily_profit_lock — prefix it so the dashboard can split.
            return 0.0, f"paused_{self.pause_reason}" if self.pause_reason else "paused"

        if len(self.positions) >= MAX_OPEN_POSITIONS:
            logger.warning(f"Max positions reached ({MAX_OPEN_POSITIONS})")
            return 0.0, "max_positions"

        # Per-symbol lifetime cap (concentration audit fix). Optional symbol
        # arg keeps backward-compat — callers that don't pass it skip the check.
        if symbol and self._would_breach_symbol_cap(symbol, 0.0):
            already = self._symbol_deployed.get(symbol, 0)
            logger.warning(
                f"[SYMBOL CAP] {symbol} | lifetime deploy {already:.4f} SOL "
                f">= cap {self._symbol_cap_sol():.4f} ({MAX_SYMBOL_LIFETIME_DEPLOY_PCT}% of start) — no new buy"
            )
            return 0.0, "symbol_cap"

        total_exposure = sum(p.sol_invested for p in self.positions.values())
        if total_exposure >= MAX_TOTAL_EXPOSURE_SOL:
            logger.warning(f"Max exposure reached ({total_exposure:.3f} SOL)")
            return 0.0, "max_exposure"

        sol_balance = await self.wallet.get_sol_balance()
        base_size   = min(MAX_SOL_PER_TRADE, sol_balance * MAX_POSITION_PCT)

        # Adaptive Kelly-lite: size based on recent win rate, not score.
        # Data showed scores above 42 don't differentiate winners, so score
        # multiplier is mostly noise. Recent performance is a stronger signal.
        multiplier = 1.0
        adaptive_note = ""
        if ADAPTIVE_SIZING_ENABLED and len(self.closed_trades) >= 5:
            recent = self.closed_trades[-ADAPTIVE_LOOKBACK:]
            wins   = sum(1 for t in recent if t.get("pnl_sol", 0) > 0)
            wr     = wins / len(recent)
            if   wr >= ADAPTIVE_HOT_WR:  multiplier = ADAPTIVE_HOT_MULT;  adaptive_note = f"HOT(wr={wr:.0%})"
            elif wr <= ADAPTIVE_COLD_WR: multiplier = ADAPTIVE_COLD_MULT; adaptive_note = f"COLD(wr={wr:.0%})"
            else:                         multiplier = 1.0;                adaptive_note = f"NORMAL(wr={wr:.0%})"

        size = min(base_size * multiplier, MAX_SOL_PER_TRADE * ADAPTIVE_HARD_CAP_MULT)

        # Don't exceed remaining capacity
        remaining = MAX_TOTAL_EXPOSURE_SOL - total_exposure
        size      = min(size, remaining)

        # Clip to remaining per-symbol cap (concentration audit fix).
        if symbol and MAX_SYMBOL_LIFETIME_DEPLOY_PCT < 999:
            cap = self._symbol_cap_sol()
            sym_remaining = max(0.0, cap - self._symbol_deployed.get(symbol, 0.0))
            if sym_remaining < size:
                size = sym_remaining

        # Minimum viable trade — covers fees + slippage with room to profit.
        min_viable = min(MAX_SOL_PER_TRADE / 2, 0.003)
        if size < min_viable:
            return 0.0, "size_below_min"

        logger.info(
            f"Position size: {size:.4f} SOL "
            f"(score={score}, mult={multiplier:.2f} {adaptive_note}, balance={sol_balance:.4f})"
        )
        return round(size, 4), ""

    # ── Open position ─────────────────────────────────────────────────────────
    def open_position(self, token: dict, trade_result: dict):
        mint   = token["mint"]
        symbol = token.get("symbol", "???")

        tokens_received = trade_result.get("tokens_expected", 0)
        if tokens_received <= 0:
            logger.warning(f"[POSITION] {symbol} — tokens_expected=0, skipping")
            return

        sol_spent    = trade_result.get("sol_spent", 0)
        entry_price  = sol_spent / tokens_received if tokens_received > 0 else 0
        creator      = token.get("creator", "")

        pos = Position(
            mint              = mint,
            symbol            = symbol,
            creator           = creator,
            entry_price_sol   = entry_price,
            entry_time        = time.time(),
            sol_invested      = sol_spent,
            tokens_held       = tokens_received,
            current_price     = entry_price,
            highest_price     = entry_price,
            score             = token.get("score", 0),
            raw_score         = token.get("raw_score", token.get("score", 0)),
            init_buy_sol      = float(token.get("initial_buy_sol", 0) or 0),
            bonding_curve_pct = float(token.get("bonding_curve_pct", 0) or 0),
        )

        self.positions[mint] = pos
        self._save_open_positions()

        # Record lifetime deploy under this symbol for the concentration cap.
        # Symbol-keyed, not mint-keyed: a creator relaunching the same name
        # after a rug counts as the same bucket.
        if symbol and sol_spent > 0:
            self._symbol_deployed[symbol] = self._symbol_deployed.get(symbol, 0.0) + sol_spent
            self._save_symbol_deployed()

        logger.success(
            f"[POSITION OPENED] {symbol} | {sol_spent:.4f} SOL | "
            f"entry={entry_price:.10f} | score={pos.score}"
        )

    # ── Close position ────────────────────────────────────────────────────────
    def _record_partial_close(
        self,
        mint: str,
        pos: "Position",
        sell_fraction: float,
        sell_result: dict,
        level_id: str,
    ) -> None:
        """Write a take-profit partial sell to closed_trades + trade_db.

        Treats each TP leg as its own trade record so the auto_tuner,
        rug_memory, and dashboard reflect the SOL the wallet actually
        received — not just the final exit. The cost basis for the partial
        is the pre-decrement sol_invested × sell_fraction.

        Position is NOT popped; the caller still owns the residual.
        """
        partial_invested = pos.sol_invested * sell_fraction
        sol_received     = float(sell_result.get("sol_received", 0) or 0)
        pnl_sol          = sol_received - partial_invested

        trade_record = {
            "mint":         mint,
            "symbol":       pos.symbol,
            "creator":      pos.creator,
            "entry_time":   pos.entry_time,
            "exit_time":    time.time(),
            "sol_invested": partial_invested,
            "sol_received": sol_received,
            "pnl_sol":      pnl_sol,
            "pnl_pct":      pos.pnl_pct,         # at the moment of TP
            "hold_minutes": pos.age_minutes,
            "reason":       sell_result.get("reason", level_id),
            "score":        pos.score,
            "partial":      True,                # marker for downstream filtering
            "sol_received_optimistic": sell_result.get("sol_received_optimistic"),
            "exit_latency_s":          sell_result.get("exit_latency_s"),
        }
        self.closed_trades.append(trade_record)
        self._append_closed_trade(trade_record)

        # Feed the partial gain to the creator tracker too — successful TPs
        # are useful signal for the creator leaderboard.
        if pos.creator:
            creator_tracker.record_trade_result(pos.creator, pnl_sol)

        sign = "+" if pnl_sol >= 0 else ""
        logger.success(
            f"[TP PARTIAL CLOSED] {pos.symbol} | leg={level_id} | "
            f"invested={partial_invested:.4f} → received={sol_received:.4f} | "
            f"PnL {sign}{pnl_sol:.4f} SOL"
        )

    def close_position(self, mint: str, sell_result: dict):
        pos = self.positions.pop(mint, None)
        if not pos:
            return
        self._save_open_positions()   # disk no longer lists this mint

        pnl_sol = sell_result.get("sol_received", 0) - pos.sol_invested

        trade_record = {
            "mint":         mint,
            "symbol":       pos.symbol,
            "creator":      pos.creator,
            "entry_time":   pos.entry_time,
            "exit_time":    time.time(),
            "sol_invested": pos.sol_invested,
            "sol_received": sell_result.get("sol_received", 0),
            "pnl_sol":      pnl_sol,
            "pnl_pct":      pos.pnl_pct,
            "hold_minutes": pos.age_minutes,
            "reason":       sell_result.get("reason", "unknown"),
            "score":        pos.score,           # for score-bin learning loop
            # Latency-honest accounting (paper sim). On stall-class exits the
            # executor returns the optimistic counterfactual — what sol_received
            # would have been if priced at the latest tick with no stampede
            # multiplier. Diff is the structural friction we'd previously been
            # ignoring. None on non-stall exits and on live trades.
            "sol_received_optimistic": sell_result.get("sol_received_optimistic"),
            "exit_latency_s":          sell_result.get("exit_latency_s"),
        }
        self.closed_trades.append(trade_record)
        self._append_closed_trade(trade_record)   # persist to JSONL

        # Feed result back into creator tracker for leaderboard
        if pos.creator:
            creator_tracker.record_trade_result(pos.creator, pnl_sol)

        # Smart-money attribution (Plan A 2026-05-10): widen the wallet_intel
        # outcome pool to include mints we actually traded, not just the
        # rejected population. pnl_pct on a closed live trade is a strong
        # outcome signal — better-than-counterfactual since we observed the
        # full trade lifecycle, not just a 10-min snapshot.
        try:
            wallet_intel.attribute_outcome(mint, float(pos.pnl_pct))
        except Exception as e:
            logger.debug(f"[risk] wallet_intel.attribute_outcome err: {e}")

        # Rug-pattern memory: if this trade rugged, record its fingerprint
        # so future candidates with the same pattern get docked at scoring.
        # Two-condition guard:
        #   1. pnl_pct <= RUG_PNL_THRESHOLD  (e.g., -50%)
        #   2. absolute SOL loss > 0.005     (filters friction-only "rugs"
        #      on tiny trades — a 0.01 SOL trade losing 50% is just fee drag)
        # And use RAW score (pre-rug-penalty) so the bucket key matches the
        # one the scorer uses at lookup time.
        if pos.pnl_pct <= RUG_PNL_THRESHOLD and pnl_sol < -0.005:
            rug_memory.record_rug(
                token_features = {
                    "initial_buy_sol":   pos.init_buy_sol,
                    "bonding_curve_pct": pos.bonding_curve_pct,
                    "score":             pos.raw_score,
                },
                pnl_pct       = pos.pnl_pct,
                hold_minutes  = pos.age_minutes,
                mint          = mint,
                symbol        = pos.symbol,
            )

        # Tier 2: loss-streak circuit breaker
        if pnl_sol <= 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= LOSS_STREAK_LIMIT:
                self.loss_streak_pause_until = time.time() + LOSS_STREAK_PAUSE_MIN * 60
                logger.warning(
                    f"[CIRCUIT BREAKER] {self.consecutive_losses} consecutive losses — "
                    f"pausing new buys for {LOSS_STREAK_PAUSE_MIN} min"
                )
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0

        sign = "+" if pnl_sol >= 0 else ""
        logger.info(
            f"[POSITION CLOSED] {pos.symbol} | "
            f"PnL: {sign}{pnl_sol:.4f} SOL ({sign}{pos.pnl_pct:.1f}%) | "
            f"held {pos.age_minutes:.1f}min | reason={trade_record['reason']}"
        )

        # Push to Telegram if configured. No-op if creds aren't set.
        emoji = "✅" if pnl_sol > 0 else ("⚠️" if pnl_sol == 0 else "❌")
        if pos.pnl_pct >= 50:
            emoji = "🚀"
        send_alert(
            f"{emoji} <b>{pos.symbol}</b> {sign}{pnl_sol:.4f} SOL "
            f"({sign}{pos.pnl_pct:.1f}%) | {pos.age_minutes:.1f}m | {trade_record['reason']}"
        )

    # ── Price update ──────────────────────────────────────────────────────────
    def update_price(self, mint: str, current_price_sol: float):
        pos = self.positions.get(mint)
        if not pos:
            return
        pos.current_price = current_price_sol
        pos.price_updated_at = time.time()
        if current_price_sol > pos.highest_price:
            pos.highest_price = current_price_sol

        # Append to rolling price history (keep last ~5 min worth).
        # Tuple shape: (ts, pnl_pct, price_sol). Existing readers consume only
        # indices 0/1 so the widened tuple is backward-compatible; index 2
        # feeds the paper executor's latency-honest exit pricing.
        now = time.time()
        pos.price_history.append((now, pos.pnl_pct, current_price_sol))
        cutoff = now - 300
        if pos.price_history and pos.price_history[0][0] < cutoff:
            pos.price_history = [p for p in pos.price_history if p[0] >= cutoff]

    # ── Main monitoring loop ──────────────────────────────────────────────────
    async def run_monitor_loop(self):
        self.running = True
        logger.info("Risk monitor loop started")

        while self.running:
            try:
                await self._check_emergency_stop()
                for mint in list(self.positions.keys()):
                    await self._check_position(mint)
            except Exception as e:
                logger.error(f"Risk monitor error: {e}")
            await asyncio.sleep(3)

    def _held_value_for_drawdown(self) -> float:
        """Sum of market value of open positions, BUT positions whose
        price hasn't been refreshed within STALE_PRICE_SEC are treated
        as worth 0 in the equity calc. Conservative: a stale price
        during a fast dump inflates equity, making drawdown look smaller
        and delaying the emergency trigger. Better to fire slightly
        early on stale data than miss a real drawdown."""
        now   = time.time()
        total = 0.0
        for p in self.positions.values():
            age = now - p.price_updated_at if p.price_updated_at else None
            if age is None or age > STALE_PRICE_SEC:
                continue   # treat as 0
            total += p.current_price * p.tokens_held
        return total

    async def _check_emergency_stop(self):
        if self.starting_sol_balance == 0:
            return

        # Drawdown is on TOTAL EQUITY (liquid SOL + open position market value),
        # not liquid SOL alone — otherwise a healthy 0.4 SOL position looks like
        # a 40% loss the moment it's opened. Stale-price positions are
        # excluded conservatively (see _held_value_for_drawdown).
        current_sol = await self.wallet.get_sol_balance()
        held_value  = self._held_value_for_drawdown()
        total_equity = current_sol + held_value
        drawdown = ((self.starting_sol_balance - total_equity) / self.starting_sol_balance) * 100

        # Already tripped via auto-drawdown: keep retrying force-sells each tick
        # until everything closes. Manual dashboard stop sets emergency_stop_active
        # alone, NOT emergency_force_sell — open positions exit naturally on TP/SL.
        if self.emergency_force_sell:
            for mint in list(self.positions.keys()):
                await self._force_sell(mint, "emergency_stop")
            return

        if drawdown >= EMERGENCY_STOP_DRAWDOWN_PCT:
            self.emergency_stop_active = True
            self.emergency_force_sell  = True
            self._save_risk_state()    # persist so a restart doesn't wipe this
            logger.critical(
                f"EMERGENCY STOP TRIGGERED | Drawdown: {drawdown:.1f}% | "
                f"Equity: {total_equity:.4f} SOL (liquid {current_sol:.4f} + held {held_value:.4f})"
            )
            for mint in list(self.positions.keys()):
                await self._force_sell(mint, "emergency_stop")

    async def _check_position(self, mint: str):
        pos = self.positions.get(mint)
        if not pos:
            return

        pnl_pct = pos.pnl_pct

        # ── Early-rug detector (first EARLY_RUG_WINDOW_SEC seconds) ──────────
        # Trade-DB analysis (2026-05-08): of 27 stop-losses, 18 fired in the
        # first 2 minutes and routinely closed at -22% to -53% due to sell
        # slippage during a dump. Exiting at -EARLY_RUG_PCT (default -5%) the
        # moment a fresh position turns south escapes the worst of that drop.
        age_seconds = time.time() - pos.entry_time
        if age_seconds <= EARLY_RUG_WINDOW_SEC and pnl_pct <= -EARLY_RUG_PCT:
            logger.warning(
                f"[EARLY RUG] {pos.symbol} | PnL: {pnl_pct:.1f}% in {age_seconds:.0f}s | Selling 100%"
            )
            await self._force_sell(mint, "early_rug")
            return

        # ── Hard stop loss ───────────────────────────────────────────────────
        if pnl_pct <= -STOP_LOSS_PCT:
            # Panic floor: position has rotted past the wider band even though
            # the primary stop-loss should have already fired. Likely a stuck
            # sell tx or rug-pulled liquidity. Log distinctly so it's findable
            # in the log, and use a different reason string so closed_trades
            # downstream can distinguish "we missed the stop" from "we hit it".
            reason = "stop_loss"
            if pnl_pct <= -PANIC_STOP_LOSS_PCT:
                reason = "panic_stop_loss"
                logger.critical(
                    f"[PANIC STOP] {pos.symbol} | PnL: {pnl_pct:.1f}% past -{PANIC_STOP_LOSS_PCT}% floor — "
                    f"force-selling 100% (regular stop fired at -{STOP_LOSS_PCT}% but position still open)"
                )
            else:
                logger.warning(
                    f"[STOP LOSS] {pos.symbol} | PnL: {pnl_pct:.1f}% | Selling 100%"
                )
            await self._force_sell(mint, reason)
            return

        # ── Adaptive trailing stop ─────────────────────────────────────────────
        # Doesn't activate until peak has crossed TRAILING_STOP_MIN_PROFIT —
        # prevents early flicker shake-outs. Wider trailing band once we're
        # in moonshot territory so the +500% tail doesn't get clipped.
        if TRAILING_STOP_ENABLED and pos.highest_price > pos.entry_price_sol:
            peak_pnl_pct = ((pos.highest_price - pos.entry_price_sol) / pos.entry_price_sol) * 100
            # Don't trail until peak is meaningfully above entry
            if peak_pnl_pct >= TRAILING_STOP_MIN_PROFIT:
                trailing_band = (TRAILING_STOP_MOONSHOT_PCT
                                 if peak_pnl_pct >= TRAILING_STOP_MOONSHOT_TRIGGER
                                 else TRAILING_STOP_PCT)
                drawdown_from_peak = (
                    (pos.highest_price - pos.current_price) / pos.highest_price
                ) * 100
                if drawdown_from_peak >= trailing_band:
                    mode = "moonshot" if trailing_band == TRAILING_STOP_MOONSHOT_PCT else "tight"
                    logger.warning(
                        f"[TRAILING STOP/{mode}] {pos.symbol} | peak +{peak_pnl_pct:.0f}% | "
                        f"Down {drawdown_from_peak:.1f}% from peak | Selling"
                    )
                    await self._force_sell(mint, f"trailing_stop_{mode}")
                    return

        # ── Tiered take-profits ───────────────────────────────────────────────
        for tp in TAKE_PROFIT_LEVELS:
            level_id = f"tp_{tp['gain_pct']}"
            if level_id not in pos.tp_levels_hit and pnl_pct >= tp["gain_pct"]:
                sell_fraction = tp["sell_pct"] / 100
                if sell_fraction <= 0 or pos.tokens_held <= 0:
                    continue

                logger.success(
                    f"[TAKE PROFIT] {pos.symbol} +{pnl_pct:.0f}% | "
                    f"Selling {tp['sell_pct']}% of remaining"
                )

                # Pass percentage string — PumpPortal accepts "15%" but 400s on
                # raw integer token counts for bonding-curve mints.
                result = await self.executor.sell(
                    mint, f"{tp['sell_pct']}%",
                    reason=f"take_profit_{tp['gain_pct']}pct"
                )

                if not result.get("success"):
                    logger.warning(
                        f"[TP SELL FAILED] {pos.symbol} | "
                        f"error={result.get('error', 'unknown')} | will retry next tick"
                    )
                    continue  # don't mark level hit; retry on the next monitor pass

                # Record the partial as its own trade BEFORE the cost-basis
                # decrement, so closed_trades reflects the SOL the wallet
                # actually received from this leg. Previously TPs were only
                # locally accounted (pos.sol_invested *= 0.85) and never
                # written to trades.db — the auto_tuner saw only final
                # exits per position, which systematically undercounted
                # wins. Skip the partial record on the final leg (handled
                # by close_position below) to avoid double-counting.
                final_leg = int(pos.tokens_held * (1 - sell_fraction)) <= 0
                if not final_leg:
                    self._record_partial_close(mint, pos, sell_fraction, result, level_id)

                # Local accounting: PumpPortal sold sell_fraction of the ATA balance,
                # so reduce our tracked tokens_held + cost basis by the same fraction.
                pos.tokens_held  = int(pos.tokens_held * (1 - sell_fraction))
                pos.sol_invested *= (1 - sell_fraction)
                pos.tp_levels_hit.append(level_id)
                self._save_open_positions()   # disk reflects post-TP state

                if pos.tokens_held <= 0:
                    self.close_position(mint, result)
                    return

        # ── No-movement exit (data: dead-after-2min trades almost never recover) ─
        # If position is flat (within ±NO_MOVEMENT_BAND_PCT) for the first
        # NO_MOVEMENT_EXIT_SECONDS, exit immediately. Don't wait for time_exit.
        age_seconds = (time.time() - pos.entry_time)
        if age_seconds >= NO_MOVEMENT_EXIT_SECONDS and abs(pnl_pct) <= NO_MOVEMENT_BAND_PCT:
            logger.warning(
                f"[NO MOVEMENT] {pos.symbol} | PnL {pnl_pct:+.1f}% | "
                f"flat for {age_seconds:.0f}s | dead token, exiting"
            )
            await self._force_sell(mint, "no_movement")
            return

        # ── Tier 2: momentum-stall exit ───────────────────────────────────────
        # If we're in profit and price has gone flat for STALL_WINDOW_SEC,
        # sell. Pump.fun pumps are bursts; flat = the pump is over.
        # Skip once a position has been in moonshot territory (peak ≥ trailing
        # moonshot trigger): the wider trailing_stop_moonshot is purpose-built
        # to ride those, and momentum_stall would clip the +500% tail.
        peak_pnl_pct = (
            ((pos.highest_price - pos.entry_price_sol) / pos.entry_price_sol) * 100
            if pos.entry_price_sol > 0 else 0
        )
        if (
            MOMENTUM_STALL_ENABLED
            and pnl_pct >= MOMENTUM_STALL_MIN_PROFIT
            and peak_pnl_pct < TRAILING_STOP_MOONSHOT_TRIGGER
        ):
            now = time.time()
            window_pts = [p for p in pos.price_history
                          if p[0] >= now - MOMENTUM_STALL_WINDOW_SEC]
            if len(window_pts) >= 3:
                pnls = [p[1] for p in window_pts]
                spread = max(pnls) - min(pnls)
                if spread <= MOMENTUM_STALL_PCT_BAND:
                    logger.warning(
                        f"[MOMENTUM STALL] {pos.symbol} | PnL: {pnl_pct:.1f}% | "
                        f"flat in ±{MOMENTUM_STALL_PCT_BAND}% for {MOMENTUM_STALL_WINDOW_SEC}s | Selling"
                    )
                    await self._force_sell(mint, "momentum_stall")
                    return

        # ── Time-based exit ───────────────────────────────────────────────────
        if pos.age_minutes > TIME_EXIT_MINUTES:
            logger.warning(
                f"[TIME EXIT] {pos.symbol} | >{TIME_EXIT_MINUTES}min | Selling"
            )
            await self._force_sell(mint, "time_exit")

    async def _force_sell(self, mint: str, reason: str):
        pos = self.positions.get(mint)
        if not pos or pos.tokens_held <= 0:
            self.positions.pop(mint, None)
            return

        # Pre-built sell tx fast-path: if we cached one within the last ~50s,
        # use it — Solana blockhashes expire ~150 slots (~60s), so 50s is the
        # safe staleness limit. Saves ~200-500ms on the time-critical exit
        # path. Stale prebuilt is dropped and we fall back to building fresh.
        prebuilt = None
        if pos.prebuilt_sell_tx and (time.time() - pos.prebuilt_sell_ts) < 50:
            prebuilt = pos.prebuilt_sell_tx

        # Use percentage string — PumpPortal 400s on raw integer token amounts
        # for bonding-curve mints.
        sol_before = await self.wallet.get_sol_balance()
        # price_history is used by the paper executor for latency-honest exit
        # pricing on stall-class force-sells. Live executor ignores it.
        result = await self.executor.sell(
            mint, "100%",
            reason=reason,
            prebuilt_tx=prebuilt,
            price_history=pos.price_history,
        )

        if not result.get("success"):
            pos.force_sell_attempts += 1
            self._save_open_positions()
            err = result.get("error", "unknown")
            if pos.force_sell_attempts >= MAX_FORCE_SELL_ATTEMPTS:
                # The mint is almost certainly dead (no liquidity / paused
                # pool). Drop it from self.positions so the emergency loop
                # isn't permanently stuck retrying this one zombie and
                # burning the buy-side priority fee floor on every tick.
                # Tokens may still be in the wallet — recover via
                # dump_orphans.py once liquidity returns.
                logger.critical(
                    f"[FORCE SELL ABANDONED] {pos.symbol} ({mint[:8]}) | "
                    f"{pos.force_sell_attempts} attempts failed; last error={err} | "
                    f"DROPPING from positions. Use dump_orphans.py to recover tokens."
                )
                self.positions.pop(mint, None)
                self._save_open_positions()
                return
            logger.warning(
                f"[FORCE SELL FAILED] {pos.symbol} ({mint[:8]}) | reason={reason} | "
                f"attempt {pos.force_sell_attempts}/{MAX_FORCE_SELL_ATTEMPTS} | "
                f"error={err} | leaving position open for retry"
            )
            return

        # Compute sol_received from the sell tx receipt directly. Polling
        # getBalance after a sendTransaction is unreliable on Helius — the
        # indexer lags 5-15s, so the post-sell read kept returning the
        # pre-sell value and the bot kept logging sol_received=0 = -100%
        # PnL on every close. The tx receipt has the exact deltas inline.
        sol_received = await self._sol_delta_from_tx(result.get("signature"))
        if sol_received <= 0:
            # Fallback: poll wallet balance with a longer wait. Logs but
            # doesn't fail the close — at worst we under-report sol_received.
            logger.debug(f"[SOL DELTA] tx-receipt parse miss for {mint[:8]}, falling back to poll")
            await asyncio.sleep(8)
            sol_after = await self.wallet.get_sol_balance()
            sol_received = max(0.0, sol_after - sol_before)

        result["sol_received"] = sol_received
        self.close_position(mint, result)

    async def _sol_delta_from_tx(self, sig: str | None) -> float:
        """Parse the wallet's SOL credit from a confirmed sell tx receipt.

        On-chain source of truth: postBalances[i] - preBalances[i] for our
        wallet's account index. Avoids the Helius getBalance indexer lag.
        Retries up to ~10s because the tx may take a moment to be queryable.
        """
        if not sig:
            return 0.0
        owner = str(self.wallet.pubkey)
        for attempt in range(7):
            await asyncio.sleep(1.5)
            try:
                resp = await self.wallet._rpc("getTransaction", [
                    sig,
                    {"encoding": "jsonParsed",
                     "maxSupportedTransactionVersion": 0,
                     "commitment": "confirmed"},
                ])
                res = resp.get("result")
                if not res:
                    continue
                meta = res.get("meta") or {}
                if meta.get("err"):
                    return 0.0  # tx failed on chain
                keys = res.get("transaction", {}).get("message", {}).get("accountKeys", [])
                pre  = meta.get("preBalances", []) or []
                post = meta.get("postBalances", []) or []
                for i, k in enumerate(keys):
                    addr = k.get("pubkey") if isinstance(k, dict) else k
                    if addr == owner and i < len(pre) and i < len(post):
                        return max(0.0, (post[i] - pre[i]) / 1e9)
            except Exception as e:
                logger.debug(f"[SOL DELTA] attempt {attempt+1} failed: {e}")
        return 0.0

    # ── Stats ─────────────────────────────────────────────────────────────────
    def get_stats(self) -> dict:
        # PnL from sum of trade records — accurate for trades happened during
        # this bot's lifetime, but doesn't include pre-persistence history.
        record_pnl = sum(t["pnl_sol"] for t in self.closed_trades)
        wins       = [t for t in self.closed_trades if t["pnl_sol"] > 0]
        now = time.time()
        paused = (now < self.loss_streak_pause_until) or self.day_paused
        return {
            "open_positions":  len(self.positions),
            "closed_trades":   len(self.closed_trades),
            "win_rate":        len(wins) / max(len(self.closed_trades), 1),
            "total_pnl_sol":   record_pnl,
            "total_exposure":  sum(p.sol_invested for p in self.positions.values()),
            "emergency_stop":  self.emergency_stop_active,
            "paused":          paused,
            "pause_reason":    self.pause_reason if paused else "",
            "consecutive_losses": self.consecutive_losses,
        }

    def stop(self):
        self.running = False
