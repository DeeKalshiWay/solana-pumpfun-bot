"""
tools/control_bot.py

Telegram control + alert bot. Operates the live pump_bot from your phone.

Run alongside the trader:
    python -m tools.control_bot

Auth: only responds to TELEGRAM_OWNER_CHAT_ID. Anyone else messaging the
bot gets ignored (and their chat_id is logged so you can claim it once).

Commands:
    /status              wallet, positions, today's pnl, uptime
    /wallet              SOL liquid + on-chain held tokens
    /positions           list open positions with mark-to-market values
    /recent [N]          last N closed trades (default 10)
    /dump <mint>         force-sell a single mint via PumpPortal
    /dumpall             dump every open position
    /stop                stop the trading bot (graceful where possible)
    /start               relaunch the watchdog
    /threshold <N>       set MIN_BUY_SCORE in .env (restart needed)
    /sizing <pct>        set MAX_POSITION_PCT in .env (restart needed)
    /blacklist <addr>    add a creator to rugger_creators.json
    /preflight           run pre-live self-check
    /log [N]             tail last N lines of pump_bot.log (default 30)
    /help                this list

Setup:
    1. Telegram → @BotFather → /newbot, save token (you've done this)
    2. Set TELEGRAM_BOT_TOKEN in .env
    3. Send any message to your bot once. The bot will log
       "[AUTH] unauthorized chat_id=NNNNNNNNN" — copy that number into
       .env as TELEGRAM_OWNER_CHAT_ID, restart this script.
    4. Use /help to see commands.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from loguru import logger

load_dotenv(override=True)

BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OWNER_CHAT_ID = os.getenv("TELEGRAM_OWNER_CHAT_ID", "").strip()
BOT_DIR       = Path(__file__).resolve().parent.parent
ENV_FILE      = BOT_DIR / ".env"
LOG_FILE      = BOT_DIR / "logs" / "pump_bot.log"
TRADES_DB     = BOT_DIR / "logs" / "trades.db"
RUGGER_FILE   = BOT_DIR / "logs" / "rugger_creators.json"
RUN_FOREVER   = BOT_DIR / "run_forever.ps1"
WATCHLIST_FILE   = BOT_DIR / "logs" / "watchlist.json"
CANDIDATE_QUEUE  = BOT_DIR / "logs" / "candidate_queue.jsonl"
WATCH_POLL_SEC   = 60
WATCH_THRESHOLD  = 25.0   # default: alert on ±25% move from last alert price
PUMP_PROGRAM_ID  = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Lazy imports — only loaded when commands need them, keeps bot responsive
def _lazy_imports():
    from telethon import TelegramClient, events
    return TelegramClient, events


# ── Watchlist helpers ───────────────────────────────────────────────────────
def _load_watchlist() -> dict:
    if not WATCHLIST_FILE.exists():
        return {}
    try:
        return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_watchlist(d: dict) -> None:
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


async def _bonding_curve_price(session, mint: str) -> float:
    """SOL per raw token unit, or 0 if curve missing/migrated."""
    try:
        import base64
        import struct
        from solders.pubkey import Pubkey
        prog = Pubkey.from_string(PUMP_PROGRAM_ID)
        bc, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(mint))], prog
        )
        rpc = os.getenv("RPC_URL")
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [str(bc), {"encoding": "base64", "commitment": "confirmed"}],
        }
        async with session.post(rpc, json=payload, timeout=4) as r:
            d = await r.json()
        val = (d.get("result") or {}).get("value")
        if not val:
            return 0.0
        raw = base64.b64decode(val["data"][0])
        v_tok, v_sol = struct.unpack_from("<QQ", raw, 8)
        if v_tok == 0:
            return 0.0
        return (v_sol / 1e9) / v_tok
    except Exception:
        return 0.0


# ── Helpers ─────────────────────────────────────────────────────────────────
def _set_env_var(key: str, value: str) -> None:
    """Idempotent .env edit. Replaces existing line or appends."""
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    new_line = f"{key}={value}"
    if pattern.search(text):
        text = pattern.sub(new_line, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += new_line + "\n"
    ENV_FILE.write_text(text, encoding="utf-8")


def _process_running() -> bool:
    """Best-effort check for a live pump_bot main.py on this machine."""
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                 "Where-Object { $_.CommandLine -like '*main.py*' -and "
                 "$_.CommandLine -notmatch 'pappertrading' } | Measure-Object | "
                 "Select-Object -ExpandProperty Count"],
                capture_output=True, text=True, timeout=5,
            )
            return int((r.stdout or "0").strip()) > 0
        except Exception:
            return False
    return False


async def _wallet_state() -> dict:
    """Liquid SOL + held token positions on chain."""
    import aiohttp
    rpc = os.getenv("RPC_URL")
    out = {"sol": 0.0, "positions": []}
    async with aiohttp.ClientSession() as s:
        async with s.post(rpc, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance",
            "params": ["8C82xpKfvg8CU6FuASD2NdqTQCzZSM4EYHikTC1au9ni",
                       {"commitment": "finalized"}],
        }, timeout=aiohttp.ClientTimeout(total=8)) as r:
            d = await r.json()
        out["sol"] = d.get("result", {}).get("value", 0) / 1e9

        async with s.post(rpc, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": ["8C82xpKfvg8CU6FuASD2NdqTQCzZSM4EYHikTC1au9ni",
                       {"programId": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"},
                       {"encoding": "jsonParsed", "commitment": "finalized"}],
        }, timeout=aiohttp.ClientTimeout(total=10)) as r:
            d = await r.json()
        for a in d.get("result", {}).get("value", []):
            info = a["account"]["data"]["parsed"]["info"]
            mint = info["mint"]
            amt = info["tokenAmount"]["uiAmount"] or 0
            if amt > 0 and not mint.startswith("3DoCwnfJ"):
                out["positions"].append({"mint": mint, "amt": amt})
    return out


def _today_pnl() -> dict:
    """Aggregate today's closed trades from sqlite."""
    import datetime
    import sqlite3
    if not TRADES_DB.exists():
        return {"n": 0, "wins": 0, "pnl_sol": 0.0}
    threshold = int(time.mktime(
        datetime.datetime.combine(datetime.date.today(), datetime.time.min).timetuple()
    ))
    c = sqlite3.connect(str(TRADES_DB))
    n    = c.execute("SELECT COUNT(*) FROM closed_trades WHERE entry_time > ?", (threshold,)).fetchone()[0]
    wins = c.execute("SELECT COUNT(*) FROM closed_trades WHERE entry_time > ? AND sol_received > sol_invested", (threshold,)).fetchone()[0]
    pnl  = c.execute("SELECT COALESCE(SUM(pnl_sol),0) FROM closed_trades WHERE entry_time > ?", (threshold,)).fetchone()[0] or 0
    c.close()
    return {"n": n, "wins": wins, "pnl_sol": pnl}


# ── Command handlers ────────────────────────────────────────────────────────
async def cmd_status(_event, _args: list[str]) -> str:
    state = await _wallet_state()
    pnl = _today_pnl()
    running = _process_running()
    return (
        f"<b>{'🟢 ACTIVE' if running else '🔴 STOPPED'}</b>\n"
        f"Wallet: <code>{state['sol']:.4f}</code> SOL\n"
        f"Open positions: {len(state['positions'])}\n"
        f"Today: {pnl['n']} trades, {pnl['wins']} wins, "
        f"PnL <code>{pnl['pnl_sol']:+.4f}</code> SOL"
    )


async def cmd_wallet(_event, _args: list[str]) -> str:
    state = await _wallet_state()
    lines = [f"<b>Wallet:</b> <code>{state['sol']:.4f}</code> SOL liquid"]
    if state["positions"]:
        lines.append(f"\n<b>{len(state['positions'])} held tokens:</b>")
        for p in state["positions"][:20]:
            lines.append(f"  <code>{p['mint'][:20]}…</code> | {p['amt']:,.0f}")
        if len(state["positions"]) > 20:
            lines.append(f"  ...and {len(state['positions']) - 20} more")
    else:
        lines.append("No open positions.")
    return "\n".join(lines)


async def cmd_positions(_event, _args: list[str]) -> str:
    return await cmd_wallet(_event, _args)


async def cmd_recent(_event, args: list[str]) -> str:
    import sqlite3
    n = int(args[0]) if args and args[0].isdigit() else 10
    n = max(1, min(n, 30))
    if not TRADES_DB.exists():
        return "No trades.db yet."
    c = sqlite3.connect(str(TRADES_DB))
    rows = list(c.execute(
        "SELECT symbol, sol_invested, sol_received, pnl_sol, pnl_pct, hold_minutes, reason "
        "FROM closed_trades ORDER BY id DESC LIMIT ?", (n,)
    ))
    c.close()
    if not rows:
        return "No trades."
    lines = [f"<b>Last {len(rows)} trades:</b>"]
    for sym, inv, rec, pnl, pct, hold, reason in rows:
        sym = (sym or "?")[:14]
        sign = "✅" if pnl > 0 else "❌"
        lines.append(
            f"{sign} <b>{sym}</b> <code>{pnl:+.4f}</code> ({pct:+.0f}%) "
            f"{hold:.1f}m {reason}"
        )
    return "\n".join(lines)


async def cmd_dump(_event, args: list[str]) -> str:
    if not args:
        return "Usage: <code>/dump &lt;mint&gt;</code>"
    mint = args[0].strip()
    return await _do_dump([mint])


async def cmd_dumpall(_event, _args: list[str]) -> str:
    state = await _wallet_state()
    if not state["positions"]:
        return "Nothing to dump."
    return await _do_dump([p["mint"] for p in state["positions"]])


async def _do_dump(mints: list[str]) -> str:
    from trader.executor import TradeExecutor
    from trader.wallet import SolanaWallet
    wallet = SolanaWallet()
    await wallet.start()
    executor = TradeExecutor(wallet.keypair)
    await executor.start()
    sol_before = await wallet.get_sol_balance()
    results = []
    for m in mints:
        try:
            r = await executor.sell(m, "100%", reason="control_bot_dump")
            results.append((m, r.get("success", False), r.get("error", "-")))
        except Exception as e:
            results.append((m, False, str(e)[:40]))
        await asyncio.sleep(2)
    await asyncio.sleep(4)
    sol_after = await wallet.get_sol_balance()
    await wallet.stop()
    succ = sum(1 for _, ok, _ in results if ok)
    lines = [
        f"<b>Dumped {succ}/{len(mints)}</b>",
        f"SOL Δ: <code>{sol_after - sol_before:+.4f}</code>",
    ]
    for m, ok, err in results[:8]:
        mark = "✅" if ok else "❌"
        lines.append(f"  {mark} <code>{m[:16]}…</code> {'' if ok else f'({err})'}")
    return "\n".join(lines)


async def cmd_stop(_event, _args: list[str]) -> str:
    if os.name != "nt":
        return "stop only implemented for Windows"
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*main.py*' -and "
        "$_.CommandLine -notmatch 'pappertrading' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
        "Where-Object { $_.CommandLine -like '*run_forever*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                   capture_output=True, timeout=10)
    await asyncio.sleep(1)
    running = _process_running()
    return "🔴 Stopped." if not running else "⚠ Still running — try again or check manually."


async def cmd_start(_event, _args: list[str]) -> str:
    if os.name != "nt":
        return "start only implemented for Windows"
    if _process_running():
        return "Already running."
    cmd = (
        f'Start-Process powershell -ArgumentList '
        f'"-WindowStyle Hidden -ExecutionPolicy Bypass -File `"{RUN_FOREVER}`"" '
        f'-WindowStyle Hidden'
    )
    subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd])
    await asyncio.sleep(8)
    return "🟢 Started." if _process_running() else "⚠ Watchdog launched but bot not visible yet — check /status in a sec."


async def cmd_threshold(_event, args: list[str]) -> str:
    if not args or not args[0].lstrip("-").isdigit():
        return "Usage: <code>/threshold 35</code>"
    n = int(args[0])
    if not 0 < n < 100:
        return "Threshold must be 1-99."
    _set_env_var("MIN_BUY_SCORE", str(n))
    return f"✅ Set <code>MIN_BUY_SCORE={n}</code> in .env\n<i>Restart for it to take effect (use /stop then /start).</i>"


async def cmd_sizing(_event, args: list[str]) -> str:
    if not args:
        return "Usage: <code>/sizing 0.05</code> (5% of wallet)"
    try:
        v = float(args[0])
    except ValueError:
        return "Sizing must be a decimal like 0.05"
    if not 0.001 <= v <= 1.0:
        return "Sizing must be between 0.001 (0.1%) and 1.0 (100%)."
    _set_env_var("MAX_POSITION_PCT", str(v))
    return f"✅ Set <code>MAX_POSITION_PCT={v}</code> in .env\n<i>Restart to apply.</i>"


async def cmd_blacklist(_event, args: list[str]) -> str:
    if not args:
        return "Usage: <code>/blacklist &lt;creator_address&gt;</code>"
    addr = args[0].strip()
    if len(addr) < 32 or len(addr) > 44:
        return "That doesn't look like a Solana address (32-44 chars)."
    data = {"generated_at": int(time.time()), "rug_threshold_pct": -50.0,
            "count": 0, "sources": {}, "creators": []}
    if RUGGER_FILE.exists():
        try:
            data = json.loads(RUGGER_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    creators = set(data.get("creators", []))
    if addr in creators:
        return "Already blacklisted."
    creators.add(addr)
    data["creators"] = sorted(creators)
    data["count"] = len(creators)
    RUGGER_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return f"✅ Blacklisted <code>{addr[:8]}…</code>\n<i>Restart to apply.</i> ({len(creators)} total)"


async def cmd_preflight(_event, _args: list[str]) -> str:
    r = subprocess.run(
        [sys.executable, "-m", "tools.preflight"],
        capture_output=True, text=True, timeout=30, cwd=BOT_DIR,
    )
    output = (r.stdout or "") + (r.stderr or "")
    # Strip ANSI color codes for telegram readability
    output = re.sub(r"\x1b\[[0-9;]*m", "", output)
    return f"<pre>{output[-3500:]}</pre>"


async def cmd_log(_event, args: list[str]) -> str:
    n = int(args[0]) if args and args[0].isdigit() else 30
    n = max(5, min(n, 100))
    if not LOG_FILE.exists():
        return "No log file yet."
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = "\n".join(lines[-n:])[-3500:]  # telegram message cap
    # Light cleanup of ANSI escape codes
    tail = re.sub(r"\x1b\[[0-9;]*m", "", tail)
    return f"<pre>{tail}</pre>"


async def cmd_watch(_event, args: list[str]) -> str:
    if not args:
        return "Usage: <code>/watch &lt;mint&gt; [threshold_pct]</code>"
    mint = args[0].strip()
    threshold = WATCH_THRESHOLD
    if len(args) >= 2:
        try:
            threshold = float(args[1])
        except ValueError:
            return "Threshold must be a number"
    import aiohttp
    async with aiohttp.ClientSession() as s:
        price = await _bonding_curve_price(s, mint)
    if price <= 0:
        return f"❌ Could not read bonding curve for <code>{mint[:12]}…</code>\n(might be migrated to Raydium or invalid)"
    wl = _load_watchlist()
    wl[mint] = {
        "added_at":         int(time.time()),
        "entry_price":      price,
        "last_alert_price": price,
        "threshold_pct":    threshold,
    }
    _save_watchlist(wl)
    return (
        f"👀 Watching <code>{mint[:12]}…</code>\n"
        f"entry: {price:.10f} SOL/raw\n"
        f"threshold: ±{threshold:.0f}% from last alert"
    )


async def cmd_unwatch(_event, args: list[str]) -> str:
    if not args:
        return "Usage: <code>/unwatch &lt;mint_prefix&gt;</code>"
    prefix = args[0].strip()
    wl = _load_watchlist()
    matches = [m for m in wl if m == prefix or m.startswith(prefix)]
    if not matches:
        return "Not found in watchlist."
    if len(matches) > 1:
        return f"Ambiguous — {len(matches)} matches. Use a longer prefix."
    del wl[matches[0]]
    _save_watchlist(wl)
    return f"✅ Removed <code>{matches[0][:12]}…</code>"


async def cmd_watchlist(_event, _args: list[str]) -> str:
    wl = _load_watchlist()
    if not wl:
        return "Watchlist empty. <code>/watch &lt;mint&gt;</code> to add."
    import aiohttp
    lines = [f"<b>Watchlist ({len(wl)}):</b>"]
    async with aiohttp.ClientSession() as s:
        for mint, info in list(wl.items())[:30]:
            price = await _bonding_curve_price(s, mint)
            entry = info.get("entry_price", 0) or 0
            pct_from_entry = ((price - entry) / entry * 100) if entry > 0 and price > 0 else 0
            sign = "+" if pct_from_entry >= 0 else ""
            status = "💀" if price <= 0 else ("🚀" if pct_from_entry >= 50 else "📈" if pct_from_entry > 0 else "📉")
            lines.append(
                f"{status} <code>{mint[:12]}…</code> "
                f"{sign}{pct_from_entry:.1f}% "
                f"(±{info.get('threshold_pct', WATCH_THRESHOLD):.0f}%)"
            )
    return "\n".join(lines)


async def cmd_buy(_event, args: list[str]) -> str:
    if not args:
        return "Usage: <code>/buy &lt;mint&gt; [sol_amount]</code> (default 0.01)"
    mint = args[0].strip()
    sol_amount = 0.01
    if len(args) >= 2:
        try:
            sol_amount = float(args[1])
        except ValueError:
            return "Amount must be a decimal like 0.02"
    if sol_amount < 0.001 or sol_amount > 1.0:
        return "Amount must be 0.001-1.0 SOL"
    from trader.executor import TradeExecutor
    from trader.wallet import SolanaWallet
    wallet = SolanaWallet()
    await wallet.start()
    executor = TradeExecutor(wallet.keypair)
    await executor.start()
    sol_before = await wallet.get_sol_balance()
    try:
        result = await executor.buy(mint, sol_amount)
    except Exception as e:
        await wallet.stop()
        return f"❌ <code>{type(e).__name__}: {str(e)[:200]}</code>"
    await asyncio.sleep(4)
    sol_after = await wallet.get_sol_balance()
    await wallet.stop()
    if not result.get("success"):
        return f"❌ Buy failed: <code>{result.get('error', 'unknown')}</code>"
    spent = sol_before - sol_after
    return (
        f"✅ Bought <code>{mint[:12]}…</code>\n"
        f"spent: {spent:.4f} SOL\n"
        f"sig: <code>{(result.get('signature') or '')[:24]}</code>\n"
        f"<i>Manual buy — bot won't auto-manage exits. Use /dump to sell, or /watch to track.</i>"
    )


async def cmd_help(_event, _args: list[str]) -> str:
    return (
        "<b>Pump bot control</b>\n"
        "<b>Health:</b>\n"
        "  /status, /wallet, /positions\n"
        "  /recent [N], /preflight, /log [N]\n"
        "<b>Trading:</b>\n"
        "  /buy &lt;mint&gt; [sol] — manual entry\n"
        "  /dump &lt;mint&gt;, /dumpall — manual sell\n"
        "  /stop, /start — bot lifecycle\n"
        "<b>Tuning (restart needed):</b>\n"
        "  /threshold N, /sizing X, /blacklist &lt;addr&gt;\n"
        "<b>Watchlist:</b>\n"
        "  /watch &lt;mint&gt; [pct] — track a token\n"
        "  /unwatch &lt;mint&gt;, /watchlist\n"
        "<b>Aggregator:</b>\n"
        "  Forward any message → I'll extract mints/tickers and score\n"
    )


COMMANDS = {
    "status":    cmd_status,
    "wallet":    cmd_wallet,
    "positions": cmd_positions,
    "recent":    cmd_recent,
    "buy":       cmd_buy,
    "dump":      cmd_dump,
    "dumpall":   cmd_dumpall,
    "stop":      cmd_stop,
    "start":     cmd_start,
    "threshold": cmd_threshold,
    "sizing":    cmd_sizing,
    "blacklist": cmd_blacklist,
    "preflight": cmd_preflight,
    "log":       cmd_log,
    "watch":     cmd_watch,
    "unwatch":   cmd_unwatch,
    "watchlist": cmd_watchlist,
    "help":      cmd_help,
}


# ── Personal Alpha Aggregator ───────────────────────────────────────────────
async def aggregator_reply(event, text: str) -> str | None:
    """Extract mints + tickers from any non-command message.

    Returns a Telegram-formatted summary if anything was found, or None
    so the caller can stay quiet on uninteresting messages.
    """
    try:
        from detector.social_monitor import (
            extract_mints_from_text, record_mention, score_hype_text,
        )
    except Exception as e:
        return f"<i>aggregator unavailable: {e}</i>"

    mints   = extract_mints_from_text(text)
    tickers = list(set(re.findall(r"\$([A-Za-z][A-Za-z0-9]{1,11})", text)))
    hype    = score_hype_text(text)

    if not mints and not tickers and hype < 30:
        return None  # nothing interesting

    # Feed mentions into the in-process social store. Only useful if the
    # trader is running in the same Python process — for our two-process
    # setup it mostly serves as a record. A future integration could
    # write to a shared queue file the trader reads.
    for m in mints:
        try:
            record_mention(m, "tg:aggregator", hype)
        except Exception:
            pass

    lines = ["📡 <b>Aggregator</b>"]
    if mints:
        lines.append("Mints found:")
        for m in mints[:5]:
            lines.append(f"  <code>{m}</code>")
            lines.append(f"  → <code>/buy {m} 0.02</code>  /  <code>/watch {m}</code>")
    if tickers:
        shown = ", ".join(f"${t}" for t in tickers[:8])
        lines.append(f"Tickers: {shown}")
    lines.append(f"Hype keywords: {hype}")
    return "\n".join(lines)


# ── Background tasks ────────────────────────────────────────────────────────
async def watchlist_poller(client) -> None:
    """Poll bonding-curve prices for watched tokens, alert on big moves."""
    import aiohttp
    while True:
        await asyncio.sleep(WATCH_POLL_SEC)
        try:
            wl = _load_watchlist()
            if not wl or not OWNER_CHAT_ID:
                continue
            async with aiohttp.ClientSession() as s:
                changed = False
                for mint, info in list(wl.items()):
                    price = await _bonding_curve_price(s, mint)
                    if price <= 0:
                        continue
                    last = info.get("last_alert_price", info.get("entry_price", 0)) or 0
                    if last <= 0:
                        info["last_alert_price"] = price
                        wl[mint] = info
                        changed = True
                        continue
                    pct = (price - last) / last * 100
                    threshold = info.get("threshold_pct", WATCH_THRESHOLD)
                    if abs(pct) >= threshold:
                        info["last_alert_price"] = price
                        wl[mint] = info
                        changed = True
                        sign = "🚀" if pct > 0 else "📉"
                        msg = (
                            f"{sign} <b>watch</b> <code>{mint[:12]}…</code> "
                            f"{'+' if pct > 0 else ''}{pct:.1f}% "
                            f"since last alert"
                        )
                        try:
                            await client.send_message(int(OWNER_CHAT_ID), msg, parse_mode="HTML")
                        except Exception as e:
                            logger.debug(f"[WATCHLIST] send fail: {e}")
                if changed:
                    _save_watchlist(wl)
        except Exception as e:
            logger.debug(f"[WATCHLIST POLLER] {e}")


async def candidate_tailer(client) -> None:
    """Tail logs/candidate_queue.jsonl and post fresh borderline candidates."""
    seen: set[str] = set()
    while True:
        await asyncio.sleep(15)
        try:
            if not CANDIDATE_QUEUE.exists() or not OWNER_CHAT_ID:
                continue
            now = time.time()
            for line in CANDIDATE_QUEUE.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    cand = json.loads(line)
                except Exception:
                    continue
                mint = cand.get("mint", "")
                if not mint or mint in seen:
                    continue
                seen.add(mint)
                # Skip stale (older than 5 min) — bot was down or post lag
                if now - cand.get("ts", 0) > 300:
                    continue
                sym   = (cand.get("symbol") or "?")[:18]
                score = cand.get("score", "?")
                init  = cand.get("init_buy", 0)
                curve = cand.get("curve_pct", 0)
                msg = (
                    f"🤔 <b>Candidate</b>: {sym}\n"
                    f"score {score} | init_buy {init:.2f} SOL | curve {curve:.1f}%\n"
                    f"<code>{mint}</code>\n"
                    f"<code>/buy {mint} 0.02</code>  /  <code>/watch {mint}</code>"
                )
                try:
                    await client.send_message(int(OWNER_CHAT_ID), msg, parse_mode="HTML")
                except Exception as e:
                    logger.debug(f"[CANDIDATE] send fail: {e}")
        except Exception as e:
            logger.debug(f"[CANDIDATE TAILER] {e}")


# ── Main ────────────────────────────────────────────────────────────────────
async def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    if not BOT_TOKEN:
        logger.error("Missing TELEGRAM_BOT_TOKEN in .env")
        return 1
    if not OWNER_CHAT_ID:
        logger.warning("Missing TELEGRAM_OWNER_CHAT_ID — first message will print yours")

    TelegramClient, events = _lazy_imports()
    # Bot tokens use `bot` connect mode; api_id and api_hash from .env still
    # required by Telethon's protocol but any valid pair works.
    api_id   = int(os.getenv("TELEGRAM_API_ID") or 1)
    api_hash = os.getenv("TELEGRAM_API_HASH") or "0" * 32
    client = TelegramClient("control_bot_session", api_id, api_hash)
    await client.start(bot_token=BOT_TOKEN)
    logger.success("[CONTROL] connected as bot")

    @client.on(events.NewMessage())
    async def handler(event):
        try:
            chat_id = str(event.chat_id)
            text = (event.raw_text or "").strip()

            # Auth gate
            if OWNER_CHAT_ID and chat_id != OWNER_CHAT_ID:
                logger.warning(f"[AUTH] unauthorized chat_id={chat_id} text={text[:60]!r}")
                return
            if not OWNER_CHAT_ID:
                logger.success(
                    f"[AUTH] first contact from chat_id={chat_id} — "
                    f"add this to .env as TELEGRAM_OWNER_CHAT_ID and restart"
                )
                await event.respond(
                    f"Your chat_id is <code>{chat_id}</code>. "
                    f"Set TELEGRAM_OWNER_CHAT_ID to that value in .env and restart this bot.",
                    parse_mode="HTML",
                )
                return

            if not text.startswith("/"):
                # Personal Alpha Aggregator — extract mints/tickers from any
                # forwarded or pasted message. Returns None if uninteresting.
                summary = await aggregator_reply(event, text)
                if summary:
                    await event.respond(summary, parse_mode="HTML")
                return

            parts = text[1:].split()
            if not parts:
                return
            cmd, args = parts[0].lower(), parts[1:]
            # Strip @botname suffix on commands sent in groups
            if "@" in cmd:
                cmd = cmd.split("@", 1)[0]

            handler_fn = COMMANDS.get(cmd)
            if not handler_fn:
                await event.respond("Unknown command. /help for list.", parse_mode="HTML")
                return

            try:
                reply = await handler_fn(event, args)
            except Exception as e:
                logger.exception(f"[CMD {cmd}] failed: {e}")
                reply = f"❌ <code>{type(e).__name__}: {str(e)[:200]}</code>"

            if reply:
                await event.respond(reply, parse_mode="HTML")

        except Exception as e:
            logger.exception(f"[HANDLER] {e}")

    # Background loops: watchlist polling + candidate queue tailer
    asyncio.create_task(watchlist_poller(client), name="watchlist_poller")
    asyncio.create_task(candidate_tailer(client),  name="candidate_tailer")
    logger.info("[CONTROL] watchlist poller + candidate tailer started")

    logger.info("[CONTROL] listening for commands. /help for list.")
    await client.run_until_disconnected()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
