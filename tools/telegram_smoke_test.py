"""
tools/telegram_smoke_test.py

One-shot connectivity check for the Telegram screener.

What it does:
  1. Loads TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE / TELEGRAM_CHANNELS from .env
  2. Connects via Telethon (will SMS-prompt on first run only)
  3. Listens to the configured channels for 60 seconds
  4. For each message: prints the channel, the mints + tickers extracted,
     and the hype score that would feed the bot's signal_scorer

Run with:
  python -m tools.telegram_smoke_test            # 60-second window
  python -m tools.telegram_smoke_test --secs 300 # custom window

First-run setup:
  1. Go to https://my.telegram.org/auth — create an app, get api_id + api_hash
  2. Edit .env:
       TELEGRAM_API_ID=12345678
       TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
       TELEGRAM_PHONE=+15551234567
       TELEGRAM_CHANNELS=["@some_pump_calls","@another_alpha_chat"]
  3. Run this script. It will SMS-prompt for the code on first connect, then
     create a `pump_bot_session` file you can keep using.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(override=True)

from config import (  # noqa: E402
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_CHANNELS,
    TELEGRAM_PHONE,
)
from detector.social_monitor import (  # noqa: E402
    extract_mints_from_text,
    score_hype_text,
)


async def main(seconds: int) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        print("✗ Missing TELEGRAM_API_ID or TELEGRAM_API_HASH in .env")
        print("  Visit https://my.telegram.org/auth to create an app.")
        return 1
    if not TELEGRAM_CHANNELS:
        print("✗ TELEGRAM_CHANNELS is empty.")
        print('  Set TELEGRAM_CHANNELS=["@channel1","@channel2"] in .env')
        return 1

    try:
        from telethon import TelegramClient, events
    except ImportError:
        print("✗ telethon not installed. Run: pip install telethon")
        return 1

    print(f"→ Connecting to Telegram with api_id={TELEGRAM_API_ID}")
    print(f"→ Watching {len(TELEGRAM_CHANNELS)} channels: {TELEGRAM_CHANNELS}")
    print(f"→ Listening for {seconds} seconds...\n")

    client = TelegramClient("pump_bot_session", TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start(phone=TELEGRAM_PHONE)

    msg_count = 0
    mint_count = 0

    @client.on(events.NewMessage(chats=TELEGRAM_CHANNELS))
    async def handler(event):
        nonlocal msg_count, mint_count
        msg_count += 1
        text = event.raw_text or ""
        channel = (event.chat.username if event.chat else None) or "?"
        mints = extract_mints_from_text(text)
        hype = score_hype_text(text)
        if mints or hype >= 30:
            mint_count += len(mints)
            preview = text.replace("\n", " ")[:80]
            print(f"  [{channel}] hype={hype} mints={len(mints)} | {preview}")
            for m in mints:
                print(f"      → {m}")

    deadline = time.time() + seconds
    while time.time() < deadline:
        await asyncio.sleep(2)

    await client.disconnect()
    print(f"\n→ Done. Saw {msg_count} messages, extracted {mint_count} mints.")
    if msg_count == 0:
        print("  (No messages in the window — channels may be quiet, or your")
        print("   Telegram account might not have access. Verify by opening")
        print("   the channels in the Telegram app and confirming you can read them.)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--secs", type=int, default=60, help="Seconds to listen (default 60)")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.secs)))
