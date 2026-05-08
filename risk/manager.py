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
import json
import os
import time
from dataclasses import dataclass, field

from loguru import logger

CLOSED_TRADES_FILE = "logs/closed_trades.jsonl"
RISK_STATE_FILE    = "logs/risk_state.json"
import datetime

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
    EMERGENCY_STOP_DRAWDOWN_PCT,
    LOSS_STREAK_LIMIT,
    LOSS_STREAK_PAUSE_MIN,
    MAX_OPEN_POSITIONS,
    MAX_POSITION_PCT,
    MAX_SOL_PER_TRADE,
    MAX_TOTAL_EXPOSURE_SOL,
    MOMENTUM_STALL_ENABLED,
    MOMENTUM_STALL_MIN_PROFIT,
    MOMENTUM_STALL_PCT_BAND,
    MOMENTUM_STALL_WINDOW_SEC,
    NO_MOVEMENT_BAND_PCT,
    NO_MOVEMENT_EXIT_SECONDS,
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
from logger.trade_db import get_trade_db


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
    # Rolling (timestamp, pnl_pct) tuples for momentum-stall detection.
    price_history:   list  = field(default_factory=list)

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

        # ── Tier 2 circuit breakers ─────────────────────────────────────────
        self.consecutive_losses     = 0
        self.loss_streak_pause_until = 0   # epoch sec
        self.day_baseline_balance   = 0    # captured at UTC midnight
        self.day_baseline_date      = None
        self.day_paused             = False
        self.pause_reason           = ""

    async def initialize(self):
        os.makedirs("logs", exist_ok=True)

        # Load historical closed trades from JSONL so the dashboard doesn't
        # forget what's happened across bot restarts.
        self._load_closed_trades()

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

    def _load_or_seed_starting_balance(self, current_balance: float) -> float:
        """
        First time we run, persist the current balance as the original
        starting capital. Every subsequent run, load that same value so PnL
        is measured against the bot's original seed, not whatever's left.
        """
        try:
            if os.path.exists(RISK_STATE_FILE):
                with open(RISK_STATE_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                v = float(data.get("original_starting_sol", current_balance))
                if v > 0:
                    return v
        except Exception:
            pass
        # Seed it
        try:
            with open(RISK_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"original_starting_sol": current_balance}, f, indent=2)
        except Exception as e:
            logger.warning(f"[RISK] Could not seed starting balance: {e}")
        return current_balance

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
        today = datetime.datetime.utcnow().date()
        if self.day_baseline_date != today:
            sol = await self.wallet.get_sol_balance()
            held = sum(p.current_price * p.tokens_held for p in self.positions.values())
            self.day_baseline_balance = sol + held
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
    async def calculate_position_size(self, score: int) -> float:
        if self.emergency_stop_active:
            logger.warning("Emergency stop active — no new positions")
            return 0

        if await self._is_paused():
            logger.warning(f"Trading paused: {self.pause_reason}")
            return 0

        if len(self.positions) >= MAX_OPEN_POSITIONS:
            logger.warning(f"Max positions reached ({MAX_OPEN_POSITIONS})")
            return 0

        total_exposure = sum(p.sol_invested for p in self.positions.values())
        if total_exposure >= MAX_TOTAL_EXPOSURE_SOL:
            logger.warning(f"Max exposure reached ({total_exposure:.3f} SOL)")
            return 0

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

        # Minimum viable trade — covers fees + slippage with room to profit.
        min_viable = min(MAX_SOL_PER_TRADE / 2, 0.003)
        if size < min_viable:
            return 0

        logger.info(
            f"Position size: {size:.4f} SOL "
            f"(score={score}, mult={multiplier:.2f} {adaptive_note}, balance={sol_balance:.4f})"
        )
        return round(size, 4)

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
            mint            = mint,
            symbol          = symbol,
            creator         = creator,
            entry_price_sol = entry_price,
            entry_time      = time.time(),
            sol_invested    = sol_spent,
            tokens_held     = tokens_received,
            current_price   = entry_price,
            highest_price   = entry_price,
            score           = token.get("score", 0),
        )

        self.positions[mint] = pos
        logger.success(
            f"[POSITION OPENED] {symbol} | {sol_spent:.4f} SOL | "
            f"entry={entry_price:.10f} | score={pos.score}"
        )

    # ── Close position ────────────────────────────────────────────────────────
    def close_position(self, mint: str, sell_result: dict):
        pos = self.positions.pop(mint, None)
        if not pos:
            return

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
        }
        self.closed_trades.append(trade_record)
        self._append_closed_trade(trade_record)   # persist to JSONL

        # Feed result back into creator tracker for leaderboard
        if pos.creator:
            creator_tracker.record_trade_result(pos.creator, pnl_sol)

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

    # ── Price update ──────────────────────────────────────────────────────────
    def update_price(self, mint: str, current_price_sol: float):
        pos = self.positions.get(mint)
        if not pos:
            return
        pos.current_price = current_price_sol
        if current_price_sol > pos.highest_price:
            pos.highest_price = current_price_sol

        # Append to rolling price history (keep last ~5 min worth)
        now = time.time()
        pos.price_history.append((now, pos.pnl_pct))
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

    async def _check_emergency_stop(self):
        if self.starting_sol_balance == 0:
            return

        # Drawdown is on TOTAL EQUITY (liquid SOL + open position market value),
        # not liquid SOL alone — otherwise a healthy 0.4 SOL position looks like
        # a 40% loss the moment it's opened.
        current_sol = await self.wallet.get_sol_balance()
        held_value  = sum(p.current_price * p.tokens_held for p in self.positions.values())
        total_equity = current_sol + held_value
        drawdown = ((self.starting_sol_balance - total_equity) / self.starting_sol_balance) * 100

        # Already tripped: keep retrying force-sells each tick until everything closes.
        # _force_sell leaves the position open if the sell tx fails, so we'd otherwise
        # be stuck holding orphans forever.
        if self.emergency_stop_active:
            for mint in list(self.positions.keys()):
                await self._force_sell(mint, "emergency_stop")
            return

        if drawdown >= EMERGENCY_STOP_DRAWDOWN_PCT:
            self.emergency_stop_active = True
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

        # ── Hard stop loss (-10%) ─────────────────────────────────────────────
        if pnl_pct <= -STOP_LOSS_PCT:
            logger.warning(
                f"[STOP LOSS] {pos.symbol} | PnL: {pnl_pct:.1f}% | Selling 100%"
            )
            await self._force_sell(mint, "stop_loss")
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

                # Local accounting: PumpPortal sold sell_fraction of the ATA balance,
                # so reduce our tracked tokens_held + cost basis by the same fraction.
                pos.tokens_held  = int(pos.tokens_held * (1 - sell_fraction))
                pos.sol_invested *= (1 - sell_fraction)
                pos.tp_levels_hit.append(level_id)

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
        if MOMENTUM_STALL_ENABLED and pnl_pct >= MOMENTUM_STALL_MIN_PROFIT:
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

        # Use percentage string — PumpPortal 400s on raw integer token amounts
        # for bonding-curve mints. Capture wallet SOL delta to learn what the
        # sell actually returned (PumpPortal doesn't echo sol_received).
        sol_before = await self.wallet.get_sol_balance()
        result = await self.executor.sell(mint, "100%", reason=reason)

        if not result.get("success"):
            logger.warning(
                f"[FORCE SELL FAILED] {pos.symbol} ({mint[:8]}) | reason={reason} | "
                f"error={result.get('error', 'unknown')} | leaving position open for retry"
            )
            return

        # Wait a couple seconds for the SOL credit to settle, then capture delta.
        await asyncio.sleep(2)
        sol_after = await self.wallet.get_sol_balance()
        result["sol_received"] = max(0.0, sol_after - sol_before)
        self.close_position(mint, result)

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
