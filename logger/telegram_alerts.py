"""
logger/telegram_alerts.py

Lightweight alert dispatcher. Anywhere in the bot, call:

    from logger.telegram_alerts import send_alert
    send_alert("trade closed +0.012 SOL")

Fire-and-forget: schedules an aiohttp POST to Telegram's bot API and
returns immediately so the trading loop never blocks on a flaky network.

Reads from .env:
    TELEGRAM_BOT_TOKEN     — bot token from @BotFather
    TELEGRAM_OWNER_CHAT_ID — your private chat with the bot (set after
                             sending /start to the bot once)

Both must be set or send_alert is a no-op (logs a single warning at boot
and otherwise stays silent).
"""

from __future__ import annotations

import asyncio
import os

import aiohttp
from loguru import logger

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID   = os.getenv("TELEGRAM_OWNER_CHAT_ID", "")
_API_URL   = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"

_warned = False


def _ready() -> bool:
    """One-time warning if alerts are disabled, then silence."""
    global _warned
    if not _BOT_TOKEN or not _CHAT_ID:
        if not _warned:
            logger.info("[ALERTS] disabled (set TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_CHAT_ID in .env)")
            _warned = True
        return False
    return True


async def _post(text: str) -> None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                _API_URL,
                json={"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=aiohttp.ClientTimeout(total=4),
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.debug(f"[ALERTS] http {r.status}: {body[:200]}")
    except Exception as e:
        logger.debug(f"[ALERTS] send failed: {e}")


def send_alert(text: str) -> None:
    """Schedule the alert without blocking the caller.

    Safe to call from any async context. If no event loop is running
    (e.g. from a sync code path), runs the post synchronously instead.
    """
    if not _ready():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Sync caller — best-effort, fire it synchronously
        try:
            asyncio.run(_post(text))
        except Exception:
            pass
        return
    loop.create_task(_post(text))
