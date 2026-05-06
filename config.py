"""
config.py — Pump Bot configuration.
Upgraded with open-source alpha strategies:
  - Creator tracking (Dexter approach, 35-45% win rate)
  - Bonding curve progress filtering (eliminates 95% of rugs)
  - Velocity-based scoring (40-60% better graduation odds)
  - Tighter risk management (-10% stop loss, 25/50% take-profits)
"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)

# ─── PAPER TRADING ─────────────────────────────────────────────────────────────
PAPER_TRADING       = True    # Set False to trade with real money
PAPER_STARTING_SOL  = 1.0    # Virtual SOL balance for paper mode
PAPER_TIME_EXIT_MIN = 10      # Force-exit paper positions after N minutes

# ─── WALLET ────────────────────────────────────────────────────────────────────
PRIVATE_KEY    = os.getenv("SOLANA_PRIVATE_KEY", "YOUR_PRIVATE_KEY_HERE")
RPC_URL        = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")

# ─── API KEYS ──────────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")
TELEGRAM_API_ID      = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH    = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE       = os.getenv("TELEGRAM_PHONE", "")
BIRDEYE_API_KEY      = os.getenv("BIRDEYE_API_KEY", "")

# ─── DETECTION SETTINGS ────────────────────────────────────────────────────────
PUMPFUN_POLL_INTERVAL = 1
DEX_POLL_INTERVAL     = 5
MIN_LIQUIDITY_SOL     = 0       # PumpPortal trades bonding-curve tokens directly
MAX_TOKEN_AGE_MINUTES = 20

TELEGRAM_CHANNELS = []
TWITTER_KEYWORDS  = []

# ─── PUMP.FUN COMMENT/METADATA TRACKER ─────────────────────────────────────────
# Polls frontend-api-v3.pump.fun/coins/{mint} per new mint to capture
# reply velocity, ATH ratio, trade staleness, livestream status.
# Free, public endpoint. Reply deltas feed the existing social_mentions_*
# scoring terms automatically.
PUMPFUN_TRACK_ENABLED          = True
PUMPFUN_TRACK_MINUTES          = 10    # follow each mint for this long
PUMPFUN_POLL_INTERVAL_S        = 3     # poll cadence per mint
PUMPFUN_MAX_CONCURRENT_TRACKS  = 60    # concurrency cap to bound memory + req rate

# ─── HARD FILTERS (win-rate boosters) ──────────────────────────────────────────
# Reject tokens already this far below their ATH at score time.
# 0 disables. 0.5 = skip if current MC < 50% of all-time-high MC.
# Only applies when tracker has data; brand-new mints aren't affected.
ATH_RATIO_REJECT_BELOW = 0.5

# Skip new entries during low-volume hours (UTC). Disabled until LEARN-tab
# counterfactual data shows which hours actually correlate with rugs.
# (Previous (3, 9) UTC was 8 PM - 2 AM Pacific = active US retail, not dead.)
# Set to None to disable. (start_hour, end_hour) inclusive-exclusive in UTC.
DEAD_HOURS_UTC = None

# Symbol/name pattern blacklist — almost-always rugs.
SYMBOL_BLACKLIST_EXACT = {
    "TEST", "TST", "ASDF", "QWER", "1234", "AAAA", "BBBB",
    "BTC", "ETH", "SOL", "USDC", "USDT",       # ticker-squatters
}
NAME_BLACKLIST_SUBSTRINGS = [
    "test ", " test", "asdf", "delete me", "remove me",
]

# ─── SCORING THRESHOLDS ────────────────────────────────────────────────────────
MIN_BUY_SCORE = 42              # Tier 1: lowered for 5x more data per day in paper

# Bonding curve progress filter (open-source research: 30%+ eliminates 95% of rugs)
# Set 0 for fresh token sniping (max speed), or 30 for safer entries
MIN_BONDING_CURVE_PCT = 0.0
MAX_BONDING_CURVE_PCT = 80.0    # Never buy within 20% of migration (~85 SOL)

# Buy cooldown after detection (0 = instant, 15 = safer mode from Chainstack research)
BUY_COOLDOWN_SECONDS = 0

# ─── CREATOR TRACKING (Dexter strategy — best documented win-rate booster) ────
CREATOR_TRACKING_ENABLED = True
CREATOR_TOP_N            = 50   # Only auto-boost tokens from top N creators
CREATOR_MIN_LAUNCHES     = 2    # Creator needs 2+ launches to appear in tracker
CREATOR_DB_FILE          = "logs/creators.json"

# ─── RISK MANAGEMENT ───────────────────────────────────────────────────────────
# Tier 1 sizing: smaller positions, more concurrent trades = larger sample size
# HIGH-RISK TIER — average trade ~3.33% of wallet (1.895 SOL ≈ $300).
# Hot-streak trades can hit 6-8% of wallet on a single position.
MAX_SOL_PER_TRADE      = 0.063    # ~3.33% of 1.895 SOL wallet (base trade)
MAX_POSITION_PCT       = 0.35     # 35% of wallet hard cap on any single trade
MAX_TOTAL_EXPOSURE_SOL = 0.40     # ~21% of wallet at risk simultaneously
MAX_OPEN_POSITIONS     = 5

# Adaptive position sizing (Kelly lite):
# Looks at win rate of last N closed trades and scales position size.
# Hot streak = bet bigger (ride momentum). Cold streak = bet smaller
# (don't dig deeper). Score-based sizing was disabled because data shows
# scores above 42 don't differentiate winners.
ADAPTIVE_SIZING_ENABLED = True
ADAPTIVE_LOOKBACK       = 20
ADAPTIVE_HOT_WR         = 0.55
ADAPTIVE_HOT_MULT       = 2.0     # was 1.5 — compound aggressively on hot streaks
ADAPTIVE_COLD_WR        = 0.30
ADAPTIVE_COLD_MULT      = 0.6
ADAPTIVE_HARD_CAP_MULT  = 2.5     # was 2.0 — allows 0.0625 SOL max single trade

# MAXIMUM-MOONSHOT TP ladder. Pushed all targets way out — most winners
# get caught by the trailing stop on the way down (which only activates
# after a real profit), so we don't miss small wins. The big wins ride
# uncapped to 10x, 20x, beyond.
TAKE_PROFIT_LEVELS = [
    {"gain_pct": 75,  "sell_pct": 15},   # +75%: tiny lock
    {"gain_pct": 300, "sell_pct": 25},   # 4x: lock 25%
    {"gain_pct": 800, "sell_pct": 30},   # 9x: lock 30%
    # Final ~30% rides with trailing stop — uncapped, can hit 30x+
]

# Trailing stop is the primary "small winner" capture mechanism now.
# It does NOT activate until peak crosses TRAILING_STOP_MIN_PROFIT — so
# small fluctuations near entry don't shake us out.
TRAILING_STOP_ENABLED            = True
TRAILING_STOP_MIN_PROFIT         = 15   # don't even start trailing until +15%
TRAILING_STOP_PCT                = 18   # widened — give early winners more room
TRAILING_STOP_MOONSHOT_PCT       = 35   # very wide once in moonshot mode
TRAILING_STOP_MOONSHOT_TRIGGER   = 100  # at +100% peak, switch to moonshot trailing

STOP_LOSS_PCT               = 10
EMERGENCY_STOP_DRAWDOWN_PCT = 40
TIME_EXIT_MINUTES           = 4    # was 8 — DATA: trades >6 min have 3.7% WR

# Data-driven: trades <3 min have 59% WR, >6 min have 3.7% WR.
# If after this many seconds the position hasn't moved either direction,
# exit immediately — it's a dead token, don't wait for time_exit.
NO_MOVEMENT_EXIT_SECONDS    = 120  # 2 min
NO_MOVEMENT_BAND_PCT        = 3.0  # ±3% counts as "not moving"

# ─── TIER 2 GUARDRAILS ─────────────────────────────────────────────────────────
# Auto-pause new buys after this many consecutive losing closes.
# Resumes after RESUME_AFTER_MINUTES of cooling off.
LOSS_STREAK_LIMIT      = 6
LOSS_STREAK_PAUSE_MIN  = 10

# Daily PnL bands. If hit, pause all new buys for the rest of the UTC day.
DAILY_LOSS_LIMIT_PCT   = 25      # still active — protects from catastrophic loss days
DAILY_PROFIT_LOCK_PCT  = 99999   # DISABLED — keep trading no matter how high we go

# Momentum-stall exit: if a position is in profit but price has not moved
# more than this percent in either direction for STALL_WINDOW_SECONDS, sell.
# Pump.fun gains stall = the pump is over.
MOMENTUM_STALL_ENABLED      = True
MOMENTUM_STALL_PCT_BAND     = 5.0    # ±5% movement window
MOMENTUM_STALL_WINDOW_SEC   = 60     # stall duration that triggers exit
MOMENTUM_STALL_MIN_PROFIT   = 5.0    # only trigger above +5% PnL

# Holder concentration filter (rug indicator). Calls getTokenLargestAccounts.
# Skip a token if top 10 holders own more than this % of supply.
# Set 0 to disable.
HOLDER_CONCENTRATION_LIMIT_PCT = 70.0

# ─── EXECUTION SETTINGS ────────────────────────────────────────────────────────
SLIPPAGE_BPS               = 1500   # 15% — pump.fun tokens need wide slippage
PRIORITY_FEE_MICROLAMPORTS = 300000 # Jupiter/RPC fee
PRIORITY_FEE_SOL           = 0.001  # PumpPortal API (total SOL, not per-CU)
JUPITER_API_URL            = "https://quote-api.jup.ag/v6"
SOL_MINT                   = "So11111111111111111111111111111111111111112"

# ─── LOGGING ───────────────────────────────────────────────────────────────────
LOG_LEVEL      = "INFO"
LOG_FILE       = "logs/pump_bot.log"
TRADE_LOG_FILE = "logs/trades.log"
