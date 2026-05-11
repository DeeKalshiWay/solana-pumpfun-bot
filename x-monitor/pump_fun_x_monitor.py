#!/usr/bin/env python3
"""
Pump.fun Meme Coin X (Twitter) Monitoring Agent
==============================================

A lightweight, production-ready scraping/monitoring agent that continuously
watches X for posts about meme coins launched on pump.fun that are
"actively being pumped".

Key Features:
- Uses official X API v2 (search_recent_tweets) - reliable & TOS compliant
- Filters for posts from accounts with >= 20,000 followers (client-side, since API v2 lacks native min_followers operator)
- Strong query for "actively pumped" signals: high engagement (min_faves) + hype keywords + direct pump.fun mentions
- Deduplication across runs (persisted seen tweet IDs)
- Extracts pump.fun links when present
- Optional Telegram instant alerts
- Logs all matches to JSONL for later analysis
- Handles rate limits gracefully
- Configurable via CLI args or environment variables

Setup Instructions:
1. Get a Bearer Token from https://developer.x.com/ (Essential or higher access recommended)
2. pip install -r requirements.txt
3. Export X_BEARER_TOKEN="your_token_here"   OR pass --bearer-token
4. (Optional) For Telegram alerts: create a bot via @BotFather and get token + your chat_id
5. Run: python pump_fun_x_monitor.py --min-followers 20000 --min-likes 30 --interval 300

Example run (every 5 minutes, high-signal pumps only):
    python pump_fun_x_monitor.py --min-faves-query 50 --min-likes 50 --interval 300 --log-file pumps.jsonl

The agent will print alerts to console and append structured data to the log file.
"""

import argparse
import json
import os
import time
from datetime import datetime

import requests
import tweepy

# ==================== CONFIG ====================
DEFAULT_QUERY = (
    '("pump.fun" OR pumpfun OR "pump fun" OR "pump.fun/" OR pump.fun/coin) '
    '(pump OR pumping OR moon OR mooning OR moonshot OR "next 100x" OR gem OR "ape in" OR "buy now" OR "going up" OR "volume exploding" OR parabolic OR shill OR "this is it") '
    'min_faves:30 lang:en -is:retweet'
)

SEEN_FILE = "seen_tweet_ids.json"
LOG_FILE_DEFAULT = "pump_fun_pumps.jsonl"


def load_seen_ids(filepath: str = SEEN_FILE) -> set[int]:
    """Load previously seen tweet IDs from disk (persists across restarts)."""
    if os.path.exists(filepath):
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("ids", []))
        except Exception:
            pass
    return set()


def save_seen_ids(seen: set[int], filepath: str = SEEN_FILE):
    """Persist seen IDs (keep only last ~5000 to avoid huge file)."""
    try:
        ids = sorted(list(seen))[-5000:]  # keep recent 5k
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "ids": ids,
                    "updated": datetime.now(datetime.UTC).isoformat(),
                },
                f,
                indent=2,
            )
    except Exception as e:
        print(f"[WARN] Could not save seen IDs: {e}")


def save_alerts(alerts: list[dict], log_file: str):
    """Append alerts to JSONL log file."""
    if not alerts:
        return
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            for alert in alerts:
                f.write(json.dumps(alert, ensure_ascii=False) + "\n")
        print(f"[LOG] Saved {len(alerts)} new alert(s) to {log_file}")
    except Exception as e:
        print(f"[ERROR] Failed to write log: {e}")


def send_telegram_alert(token: str, chat_id: str, alert: dict):
    """Send formatted alert to Telegram (optional)."""
    if not token or not chat_id:
        return
    try:
        text = (
            f"🚀 <b>PUMP.FUN ALERT</b> 🚀\n\n"
            f"<b>@{alert['author']}</b> ({alert['followers']:,} followers)\n"
            f"❤️ {alert['likes']:,} likes | 🔁 {alert['retweets']:,} RTs\n\n"
            f"{alert['text'][:400]}{'...' if len(alert['text']) > 400 else ''}\n\n"
            f"🔗 <a href=\"{alert['url']}\">View on X</a>"
        )
        if alert.get("pump_link"):
            text += f"\n\n💎 <a href=\"{alert['pump_link']}\">pump.fun link</a>"

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[TELEGRAM] Error: {resp.text}")
    except Exception as e:
        print(f"[TELEGRAM] Failed to send: {e}")


def print_alert(alert: dict):
    """Pretty print alert to console."""
    print("\n" + "=" * 70)
    print(f"🚀 NEW PUMP.FUN PUMP DETECTED @ {alert['timestamp']}")
    print(
        f"👤 @{alert['author']} ({alert['followers']:,} followers) | {alert['author_name']}"
    )
    print(f"❤️ {alert['likes']:,} likes | 🔁 {alert['retweets']:,} retweets")
    print(f"📝 {alert['text']}")
    print(f"🔗 {alert['url']}")
    if alert.get("pump_link"):
        print(f"💎 Pump.fun: {alert['pump_link']}")
    print("=" * 70 + "\n")


def extract_pump_link(tweet) -> str | None:
    """Extract first pump.fun link from tweet entities if present."""
    if not hasattr(tweet, "entities") or not tweet.entities:
        return None
    urls = tweet.entities.get("urls", [])
    for u in urls:
        expanded = u.get("expanded_url") or u.get("url", "")
        if "pump.fun" in expanded.lower():
            return expanded
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Monitor X for actively pumped pump.fun meme coins from big accounts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pump_fun_x_monitor.py
  python pump_fun_x_monitor.py --min-followers 50000 --min-likes 100 --interval 180
  python pump_fun_x_monitor.py --telegram-bot-token YOUR_BOT_TOKEN --telegram-chat-id YOUR_CHAT_ID
        """,
    )
    parser.add_argument(
        "--bearer-token",
        default=os.getenv("X_BEARER_TOKEN"),
        help="X API Bearer Token (or set X_BEARER_TOKEN env var)",
    )
    parser.add_argument(
        "--min-followers",
        type=int,
        default=20000,
        help="Minimum followers the posting account must have (default: 20000)",
    )
    parser.add_argument(
        "--min-likes",
        type=int,
        default=50,
        help="Minimum likes on the tweet (client-side filter, default: 50)",
    )
    parser.add_argument(
        "--min-faves-query",
        type=int,
        default=30,
        help="min_faves: value in the X search query (default: 30)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Polling interval in seconds (default: 300 = 5 minutes)",
    )
    parser.add_argument(
        "--log-file", default=LOG_FILE_DEFAULT, help="Path to JSONL log file for alerts"
    )
    parser.add_argument(
        "--seen-file", default=SEEN_FILE, help="Path to persist seen tweet IDs"
    )
    parser.add_argument(
        "--telegram-bot-token",
        default=os.getenv("TELEGRAM_BOT_TOKEN"),
        help="Telegram bot token for alerts (optional)",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=os.getenv("TELEGRAM_CHAT_ID"),
        help="Telegram chat ID to send alerts to (optional)",
    )
    parser.add_argument(
        "--query", default=DEFAULT_QUERY, help="Custom X search query (advanced users)"
    )

    args = parser.parse_args()

    if not args.bearer_token:
        print("ERROR: No Bearer Token provided. Get one at https://developer.x.com/")
        print("Set via --bearer-token or X_BEARER_TOKEN environment variable.")
        return

    # Build dynamic query with user-provided min_faves
    query = args.query.replace("min_faves:30", f"min_faves:{args.min_faves_query}")

    print("🚀 Starting Pump.fun X Monitor Agent")
    print(f"   Query: {query[:120]}...")
    print(f"   Min followers: {args.min_followers:,}")
    print(f"   Min likes (client): {args.min_likes}")
    print(f"   Poll every: {args.interval}s")
    print(f"   Log file: {args.log_file}")
    print(
        f"   Telegram alerts: {'ENABLED' if args.telegram_bot_token and args.telegram_chat_id else 'disabled'}"
    )
    print("-" * 70)

    client = tweepy.Client(
        bearer_token=args.bearer_token,
        wait_on_rate_limit=True,  # auto-handles some rate limits
        return_type=dict,  # easier handling
    )

    seen: set[int] = load_seen_ids(args.seen_file)
    print(f"[INFO] Loaded {len(seen)} previously seen tweet IDs")

    while True:
        try:
            print(
                f"\n[{datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}] Searching X..."
            )

            response = client.search_recent_tweets(
                query=query,
                max_results=50,
                tweet_fields=["created_at", "public_metrics", "text", "id", "entities"],
                user_fields=["username", "name", "public_metrics", "verified"],
                expansions=["author_id"],
                sort_order="recency",  # newest first
            )

            if not response or not response.get("data"):
                print("   No new tweets matching query.")
                time.sleep(args.interval)
                continue

            users = {
                u["id"]: u
                for u in response.get("includes", {}).get("users", [])
            }

            new_alerts: list[dict] = []

            for tweet in response["data"]:
                tweet_id = tweet["id"]
                if tweet_id in seen:
                    continue

                author = users.get(tweet.get("author_id"))
                if not author:
                    continue

                followers = author.get("public_metrics", {}).get(
                    "followers_count", 0
                )
                if followers < args.min_followers:
                    continue

                likes = tweet.get("public_metrics", {}).get("like_count", 0)
                if likes < args.min_likes:
                    continue

                # Passed all filters → new alert
                seen.add(tweet_id)

                alert = {
                    "timestamp": datetime.now(datetime.UTC).isoformat(),
                    "tweet_id": tweet_id,
                    "author": author.get("username"),
                    "author_name": author.get("name"),
                    "followers": followers,
                    "likes": likes,
                    "retweets": tweet.get("public_metrics", {}).get(
                        "retweet_count", 0
                    ),
                    "replies": tweet.get("public_metrics", {}).get("reply_count", 0),
                    "text": tweet.get("text", ""),
                    "url": f"https://x.com/{author.get('username')}/status/{tweet_id}",
                    "pump_link": extract_pump_link(tweet),
                    "created_at": tweet.get("created_at"),
                }

                new_alerts.append(alert)
                print_alert(alert)

                # Optional Telegram push
                if args.telegram_bot_token and args.telegram_chat_id:
                    send_telegram_alert(
                        args.telegram_bot_token, args.telegram_chat_id, alert
                    )

            if new_alerts:
                save_alerts(new_alerts, args.log_file)
                save_seen_ids(seen, args.seen_file)

            print(f"   Cycle complete. Next search in {args.interval} seconds...")
            time.sleep(args.interval)

        except tweepy.TooManyRequests as e:
            reset_time = getattr(e, "reset_in", 900)
            print(f"[RATE LIMIT] Sleeping for {reset_time} seconds...")
            time.sleep(reset_time + 10)
        except tweepy.TweepyException as e:
            print(f"[TWEEPY ERROR] {e}")
            time.sleep(60)
        except KeyboardInterrupt:
            print("\n🛑 Shutting down gracefully...")
            save_seen_ids(seen, args.seen_file)
            break
        except Exception as e:
            print(f"[UNEXPECTED ERROR] {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
