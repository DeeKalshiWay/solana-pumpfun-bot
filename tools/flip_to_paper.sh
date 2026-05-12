#!/usr/bin/env bash
# tools/flip_to_paper.sh
#
# One-shot flip from LIVE → PAPER on a running VPS. Atomic, idempotent,
# reversible (.env is backed up). Designed to be safe to run twice; the
# second run is a no-op when already in paper mode.
#
# What it does:
#   1. Detects the bot process and process manager (systemd / docker / nohup)
#   2. Stops the bot cleanly (SIGINT, 10s grace)
#   3. Backs up .env → .env.bak.<timestamp>
#   4. Sets PAPER_TRADING=true and PAPER_STARTING_SOL=<value> in .env
#   5. Wipes logs/paper_wallet.json so the new paper run starts at the
#      configured balance (does NOT touch trades.db or closed_trades.jsonl —
#      keeping the historical signal for the learning loops)
#   6. Clears emergency_stop_active in logs/risk_state.json so the paper
#      bot can trade (no real money at risk in paper mode)
#   7. Restarts the bot via whichever manager it found
#   8. Probes the dashboard to confirm paper_trading=true
#
# Usage on your VPS:
#   cd <bot-dir>
#   ./tools/flip_to_paper.sh                    # default 2.5 SOL paper seed
#   PAPER_STARTING_SOL=1.0 ./tools/flip_to_paper.sh
#
# Reversal:
#   mv .env.bak.<timestamp> .env && systemctl restart pump_bot  (or equivalent)

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(pwd)}"
PAPER_STARTING_SOL="${PAPER_STARTING_SOL:-2.5}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"
DASHBOARD_AUTH_USER="${DASHBOARD_AUTH_USER:-admin}"
DASHBOARD_AUTH_PASS="${DASHBOARD_AUTH_PASS:-}"

c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'; c_red=$'\033[1;31m'; c_reset=$'\033[0m'
log()  { printf '%s▸%s %s\n' "$c_green"  "$c_reset" "$*"; }
warn() { printf '%s!%s %s\n' "$c_yellow" "$c_reset" "$*"; }
die()  { printf '%s✗%s %s\n' "$c_red"    "$c_reset" "$*" >&2; exit 1; }

cd "$REPO_DIR"
[[ -d .git ]] || die "Run this from the bot's git repo root. cd into your bot dir first."

# ── 1. Detect process manager ───────────────────────────────────────────────
MANAGER=""
SERVICE_NAME=""

if command -v systemctl >/dev/null && systemctl list-units --type=service --all 2>/dev/null \
     | grep -qE 'pump[_-]?bot|trading[_-]?bot'; then
  SERVICE_NAME=$(systemctl list-units --type=service --all --no-legend 2>/dev/null \
                  | awk '{print $1}' | grep -iE 'pump[_-]?bot|trading[_-]?bot' | head -1)
  MANAGER="systemd"
  log "Detected systemd unit: $SERVICE_NAME"
elif [[ -f docker-compose.yml || -f compose.yml ]] && command -v docker >/dev/null \
     && docker compose ps --status running 2>/dev/null | grep -q .; then
  MANAGER="docker"
  log "Detected docker compose"
elif pgrep -f 'python.*main\.py' >/dev/null; then
  MANAGER="nohup"
  log "Detected raw python process (nohup/screen)"
else
  warn "No running bot process detected. Will edit config and start fresh."
  MANAGER="cold-start"
fi

# ── 2. Stop the bot cleanly ─────────────────────────────────────────────────
case "$MANAGER" in
  systemd)
    log "Stopping $SERVICE_NAME"
    sudo systemctl stop "$SERVICE_NAME"
    ;;
  docker)
    log "Stopping compose services"
    docker compose stop
    ;;
  nohup)
    log "Sending SIGINT to main.py"
    pkill -INT -f 'python.*main\.py' || true
    for _ in $(seq 1 20); do
      pgrep -f 'python.*main\.py' >/dev/null || break
      sleep 0.5
    done
    if pgrep -f 'python.*main\.py' >/dev/null; then
      warn "Bot didn't exit in 10s — sending SIGTERM"
      pkill -TERM -f 'python.*main\.py' || true
      sleep 2
    fi
    ;;
  cold-start)
    : # nothing to stop
    ;;
esac

# ── 3. Backup .env ──────────────────────────────────────────────────────────
TS=$(date +%Y%m%d_%H%M%S)
if [[ -f .env ]]; then
  cp .env ".env.bak.$TS"
  log "Backed up .env → .env.bak.$TS"
else
  warn "No .env present — creating a minimal one"
  touch .env
fi

# ── 4. Edit .env atomically ─────────────────────────────────────────────────
ENV_TMP=$(mktemp)
trap 'rm -f "$ENV_TMP"' EXIT

# Preserve every existing line EXCEPT the two keys we're managing.
grep -v '^PAPER_TRADING=' .env 2>/dev/null | grep -v '^PAPER_STARTING_SOL=' > "$ENV_TMP" || true
{
  echo ""
  echo "# Flipped to paper by tools/flip_to_paper.sh at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "PAPER_TRADING=true"
  echo "PAPER_STARTING_SOL=$PAPER_STARTING_SOL"
} >> "$ENV_TMP"
mv "$ENV_TMP" .env
trap - EXIT
log "Set PAPER_TRADING=true · PAPER_STARTING_SOL=$PAPER_STARTING_SOL"

# ── 5. Wipe stale paper-wallet state ────────────────────────────────────────
# Leave trades.db + closed_trades.jsonl + creator/rug/wallet intel intact —
# the learning loops benefit from history. We only zero the wallet itself
# so the new paper run starts at PAPER_STARTING_SOL, not a stale carryover.
if [[ -f logs/paper_wallet.json ]]; then
  cp logs/paper_wallet.json "logs/paper_wallet.json.bak.$TS"
  rm -f logs/paper_wallet.json
  log "Cleared stale paper_wallet.json (backup: logs/paper_wallet.json.bak.$TS)"
fi

# ── 6. Clear emergency_stop_active in risk_state.json ───────────────────────
# Paper mode has no real money to protect; the breaker should NOT carry
# over from live. Use python so we preserve original_starting_sol +
# any other keys without trampling them.
if [[ -f logs/risk_state.json ]]; then
  python3 - <<'PY'
import json, os, time
path = "logs/risk_state.json"
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except Exception as e:
    print(f"  ! could not parse {path}: {e} — skipping clear")
    raise SystemExit(0)
data["emergency_stop_active"] = False
data["emergency_force_sell"]  = False
data["saved_at"] = time.time()
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, path)
print("  ▸ Cleared emergency_stop_active + emergency_force_sell")
PY
fi

# ── 7. Restart ──────────────────────────────────────────────────────────────
case "$MANAGER" in
  systemd)
    log "Starting $SERVICE_NAME"
    sudo systemctl start "$SERVICE_NAME"
    ;;
  docker)
    log "Starting compose services"
    docker compose up -d
    ;;
  nohup|cold-start)
    log "Starting main.py via nohup (logs → bot.log)"
    nohup python main.py > bot.log 2>&1 &
    disown || true
    ;;
esac

# ── 8. Probe ────────────────────────────────────────────────────────────────
log "Waiting up to 30s for the dashboard to respond..."
PROBE_URL="http://127.0.0.1:$DASHBOARD_PORT/api/status"
CURL_AUTH=()
[[ -n "$DASHBOARD_AUTH_PASS" ]] && CURL_AUTH=(-u "$DASHBOARD_AUTH_USER:$DASHBOARD_AUTH_PASS")

OK=0
for _ in $(seq 1 60); do
  if curl -fs "${CURL_AUTH[@]}" "$PROBE_URL" >/dev/null 2>&1; then
    OK=1; break
  fi
  sleep 0.5
done

if [[ "$OK" -eq 0 ]]; then
  warn "Dashboard didn't respond on :$DASHBOARD_PORT within 30s."
  warn "Check the bot's log (systemctl status / docker compose logs / bot.log)."
  exit 2
fi

STATUS=$(curl -s "${CURL_AUTH[@]}" "$PROBE_URL")
PAPER=$(echo "$STATUS" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("paper_trading"))' 2>/dev/null || echo "?")
BAL=$(echo "$STATUS" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("balance_sol"))' 2>/dev/null || echo "?")

BAR='━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
printf '\n%s%s%s\n' "$c_green" "$BAR" "$c_reset"
if [[ "$PAPER" == "True" ]]; then
  printf '  %s✓ FLIPPED TO PAPER%s\n' "$c_green" "$c_reset"
else
  printf '  %s✗ FLIP DID NOT TAKE%s  (paper_trading=%s)\n' "$c_red" "$c_reset" "$PAPER"
fi
printf '  Balance:    %s SOL  (configured start: %s)\n' "$BAL" "$PAPER_STARTING_SOL"
printf '  Dashboard:  http://127.0.0.1:%s/\n' "$DASHBOARD_PORT"
printf '  Reverse:    mv .env.bak.%s .env && (restart command)\n' "$TS"
printf '%s%s%s\n' "$c_green" "$BAR" "$c_reset"

[[ "$PAPER" == "True" ]] && exit 0 || exit 1
