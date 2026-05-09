"""
config.py — Pump Bot configuration.

Two layers:
  - In-code constants below = CONSERVATIVE STARTER defaults. Anyone
    who clones the repo can run the bot safely with these.
  - Anything wrapped in `_env_*("NAME", default)` reads from the local
    `.env` file (gitignored). The operator's tuned values live there.

This split exists so the public repo demonstrates the *machinery*
without leaking the *exact tuned numbers* that the operator uses
when actually running. Changing a tuned value = edit `.env`, no
git commit needed.
"""

import json
import os

from dotenv import load_dotenv

load_dotenv(override=True)


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_json(name: str, default):
    v = os.getenv(name)
    if not v:
        return default
    try:
        return json.loads(v)
    except (json.JSONDecodeError, ValueError):
        return default

# ─── PAPER TRADING ─────────────────────────────────────────────────────────────
PAPER_TRADING       = False   # Set False to trade with real money
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

# Telegram channels to monitor for token calls. Provide as JSON list in .env:
#   TELEGRAM_CHANNELS=["@channel1","@channel2","@private_chat_invite_hash"]
# Public channels: use @username form. Private: use the t.me/+HASH portion.
def _load_telegram_channels() -> list:
    raw = os.getenv("TELEGRAM_CHANNELS", "").strip()
    if not raw:
        return []
    try:
        import json as _json
        v = _json.loads(raw)
        return list(v) if isinstance(v, list) else []
    except Exception:
        # Comma-separated fallback for "@a,@b" style values
        return [s.strip() for s in raw.split(",") if s.strip()]


TELEGRAM_CHANNELS = _load_telegram_channels()
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
# Defaults are CONSERVATIVE STARTER values — operator's tuned values live
# in `.env` (gitignored). See `.env.example` for the full list.
MIN_BUY_SCORE         = _env_int("MIN_BUY_SCORE",         60)
MIN_BONDING_CURVE_PCT = _env_float("MIN_BONDING_CURVE_PCT", 30.0)
MAX_BONDING_CURVE_PCT = _env_float("MAX_BONDING_CURVE_PCT", 80.0)
BUY_COOLDOWN_SECONDS  = _env_int("BUY_COOLDOWN_SECONDS",  15)

# Counterfactual analysis (2026-05-08): tokens with creator initial buys
# >= 1.5 SOL rugged at ~91% rate within 10 min, while tokens with init buys
# < 0.30 SOL produced the bulk of the +100% pumps and 100% of the
# moonshots in the rejected sample. Big initial buy = creator bag dump risk.
# Hard filter at this threshold; the scoring also inverts to reward small.
MAX_INITIAL_BUY_SOL   = _env_float("MAX_INITIAL_BUY_SOL",  1.5)

# ─── CREATOR TRACKING (Dexter strategy) ───────────────────────────────────────
CREATOR_TRACKING_ENABLED = _env_bool("CREATOR_TRACKING_ENABLED", True)
CREATOR_TOP_N            = _env_int("CREATOR_TOP_N",            50)
CREATOR_MIN_LAUNCHES     = _env_int("CREATOR_MIN_LAUNCHES",      2)
CREATOR_DB_FILE          = "logs/creators.json"

# ─── RISK MANAGEMENT ───────────────────────────────────────────────────────────
# Defaults below assume a small starter wallet (~1 SOL) and bias toward safety.
# Operators with tuned values override via `.env`.
MAX_SOL_PER_TRADE      = _env_float("MAX_SOL_PER_TRADE",      0.05)
MAX_POSITION_PCT       = _env_float("MAX_POSITION_PCT",       0.05)
MAX_TOTAL_EXPOSURE_SOL = _env_float("MAX_TOTAL_EXPOSURE_SOL", 0.50)
MAX_OPEN_POSITIONS     = _env_int("MAX_OPEN_POSITIONS",         3)

# Adaptive position sizing (Kelly lite):
# Looks at win rate of last N closed trades and scales position size.
# Hot streak = bet bigger. Cold streak = bet smaller.
ADAPTIVE_SIZING_ENABLED = _env_bool("ADAPTIVE_SIZING_ENABLED", True)
ADAPTIVE_LOOKBACK       = _env_int("ADAPTIVE_LOOKBACK",        20)
ADAPTIVE_HOT_WR         = _env_float("ADAPTIVE_HOT_WR",         0.55)
ADAPTIVE_HOT_MULT       = _env_float("ADAPTIVE_HOT_MULT",       1.25)
ADAPTIVE_COLD_WR        = _env_float("ADAPTIVE_COLD_WR",        0.30)
ADAPTIVE_COLD_MULT      = _env_float("ADAPTIVE_COLD_MULT",      0.50)
ADAPTIVE_HARD_CAP_MULT  = _env_float("ADAPTIVE_HARD_CAP_MULT",  1.50)

# Take-profit ladder. Default = balanced 50/100/200% with 25/25/25 sells
# (50% rides for moonshot via trailing stop).
TAKE_PROFIT_LEVELS = _env_json("TAKE_PROFIT_LEVELS", [
    {"gain_pct": 50,  "sell_pct": 25},
    {"gain_pct": 100, "sell_pct": 25},
    {"gain_pct": 200, "sell_pct": 25},
])

# Trailing stop — primary "lock in winners" mechanism. Inactive until peak
# crosses TRAILING_STOP_MIN_PROFIT.
TRAILING_STOP_ENABLED          = _env_bool("TRAILING_STOP_ENABLED",          True)
TRAILING_STOP_MIN_PROFIT       = _env_float("TRAILING_STOP_MIN_PROFIT",      20.0)
TRAILING_STOP_PCT              = _env_float("TRAILING_STOP_PCT",             15.0)
TRAILING_STOP_MOONSHOT_PCT     = _env_float("TRAILING_STOP_MOONSHOT_PCT",    25.0)
TRAILING_STOP_MOONSHOT_TRIGGER = _env_float("TRAILING_STOP_MOONSHOT_TRIGGER", 100.0)

STOP_LOSS_PCT               = _env_float("STOP_LOSS_PCT",               15.0)
EMERGENCY_STOP_DRAWDOWN_PCT = _env_float("EMERGENCY_STOP_DRAWDOWN_PCT", 25.0)
TIME_EXIT_MINUTES           = _env_int("TIME_EXIT_MINUTES",              5)

# Early-rug detector: if a fresh position drops past EARLY_RUG_PCT within
# EARLY_RUG_WINDOW_SEC, exit immediately — don't wait for the regular stop-loss
# check. Trade-DB analysis showed 0-2 min holds had 11-33% WR with stop-losses
# routinely closing at -25% to -50% due to sell-side slippage during a dump.
# Faster exit on the first sign of a rug saves the bulk of that slippage.
EARLY_RUG_PCT        = _env_float("EARLY_RUG_PCT",         5.0)
EARLY_RUG_WINDOW_SEC = _env_int  ("EARLY_RUG_WINDOW_SEC",  60)

# Smart Caller: when scoring rejects a token in the borderline band, write it
# to logs/candidate_queue.jsonl so the control bot can post it for manual
# yes/no review. Range is [SMART_CALLER_MIN, MIN_BUY_SCORE-1].
SMART_CALLER_MIN = _env_int("SMART_CALLER_MIN", 20)

# Creators that produced rugs we've already lost on, OR rugged after we
# rejected their token. Block at trade-loop level so we never enter again.
# The JSON file is rebuilt by `python -m tools.build_rugger_blacklist`
# from trades.db + counterfactual.jsonl + creators.json.
def _load_rugger_creators() -> set[str]:
    import json as _json
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "logs", "rugger_creators.json")
    if not _os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as _f:
            data = _json.load(_f)
        return set(data.get("creators", []))
    except Exception:
        return set()


CREATOR_BLACKLIST = {
    "ETroz4qu4C6E9HJvYx8G3RjwwhtffSLaBy3yPjYm8THL",  # 4 trades, 0 wins, -0.020 SOL (2026-05-08)
} | _load_rugger_creators()

# No-movement exit: if position hasn't moved either direction in N seconds.
NO_MOVEMENT_EXIT_SECONDS = _env_int("NO_MOVEMENT_EXIT_SECONDS", 90)
NO_MOVEMENT_BAND_PCT     = _env_float("NO_MOVEMENT_BAND_PCT",   3.0)

# ─── TIER 2 GUARDRAILS ─────────────────────────────────────────────────────────
LOSS_STREAK_LIMIT      = _env_int("LOSS_STREAK_LIMIT",      5)
LOSS_STREAK_PAUSE_MIN  = _env_int("LOSS_STREAK_PAUSE_MIN", 15)

# Daily PnL bands. If hit, pause all new buys for the rest of the UTC day.
DAILY_LOSS_LIMIT_PCT  = _env_float("DAILY_LOSS_LIMIT_PCT",   20.0)
DAILY_PROFIT_LOCK_PCT = _env_float("DAILY_PROFIT_LOCK_PCT", 9999.0)

# Momentum-stall exit.
MOMENTUM_STALL_ENABLED    = _env_bool("MOMENTUM_STALL_ENABLED",      True)
MOMENTUM_STALL_PCT_BAND   = _env_float("MOMENTUM_STALL_PCT_BAND",     5.0)
MOMENTUM_STALL_WINDOW_SEC = _env_int("MOMENTUM_STALL_WINDOW_SEC",    60)
MOMENTUM_STALL_MIN_PROFIT = _env_float("MOMENTUM_STALL_MIN_PROFIT",   5.0)

# Holder concentration filter (rug indicator). Calls getTokenLargestAccounts.
# Skip a token if top 10 holders own more than this % of supply.
# Set 0 to disable.
HOLDER_CONCENTRATION_LIMIT_PCT = 70.0

# ─── EXECUTION SETTINGS ────────────────────────────────────────────────────────
SLIPPAGE_BPS               = 1500   # 15% — pump.fun tokens need wide slippage
PRIORITY_FEE_MICROLAMPORTS = 300000 # Jupiter/RPC fee
PRIORITY_FEE_SOL           = _env_float("PRIORITY_FEE_SOL",      0.001)
# Sells during a dump need to land FAST or the price moves further. Stop-loss
# slippage analysis showed -10% configured stops closing at -25 to -50% because
# the sell tx was sitting in mempool while the rug deepened. 5x the buy fee
# pays for priority during high-volatility exits.
SELL_PRIORITY_FEE_SOL      = _env_float("SELL_PRIORITY_FEE_SOL", 0.005)
JUPITER_API_URL            = "https://quote-api.jup.ag/v6"
SOL_MINT                   = "So11111111111111111111111111111111111111112"

# ─── LOGGING ───────────────────────────────────────────────────────────────────
LOG_LEVEL      = "INFO"
LOG_FILE       = "logs/pump_bot.log"
TRADE_LOG_FILE = "logs/trades.log"
