"""
main.py
Pump Bot orchestrator. Set PAPER_TRADING=True in config.py to simulate without
spending real SOL. Set False to go live.
"""

import asyncio
import signal
import sys

import aiohttp
from loguru import logger

from analyzer.counterfactual import counterfactual
from analyzer.signal_scorer import SignalScorer
from config import (
    MAX_OPEN_POSITIONS,
    MAX_SOL_PER_TRADE,
    MIN_BUY_SCORE,
    PAPER_STARTING_SOL,
    PAPER_TRADING,
    PRIVATE_KEY,
)
from detector.dex_monitor import TrendingScanner
from detector.influencer_monitor import influencer_monitor
from detector.pumpfun_monitor import PumpFunMonitor
from detector.pumpfun_tracker import PumpFunTracker
from detector.social_monitor import SocialMonitor
from detector.wallet_intel import wallet_intel
from logger.dashboard import Dashboard, setup_logging
from logger.report import ReportLogger
from logger.web_server import WebDashboard
from risk.manager import RiskManager


async def _resolve_tokens_received(result: dict, mint: str, wallet, retries: int = 4, delay: float = 2.0):
    """Query wallet balance after a PumpPortal buy to get actual tokens received."""
    for attempt in range(retries):
        await asyncio.sleep(delay)
        try:
            balance = await wallet.get_token_balance(mint)
            if balance > 0:
                # pump.fun tokens use 6 decimals
                result["tokens_expected"] = int(balance * 1_000_000)
                logger.info(f"[TOKEN RESOLVE] {mint[:8]} — {balance:,.0f} tokens confirmed on attempt {attempt+1}")
                return
        except Exception as e:
            logger.debug(f"[TOKEN RESOLVE] attempt {attempt+1} failed: {e}")
    logger.warning(f"[TOKEN RESOLVE] {mint[:8]} — could not confirm token balance after {retries} attempts")


async def trade_loop(trade_queue, executor, risk_manager, dashboard, wallet):
    while True:
        try:
            token = await asyncio.wait_for(trade_queue.get(), timeout=1.0)
        except TimeoutError:
            continue

        if risk_manager.emergency_stop_active:
            continue

        mint   = token["mint"]
        symbol = token.get("symbol", "???")
        score  = token.get("score", 0)

        sol_amount = await risk_manager.calculate_position_size(score)
        if sol_amount <= 0:
            token["reject_reason"] = "no_capacity"
            dashboard.record_signal(token)
            continue

        logger.info(f"[TRADE LOOP] Buy: {symbol} | {sol_amount} SOL | score={score}")
        result = await executor.buy(mint, sol_amount, token=token)

        if result["success"]:
            # PumpPortal doesn't return token amount — query wallet after settlement
            if not PAPER_TRADING and result.get("venue") == "pumpportal" and result.get("tokens_expected", 0) == 0:
                await _resolve_tokens_received(result, mint, wallet)

            if result.get("tokens_expected", 0) > 0:
                risk_manager.open_position(token, result)
                token["queued_for_buy"] = True
            else:
                logger.warning(f"[TRADE LOOP] {symbol} buy confirmed but tokens unresolved — position skipped")
                token["reject_reason"] = "tokens_unresolved"
        else:
            token["reject_reason"] = result.get("error", "buy_failed")
            logger.error(f"[TRADE LOOP] Buy failed {symbol}: {token['reject_reason']}")

        dashboard.record_signal(token)


async def price_monitor_loop(risk_manager, executor):
    import aiohttp

    from config import JUPITER_API_URL, SOL_MINT

    async with aiohttp.ClientSession() as session:
        while True:
            for mint, pos in list(risk_manager.positions.items()):
                price = await _get_token_price(session, mint, JUPITER_API_URL, SOL_MINT)
                if price > 0:
                    risk_manager.update_price(mint, price)
                    if hasattr(executor, "update_price"):
                        executor.update_price(mint, price)
            await asyncio.sleep(5)


async def _get_token_price(session, mint: str, jupiter_url: str, sol_mint: str) -> float:
    """Try Jupiter first, fall back to DexScreener for pump.fun bonding-curve tokens."""
    # Jupiter
    try:
        params = {
            "inputMint":   mint,
            "outputMint":  sol_mint,
            "amount":      "1000000",
            "slippageBps": "100",
        }
        async with session.get(
            f"{jupiter_url}/quote", params=params,
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                out = int(data.get("outAmount", 0))
                price = out / 1e9 / 1_000_000  # SOL per raw token unit
                if price > 0:
                    return price
    except Exception:
        pass

    # DexScreener fallback
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs") or []
                if pairs:
                    price_native = float(pairs[0].get("priceNative") or 0)
                    if price_native > 0:
                        return price_native / 1_000_000  # SOL per raw token unit
    except Exception:
        pass

    return 0.0


PID_LOCK_FILE = "logs/bot.pid"


def _acquire_pid_lock():
    """
    Single-instance guard. If another bot is already running, refuse to start.
    Prevents the multi-bot file-race crashes we saw overnight.

    Logic:
      - If logs/bot.pid exists AND the PID inside is still a live python
        process, we exit with code 2 ("already running"). Watchdog will retry.
      - Otherwise we write our own PID and proceed.
      - On clean shutdown we remove the file (best-effort).
    """
    import os
    os.makedirs("logs", exist_ok=True)

    if os.path.exists(PID_LOCK_FILE):
        try:
            with open(PID_LOCK_FILE) as f:
                old_pid = int(f.read().strip())
        except Exception:
            old_pid = None

        if old_pid and old_pid != os.getpid():
            # Check if the old PID is still alive
            alive = False
            try:
                if os.name == "nt":
                    # Windows: open handle to the process
                    import ctypes
                    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                    h = ctypes.windll.kernel32.OpenProcess(
                        PROCESS_QUERY_LIMITED_INFORMATION, False, old_pid
                    )
                    if h:
                        # Check exit code: STILL_ACTIVE = 259
                        exit_code = ctypes.c_ulong(0)
                        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
                        ctypes.windll.kernel32.CloseHandle(h)
                        alive = (exit_code.value == 259)
                else:
                    os.kill(old_pid, 0)
                    alive = True
            except Exception:
                alive = False

            if alive:
                logger.critical(
                    f"Another bot is already running (PID {old_pid}). "
                    f"Refusing to start a duplicate. Exit 2."
                )
                sys.exit(2)
            else:
                logger.warning(f"Stale PID lock from dead PID {old_pid}, taking over")

    # Write our PID
    with open(PID_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def _release_pid_lock():
    import os
    try:
        if os.path.exists(PID_LOCK_FILE):
            with open(PID_LOCK_FILE) as f:
                pid = int(f.read().strip())
            if pid == os.getpid():
                os.remove(PID_LOCK_FILE)
    except Exception:
        pass


async def main():
    setup_logging()
    _acquire_pid_lock()

    logger.info("=" * 60)
    logger.info("  PUMP BOT STARTING")
    if PAPER_TRADING:
        logger.warning("  *** PAPER TRADING MODE — no real money ***")
        logger.warning(f"  Virtual balance: {PAPER_STARTING_SOL} SOL")
    else:
        logger.info("  *** LIVE TRADING — real money at risk ***")
        if PRIVATE_KEY == "YOUR_PRIVATE_KEY_HERE":
            logger.critical("Set SOLANA_PRIVATE_KEY before running!")
            sys.exit(1)
    logger.info(f"  Max per trade:   {MAX_SOL_PER_TRADE} SOL")
    logger.info(f"  Min buy score:   {MIN_BUY_SCORE}/100")
    logger.info(f"  Max positions:   {MAX_OPEN_POSITIONS}")
    logger.info("=" * 60)

    raw_queue   = asyncio.Queue(maxsize=500)
    trade_queue = asyncio.Queue(maxsize=50)

    if PAPER_TRADING:
        from trader.paper_executor import PaperExecutor
        from trader.paper_wallet import PaperWallet
        wallet   = PaperWallet(PAPER_STARTING_SOL)
        executor = PaperExecutor(wallet)
    else:
        from trader.executor import TradeExecutor
        from trader.wallet import SolanaWallet
        wallet = SolanaWallet()
        await wallet.start()
        executor = TradeExecutor(wallet.keypair)
        await executor.start()

    risk_mgr  = RiskManager(wallet, executor)
    await risk_mgr.initialize()

    scorer     = SignalScorer(raw_queue, trade_queue, executor=executor)
    dashboard  = Dashboard(risk_mgr, scorer)
    reporter   = ReportLogger(risk_mgr, scorer)
    web_dash   = WebDashboard(risk_mgr, scorer, dashboard, PAPER_TRADING, report_logger=reporter)

    pumpfun_monitor  = PumpFunMonitor(raw_queue)
    # Tracker piggybacks on the monitor's WS via callback (no own connection).
    pumpfun_tracker  = PumpFunTracker(monitor=pumpfun_monitor)
    # Wallet intel attaches before monitor.run() so it sees every event.
    wallet_intel.attach(pumpfun_monitor)
    trending_scanner = TrendingScanner(raw_queue)
    social_monitor   = SocialMonitor()

    shutdown_event = asyncio.Event()

    def _signal_handler():
        logger.warning("Shutdown requested...")
        shutdown_event.set()

    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)
    except (NotImplementedError, AttributeError):
        pass  # Windows doesn't support signal handlers

    tasks = [
        asyncio.create_task(pumpfun_monitor.run(),                                          name="pumpfun"),
        asyncio.create_task(pumpfun_tracker.run(),                                          name="pumpfun_tracker"),
        asyncio.create_task(trending_scanner.run(),                                         name="dex_scanner"),
        asyncio.create_task(social_monitor.run(),                                           name="social"),
        asyncio.create_task(wallet_intel.run(),                                             name="wallet_intel"),
        asyncio.create_task(influencer_monitor.run(),                                       name="influencer"),
        asyncio.create_task(scorer.start(),                                                 name="scorer"),
        asyncio.create_task(trade_loop(trade_queue, executor, risk_mgr, dashboard, wallet), name="trade"),
        asyncio.create_task(risk_mgr.run_monitor_loop(),                                    name="risk"),
        asyncio.create_task(price_monitor_loop(risk_mgr, executor),                         name="prices"),
        asyncio.create_task(dashboard.run(),                                                name="dashboard"),
        asyncio.create_task(web_dash.run(),                                                 name="web"),
        asyncio.create_task(reporter.run(),                                                 name="report"),
        asyncio.create_task(counterfactual.run(),                                           name="counterfactual"),
    ]

    logger.success("All systems GO — bot is live")

    try:
        await shutdown_event.wait()
    except KeyboardInterrupt:
        shutdown_event.set()

    logger.warning("Shutting down...")
    pumpfun_monitor.stop()
    pumpfun_tracker.stop()
    trending_scanner.stop()
    wallet_intel.stop()
    influencer_monitor.stop()
    counterfactual.stop()
    risk_mgr.stop()
    await social_monitor.stop()

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if not PAPER_TRADING:
        await executor.stop()
        await wallet.stop()

    stats = risk_mgr.get_stats()
    logger.info(f"Final stats: {stats}")
    logger.info("Bot stopped cleanly.")
    _release_pid_lock()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception as e:
        logger.exception(f"Top-level crash: {e}")
        _release_pid_lock()
        sys.exit(1)
    finally:
        _release_pid_lock()
