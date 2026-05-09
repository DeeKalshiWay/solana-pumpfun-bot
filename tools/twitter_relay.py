"""
tools/twitter_relay.py

Tiny HTTP relay: IFTTT (Twitter → Webhooks) → this server → Telegram channel.

Why this exists:
  IFTTT free tier limits you to 2 active applets if you use their built-in
  Telegram service. The Webhooks action has no such cap — you can have as
  many "watch this Twitter user" applets as you want, all hitting one URL
  on this relay. The relay then posts each tweet into your private Telegram
  channel, where pump_bot's TelegramMonitor (Telethon) reads it and feeds
  the extracted mints/tickers into the scorer.

  Net effect: unlimited curated Twitter feed → bot scoring boost. Free.

Setup (one-time):

  1. Create a Telegram bot via @BotFather, save the token.
  2. Create a private Telegram channel. Add the bot as admin. Add yourself
     as a member so Telethon can read it.
  3. Forward any message from the channel to @JsonDumpBot to get the
     numeric chat ID (looks like -1001234567890).
  4. Add to .env:
        TELEGRAM_BOT_TOKEN=123456789:ABC...
        TELEGRAM_RELAY_CHAT_ID=-1001234567890
        RELAY_SECRET=somelongrandomstring
        RELAY_PORT=8090
  5. Add the same chat ID to TELEGRAM_CHANNELS so the bot listens to it:
        TELEGRAM_CHANNELS=["-1001234567890"]
  6. Start the relay:
        python -m tools.twitter_relay
  7. In another terminal, expose it publicly:
        tools\ngrok.exe http 8090
     ngrok prints a URL like https://abc123.ngrok-free.app — note it.
  8. In IFTTT, create an applet:
        - If This: X (Twitter) → "New tweet by a specific user"
        - Then That: Webhooks → "Make a web request"
            URL:          https://abc123.ngrok-free.app/relay/<RELAY_SECRET>
            Method:       POST
            Content type: application/json
            Body:
                {"text":"{{Text}}","link":"{{LinkToTweet}}","user":"{{UserName}}"}
  9. Repeat step 8 for each Twitter account. No 2-applet cap on Webhooks.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from loguru import logger

load_dotenv(override=True)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_RELAY_CHAT_ID", "")
SECRET    = os.getenv("RELAY_SECRET", "changeme")
PORT      = int(os.getenv("RELAY_PORT", "8090"))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


async def _post_to_telegram(text: str) -> tuple[bool, dict]:
    payload = {
        "chat_id": CHAT_ID,
        "text":    text,
        "disable_web_page_preview": False,
        "parse_mode": "HTML",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(
            TELEGRAM_API, json=payload,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            data = await r.json()
    return data.get("ok", False), data


async def health(_request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "service": "twitter_relay",
        "bot_configured": bool(BOT_TOKEN),
        "chat_configured": bool(CHAT_ID),
    })


async def relay(request: web.Request) -> web.Response:
    secret = request.match_info.get("secret", "")
    if secret != SECRET:
        logger.warning(f"403 from {request.remote} (bad secret)")
        return web.Response(status=403, text="forbidden")

    try:
        body = await request.json()
    except Exception:
        # IFTTT sometimes sends form-encoded — fall back to raw text
        body = {"text": (await request.text()).strip()}

    text = (body.get("text") or "").strip()
    link = (body.get("link") or "").strip()
    user = (body.get("user") or "").strip().lstrip("@")
    if not text and not link:
        return web.json_response({"status": "empty"}, status=400)

    # Compose the channel message — kept terse so the existing
    # extract_mints_from_text / score_hype_text logic can do its thing
    parts = []
    if user:
        parts.append(f"<b>@{user}</b>")
    if text:
        parts.append(text)
    if link:
        parts.append(link)
    msg = "\n".join(parts)

    ok, resp = await _post_to_telegram(msg)
    if not ok:
        logger.warning(f"telegram failed: {resp}")
        return web.json_response({"status": "telegram_error", "details": resp}, status=502)

    logger.info(f"relayed @{user or '?'}: {(text or link)[:80]}")
    return web.json_response({"status": "ok"})


async def smoke(_request: web.Request) -> web.Response:
    """GET /smoke?msg=hello — sends a test message to verify the chain.

    Hit this once after setup to confirm the bot can post to the channel
    before wiring up IFTTT.
    """
    qs = _request.rel_url.query
    msg = qs.get("msg", "twitter_relay smoke test 🛰")
    ok, resp = await _post_to_telegram(msg)
    return web.json_response({"ok": ok, "telegram": resp})


def main() -> int:
    if not BOT_TOKEN:
        logger.error("Missing TELEGRAM_BOT_TOKEN in .env — get one from @BotFather")
        return 1
    if not CHAT_ID:
        logger.error("Missing TELEGRAM_RELAY_CHAT_ID in .env — forward a channel msg to @JsonDumpBot to get the id")
        return 1
    if SECRET == "changeme":
        logger.warning("RELAY_SECRET is the default — set something random in .env")

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/smoke", smoke)
    app.router.add_post("/relay/{secret}", relay)

    logger.info(f"twitter_relay listening on :{PORT}")
    logger.info(f"  health:  http://localhost:{PORT}/health")
    logger.info(f"  smoke:   http://localhost:{PORT}/smoke?msg=hello (sends test to channel)")
    logger.info(f"  ingress: POST http://localhost:{PORT}/relay/{SECRET[:4]}***")
    logger.info(f"Expose publicly with: tools\\ngrok.exe http {PORT}")

    web.run_app(app, port=PORT, print=lambda *_: None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
