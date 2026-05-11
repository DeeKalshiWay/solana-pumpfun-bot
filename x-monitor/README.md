# Pump.fun X Monitoring Agent

A powerful, easy-to-run Python agent that monitors X (Twitter) in real-time for **meme coins on pump.fun that are actively being pumped** by accounts with **≥20k followers**.

## Why this agent?
- Pump.fun launches thousands of meme coins daily. The ones that "pump" hard are usually shilled by big accounts (KOLs/influencers).
- This agent filters for **high-signal** posts only:
  - Direct mentions of `pump.fun`
  - Strong hype language ("pumping", "mooning", "100x", "ape in", etc.)
  - High engagement (`min_faves:30+` in query + client-side `min_likes:50+`)
  - **Only accounts with 20,000+ followers**
- Perfect for traders who want early signals without noise from bot accounts or low-follower spam.

## Quick Start

### 1. Prerequisites
- Python 3.8+
- X Developer account with **Bearer Token** (free Essential tier works)
  - Go to https://developer.x.com/ → sign in → create a Project/App → copy **Bearer Token**

### 2. Install
```bash
cd /path/to/artifacts
pip install -r requirements.txt
```

### 3. Run the agent
```bash
# Basic (every 5 min, 20k followers, 50+ likes)
python pump_fun_x_monitor.py

# Stronger signals (50k followers, 100+ likes, every 3 min)
python pump_fun_x_monitor.py --min-followers 50000 --min-likes 100 --interval 180

# With Telegram instant notifications (recommended)
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
export TELEGRAM_CHAT_ID="123456789"
python pump_fun_x_monitor.py --telegram-bot-token $TELEGRAM_BOT_TOKEN --telegram-chat-id $TELEGRAM_CHAT_ID
```

### 4. Environment Variables (recommended)
Create a `.env` file:
```
X_BEARER_TOKEN=your_bearer_token_here
TELEGRAM_BOT_TOKEN=optional_bot_token
TELEGRAM_CHAT_ID=optional_chat_id
```

Then just run:
```bash
python pump_fun_x_monitor.py
```

## Output
- **Console**: Beautiful formatted alerts with author, followers, engagement, full text, and direct links.
- **JSONL Log** (`pump_fun_pumps.jsonl`): Machine-readable history of every alert (great for backtesting or dashboards).
- **Telegram** (optional): Instant push notifications with clickable links.

## How the filtering works
1. **X API v2 Search** with optimized query:
   - Keywords: `pump.fun` + pumping signals
   - `min_faves:30+` (server-side engagement filter)
   - English only, no retweets
2. **Client-side filters** (because X API v2 doesn't support `min_followers:`):
   - Author has ≥20,000 followers
   - Tweet has ≥50 likes (configurable)
3. **Deduplication**: Never alerts twice for the same tweet (persisted across restarts).

## Customization
Edit the `DEFAULT_QUERY` in the script or pass `--query "your custom query"` for advanced use.

Example custom query ideas:
- Add specific tickers or themes: `("pump.fun" $PEPE OR $DOGE) ...`
- Focus on new launches: `("just launched" OR "new coin" OR "fresh pump") pump.fun ...`

## Rate Limits & Best Practices
- X API Essential tier: ~450 requests / 15 min for recent search.
- Default 5-minute interval = ~288 calls/day → well within limits.
- The agent uses `wait_on_rate_limit=True` and handles `TooManyRequests` gracefully.

## Disclaimer
This is a **monitoring tool only**. Meme coins are extremely risky and most go to zero. Always DYOR, never ape in with money you can't afford to lose. The agent surfaces signals — you decide what to do with them.

## Files
- `pump_fun_x_monitor.py` — The full agent (run this)
- `requirements.txt` — Dependencies
- `pump_fun_pumps.jsonl` — Generated log (created on first run)
- `seen_tweet_ids.json` — Internal dedup file (created automatically)

Happy hunting! 🚀💎

*Built with ❤️ for degens who want an edge on pump.fun.*