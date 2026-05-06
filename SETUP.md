# ─── PUMP BOT SETUP GUIDE ────────────────────────────────────────────────────
#
# STEP 1: INSTALL PYTHON
#   https://python.org — install Python 3.11+
#   Verify: python --version
#
# STEP 2: CLONE/COPY PROJECT
#   Put the pump_bot/ folder on your machine or VPS
#
# STEP 3: CREATE VIRTUAL ENVIRONMENT (recommended)
#   cd pump_bot
#   python -m venv venv
#   source venv/bin/activate          # Mac/Linux
#   venv\Scripts\activate             # Windows
#
# STEP 4: INSTALL DEPENDENCIES
#   pip install -r requirements.txt
#   playwright install chromium       # Only needed if using browser scraping
#
# STEP 5: CREATE YOUR .env FILE
#   Copy this file to .env and fill in your values:
#   cp .env.example .env
#
# STEP 6: CONFIGURE RISK SETTINGS
#   Edit config.py — especially:
#     MAX_SOL_PER_TRADE    (start small, e.g. 0.05)
#     MIN_BUY_SCORE        (65 is balanced, raise to 75 for more conservative)
#     STOP_LOSS_PCT        (25% default)
#
# STEP 7: RUN
#   python main.py
#
# STEP 8: RUN 24/7 ON A VPS (Linux)
#   # Install screen or use systemd
#   screen -S pumpbot
#   python main.py
#   # Detach: Ctrl+A then D
#   # Reattach: screen -r pumpbot
#
#   OR with systemd — create /etc/systemd/system/pumpbot.service:
#   [Unit]
#   Description=Pump Bot
#   After=network.target
#   [Service]
#   WorkingDirectory=/home/user/pump_bot
#   ExecStart=/home/user/pump_bot/venv/bin/python main.py
#   Restart=always
#   [Install]
#   WantedBy=multi-user.target
#
#   Then: systemctl enable pumpbot && systemctl start pumpbot
#
# ── API KEYS YOU NEED ─────────────────────────────────────────────────────────
#
# SOLANA WALLET:
#   Export your private key from Phantom:
#   Settings > Security & Privacy > Export Private Key
#   USE A DEDICATED WALLET — never use your main wallet
#   Only fund it with what you're willing to lose
#
# RPC (REQUIRED for speed — free RPC is too slow):
#   Helius (recommended): https://helius.dev — free tier available
#   QuickNode:            https://quicknode.com
#   Triton:               https://triton.one
#   Replace mainnet-beta URL with your paid endpoint
#
# TWITTER/X (optional but recommended):
#   developer.twitter.com > Create App > Bearer Token
#   Free tier: 500k tweet reads/month
#
# TELEGRAM (optional):
#   my.telegram.org > API Development Tools
#   Get api_id and api_hash
#   First run will ask you to verify your phone number
#
# BIRDEYE (optional — adds rug detection):
#   birdeye.so > Settings > API Key

# ─── .env FILE CONTENTS (copy below into your .env) ──────────────────────────

SOLANA_PRIVATE_KEY=your_base58_private_key_here

# Paid RPC is STRONGLY recommended — free nodes will lag
RPC_URL=https://api.mainnet-beta.solana.com
HELIUS_API_KEY=

TWITTER_BEARER_TOKEN=
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=+1234567890
BIRDEYE_API_KEY=

# ─── VPS RECOMMENDATIONS ─────────────────────────────────────────────────────
#
# For 24/7 operation, rent a cheap VPS:
#   Hetzner CX22:  ~$5/mo  (2 vCPU, 4GB RAM) — plenty for this bot
#   DigitalOcean:  ~$6/mo  Basic Droplet
#   Vultr:         ~$6/mo  Cloud Compute
#
# Choose a datacenter close to Solana validators (US East/West or EU)
# for lowest latency on RPC calls.
#
# ─── LOGS ────────────────────────────────────────────────────────────────────
#
# logs/pump_bot.log  — Full activity log (all events)
# logs/trades.log    — Trade-only log (buys, sells, positions)
#
# Watch live:  tail -f logs/pump_bot.log
#
# ─── SAFETY NOTES ────────────────────────────────────────────────────────────
#
# 1. START SMALL. Set MAX_SOL_PER_TRADE=0.05 for first few days.
# 2. The bot will lose on many trades — meme coins are high risk.
# 3. EMERGENCY_STOP_DRAWDOWN_PCT=40 will halt if portfolio drops 40%.
# 4. Monitor the dashboard and logs actively at first.
# 5. Keep most of your SOL in a separate wallet — only fund the bot wallet.
# 6. The bot does NOT guarantee profits. Use at your own risk.
