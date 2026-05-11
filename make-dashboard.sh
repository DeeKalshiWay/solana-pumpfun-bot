#!/usr/bin/env bash
# make-dashboard.sh — clone, install, seed, and serve the paper dashboard
# in one shot. Safe to re-run; each step short-circuits if already done.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/DeeKalshiWay/solana-pumpfun-bot/main/make-dashboard.sh | bash
#   # or
#   ./make-dashboard.sh
#
# What it does:
#   1. Clones (or pulls) the repo into ./solana-pumpfun-bot
#   2. Creates a venv at .venv
#   3. Installs requirements (skipped if already installed)
#   4. Seeds the dashboard with 6 synthetic trades for non-empty panels
#   5. Starts the bot in paper mode at PAPER_STARTING_SOL
#   6. Opens http://127.0.0.1:8765/ in the default browser
#
# Env overrides:
#   REPO_DIR              default: ./solana-pumpfun-bot
#   BRANCH                default: main
#   PAPER_STARTING_SOL    default: 2.5
#   DASHBOARD_PORT        default: 8765
#   DASHBOARD_AUTH_USER   default: admin
#   DASHBOARD_AUTH_PASS   default: test123  (CHANGE THIS for non-loopback)

set -euo pipefail

REPO_URL="https://github.com/DeeKalshiWay/solana-pumpfun-bot"
REPO_DIR="${REPO_DIR:-solana-pumpfun-bot}"
BRANCH="${BRANCH:-main}"
PAPER_STARTING_SOL="${PAPER_STARTING_SOL:-2.5}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"
DASHBOARD_AUTH_USER="${DASHBOARD_AUTH_USER:-admin}"
DASHBOARD_AUTH_PASS="${DASHBOARD_AUTH_PASS:-test123}"

c_green='\033[1;32m'; c_yellow='\033[1;33m'; c_red='\033[1;31m'; c_dim='\033[0;90m'; c_reset='\033[0m'
log()  { printf "${c_green}▸${c_reset} %s\n" "$*"; }
warn() { printf "${c_yellow}!${c_reset} %s\n" "$*"; }
die()  { printf "${c_red}✗${c_reset} %s\n" "$*" >&2; exit 1; }

# ── 1. Clone or pull ────────────────────────────────────────────────────────
if [[ -d "$REPO_DIR/.git" ]]; then
  log "Repo present — pulling $BRANCH"
  git -C "$REPO_DIR" fetch origin "$BRANCH" --quiet
  git -C "$REPO_DIR" checkout "$BRANCH" --quiet
  git -C "$REPO_DIR" pull --ff-only origin "$BRANCH" --quiet
else
  log "Cloning $REPO_URL into $REPO_DIR"
  git clone --branch "$BRANCH" --depth 30 "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"

# ── 2. Venv ─────────────────────────────────────────────────────────────────
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || die "$PYTHON not found. Install Python 3.12+ first."
PY_VER=$("$PYTHON" -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
case "$PY_VER" in
  3.11|3.12|3.13) ;;
  *) warn "Python $PY_VER may be too old; project targets 3.12. Continuing anyway." ;;
esac

if [[ ! -d .venv ]]; then
  log "Creating venv (.venv)"
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ── 3. Install deps (cache via marker file) ─────────────────────────────────
REQ_MARKER=".venv/.requirements.installed"
REQ_HASH=$(sha256sum requirements.txt 2>/dev/null | awk '{print $1}' || shasum -a 256 requirements.txt | awk '{print $1}')
if [[ ! -f "$REQ_MARKER" ]] || [[ "$(cat "$REQ_MARKER" 2>/dev/null)" != "$REQ_HASH" ]]; then
  log "Installing requirements (this may take a minute)"
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
  echo "$REQ_HASH" > "$REQ_MARKER"
else
  log "Requirements already installed (cached)"
fi

# ── 4. Stop any existing bot on this port, then seed ────────────────────────
if lsof -tiTCP:"$DASHBOARD_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  warn "Something is already listening on :$DASHBOARD_PORT — stopping it"
  lsof -tiTCP:"$DASHBOARD_PORT" -sTCP:LISTEN | xargs -r kill -INT
  sleep 2
fi

mkdir -p logs
log "Resetting paper state and seeding 6 synthetic trades"
rm -f logs/paper_wallet.json logs/risk_state.json logs/symbol_deployed.json \
      logs/trades.db logs/closed_trades.jsonl

PAPER_STARTING_SOL="$PAPER_STARTING_SOL" python -m tools.seed_dashboard >/dev/null

# ── 5. Start the bot in the background ──────────────────────────────────────
LOG_FILE="logs/dashboard_run.log"
log "Starting bot in paper mode at $PAPER_STARTING_SOL SOL"
PAPER_TRADING=1 \
PAPER_STARTING_SOL="$PAPER_STARTING_SOL" \
DASHBOARD_AUTH_USER="$DASHBOARD_AUTH_USER" \
DASHBOARD_AUTH_PASS="$DASHBOARD_AUTH_PASS" \
nohup python main.py > "$LOG_FILE" 2>&1 &
BOT_PID=$!
echo "$BOT_PID" > .dashboard.pid

# Wait for the HTTP server to come up (max 30s).
for i in $(seq 1 60); do
  if curl -fs -u "$DASHBOARD_AUTH_USER:$DASHBOARD_AUTH_PASS" \
       "http://127.0.0.1:$DASHBOARD_PORT/api/status" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! curl -fs -u "$DASHBOARD_AUTH_USER:$DASHBOARD_AUTH_PASS" \
       "http://127.0.0.1:$DASHBOARD_PORT/api/status" >/dev/null 2>&1; then
  warn "Bot didn't respond on :$DASHBOARD_PORT within 30s. Tail of $LOG_FILE:"
  tail -30 "$LOG_FILE"
  die "Aborting"
fi

# ── 6. Print connection info and open the browser ───────────────────────────
URL="http://127.0.0.1:$DASHBOARD_PORT/"
printf "\n${c_green}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${c_reset}\n"
printf "  ${c_green}DASHBOARD READY${c_reset}\n\n"
printf "  URL:       %s\n" "$URL"
printf "  Username:  %s\n" "$DASHBOARD_AUTH_USER"
printf "  Password:  %s\n" "$DASHBOARD_AUTH_PASS"
printf "  Mode:      PAPER ($PAPER_STARTING_SOL SOL starting)\n"
printf "  Logs:      tail -f %s/%s\n" "$REPO_DIR" "$LOG_FILE"
printf "  Stop:      kill \$(cat %s/.dashboard.pid)\n" "$REPO_DIR"
printf "${c_green}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${c_reset}\n\n"

# Try to open the URL in the default browser.
if   command -v open       >/dev/null; then open "$URL"
elif command -v xdg-open   >/dev/null; then xdg-open "$URL" >/dev/null 2>&1
elif command -v start      >/dev/null; then start "$URL"
fi
