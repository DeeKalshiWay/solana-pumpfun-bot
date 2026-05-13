"""
main.py
Pump Bot orchestrator. Set PAPER_TRADING=True in config.py to simulate without
spending real SOL. Set False to go live.
"""

import asyncio
import faulthandler
import os
import signal
import sys
import threading
import time

import aiohttp
from loguru import logger

from analyzer.auto_tuner import auto_tuner
from analyzer.counterfactual import counterfactual
from analyzer.regime_filter import regime_filter
from analyzer.signal_scorer import SignalScorer
from config import (
    CREATOR_BLACKLIST,
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
from detector.whale_tracker import whale_tracker
from logger.daily_report import DailyReporter
from logger.dashboard import Dashboard, setup_logging
from logger.report import ReportLogger
from logger.web_server import WebDashboard
from risk.manager import RiskManager


async def _resolve_tokens_received(result: dict, mint: str, wallet, retries: int = 8, delay: float = 1.5):
    """Resolve tokens received from a PumpPortal buy.

    Primary path: parse postTokenBalances from the buy tx receipt — this is
    the on-chain source of truth and works for both legacy SPL and Token-2022
    mints (pump.fun uses Token-2022). Polling the wallet via
    getTokenAccountsByOwner can race with indexer lag for freshly-created ATAs.

    Fallback: poll wallet.get_token_balance.
    """
    sig = result.get("signature")
    owner_str = str(wallet.pubkey)

    if sig:
        for attempt in range(retries):
            await asyncio.sleep(delay)
            try:
                rpc_resp = await wallet._rpc("getTransaction", [
                    sig,
                    {"encoding": "jsonParsed",
                     "maxSupportedTransactionVersion": 0,
                     "commitment": "confirmed"},
                ])
                res = rpc_resp.get("result")
                if not res:
                    continue
                meta = res.get("meta", {}) or {}
                if meta.get("err"):
                    logger.warning(f"[TOKEN RESOLVE] {mint[:8]} — tx failed on chain: {meta.get('err')}")
                    return
                pre  = {t.get("accountIndex"): t.get("uiTokenAmount", {}).get("uiAmount") or 0
                        for t in (meta.get("preTokenBalances") or [])}
                for t in (meta.get("postTokenBalances") or []):
                    if t.get("mint") == mint and t.get("owner") == owner_str:
                        post_amt = t.get("uiTokenAmount", {}).get("uiAmount") or 0
                        delta = post_amt - pre.get(t.get("accountIndex"), 0)
                        if delta > 0:
                            decimals = t.get("uiTokenAmount", {}).get("decimals", 6)
                            result["tokens_expected"] = int(delta * (10 ** decimals))
                            logger.info(
                                f"[TOKEN RESOLVE] {mint[:8]} — {delta:,.0f} tokens "
                                f"(from tx receipt, attempt {attempt+1})"
                            )
                            return
            except Exception as e:
                logger.debug(f"[TOKEN RESOLVE] tx-receipt attempt {attempt+1} failed: {e}")

    # Fallback: poll wallet balance directly
    for attempt in range(retries):
        await asyncio.sleep(delay)
        try:
            balance = await wallet.get_token_balance(mint)
            if balance > 0:
                # pump.fun tokens use 6 decimals
                result["tokens_expected"] = int(balance * 1_000_000)
                logger.info(f"[TOKEN RESOLVE] {mint[:8]} — {balance:,.0f} tokens (wallet poll, attempt {attempt+1})")
                return
        except Exception as e:
            logger.debug(f"[TOKEN RESOLVE] wallet-poll attempt {attempt+1} failed: {e}")
    logger.warning(f"[TOKEN RESOLVE] {mint[:8]} — could not confirm token balance after {retries*2} attempts")


async def _prebuild_sell_for_position(executor, risk_manager, mint: str):
    """
    Background task: pre-build the sell-100% tx for a freshly-opened position
    and stash it on the Position. Lets emergency exits (rug, stop-loss) skip
    the PumpPortal _build_tx call (~200-500ms saved on the time-critical path).
    Solana blockhashes expire after ~60s, so risk_manager checks staleness
    before using the bytes.
    """
    try:
        tx_bytes = await executor.prebuild_sell_tx(mint)
        pos = risk_manager.positions.get(mint)
        if pos is not None and tx_bytes:
            pos.prebuilt_sell_tx = tx_bytes
            pos.prebuilt_sell_ts = time.time()
            logger.debug(f"[PREBUILD] sell-100% cached for {mint[:8]}... ({len(tx_bytes)} bytes)")
    except Exception as e:
        logger.debug(f"[PREBUILD] failed for {mint[:8]}: {e}")


async def trade_loop(trade_queue, executor, risk_manager, dashboard, wallet):
    while True:
        try:
            token = await asyncio.wait_for(trade_queue.get(), timeout=1.0)
        except TimeoutError:
            continue

        if risk_manager.emergency_stop_active:
            continue

        mint    = token["mint"]
        symbol  = token.get("symbol", "???")
        score   = token.get("score", 0)
        creator = token.get("creator", "")

        # Block creators that produced repeat losers (see config.CREATOR_BLACKLIST).
        if creator and creator in CREATOR_BLACKLIST:
            logger.info(f"[TRADE LOOP] Skip {symbol}: creator {creator[:8]}... is blacklisted")
            token["reject_reason"] = "creator_blacklisted"
            dashboard.record_signal(token)
            continue

        sol_amount, reject_reason = await risk_manager.calculate_position_size(score, symbol=symbol)
        if sol_amount <= 0:
            # reject_reason is one of: emergency_stop, paused_<sub>,
            # max_positions, symbol_cap, max_exposure, size_below_min.
            # Falls back to "no_capacity" if the sizer surfaced none (defensive).
            token["reject_reason"] = reject_reason or "no_capacity"
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
                # Pre-build the sell-100% tx async so an emergency exit
                # (rug, stop-loss) can skip the PumpPortal _build_tx
                # roundtrip (~200-500ms). The blockhash inside expires
                # after ~60s; risk_manager checks freshness before use.
                asyncio.create_task(_prebuild_sell_for_position(executor, risk_manager, mint))
            else:
                logger.warning(f"[TRADE LOOP] {symbol} buy confirmed but tokens unresolved — position skipped")
                token["reject_reason"] = "tokens_unresolved"
        else:
            token["reject_reason"] = result.get("error", "buy_failed")
            logger.error(f"[TRADE LOOP] Buy failed {symbol}: {token['reject_reason']}")

        dashboard.record_signal(token)


async def price_monitor_loop(risk_manager, executor):
    """Poll the freshest price source for each open position.

    For pump.fun bonding-curve tokens we read the bonding-curve PDA directly
    via getAccountInfo — instant freshness, no third-party lag. DexScreener
    polled at 5s was the strategy's biggest blind spot: by the time it
    reflected a +200% pump, the token was already rolling over and our
    sells fired into a worse fill.

    Fan out the per-position queries with asyncio.gather so a slow RPC on
    one mint can't starve the others.
    """
    import aiohttp

    from config import JUPITER_API_URL, RPC_URL, SOL_MINT

    async with aiohttp.ClientSession() as session:
        while True:
            mints = list(risk_manager.positions.keys())
            if mints:
                results = await asyncio.gather(
                    *[_get_token_price(session, m, JUPITER_API_URL, SOL_MINT, RPC_URL) for m in mints],
                    return_exceptions=True,
                )
                for mint, price in zip(mints, results, strict=True):
                    if isinstance(price, (int, float)) and price > 0:
                        risk_manager.update_price(mint, price)
                        if hasattr(executor, "update_price"):
                            executor.update_price(mint, price)
            await asyncio.sleep(1.5)


# pump.fun program ID — bonding-curve PDA derivation
_PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


async def _bonding_curve_price(session, mint: str, rpc_url: str) -> float:
    """Read the pump.fun bonding-curve PDA directly. Returns SOL per raw
    token unit, or 0 if the curve is missing (token migrated to Raydium).

    Layout: 8b discriminator + u64 v_token_reserves + u64 v_sol_reserves + ...
    Source of truth — no indexer lag, sub-second freshness.
    """
    try:
        from solders.pubkey import Pubkey
        prog = Pubkey.from_string(_PUMP_PROGRAM_ID)
        bc, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))], prog)
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [str(bc), {"encoding": "base64", "commitment": "confirmed"}],
        }
        async with session.post(rpc_url, json=payload,
                                timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status != 200:
                return 0.0
            data = await resp.json()
        val = (data.get("result") or {}).get("value")
        if not val:
            return 0.0  # migrated or curve closed
        import base64
        import struct
        raw = base64.b64decode(val["data"][0])
        # u64 LE: v_token_reserves, v_sol_reserves
        v_tok, v_sol = struct.unpack_from("<QQ", raw, 8)
        if v_tok == 0:
            return 0.0
        return (v_sol / 1e9) / v_tok  # SOL per raw token unit
    except Exception:
        return 0.0


async def _get_token_price(session, mint: str, jupiter_url: str, sol_mint: str, rpc_url: str) -> float:
    """Bonding-curve PDA first (fastest for pump.fun), then Jupiter (post-migration),
    then DexScreener as a last resort."""
    # 1) Bonding curve direct read — instant, no indexer lag
    price = await _bonding_curve_price(session, mint, rpc_url)
    if price > 0:
        return price

    # 2) Jupiter — relevant only for migrated/Raydium tokens
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

    # 3) DexScreener — last resort
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
            # Check if the old PID is still alive AND is actually a pump_bot
            # process. Without the second check, Windows PID recycling can
            # make us think a Kalshi-bot python (or any other script) is
            # "us" still running, and we refuse to start. That left the bot
            # offline whenever a sibling project happened to grab the
            # recycled PID. Tracked via the "kalshi blocks pump_bot" bug.
            alive = False
            try:
                if os.name == "nt":
                    import ctypes
                    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                    h = ctypes.windll.kernel32.OpenProcess(
                        PROCESS_QUERY_LIMITED_INFORMATION, False, old_pid
                    )
                    if h:
                        exit_code = ctypes.c_ulong(0)
                        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
                        ctypes.windll.kernel32.CloseHandle(h)
                        # STILL_ACTIVE = 259. Process exists, but we still
                        # need to confirm it's THIS bot.
                        process_exists = (exit_code.value == 259)
                    else:
                        process_exists = False

                    if process_exists:
                        # Verify the process is actually pump_bot by inspecting
                        # its command line. This is the bit the original code
                        # was missing.
                        try:
                            import subprocess
                            r = subprocess.run(
                                ["powershell", "-NoProfile", "-Command",
                                 f"(Get-CimInstance Win32_Process -Filter "
                                 f"\"ProcessId={old_pid}\" "
                                 f"-ErrorAction SilentlyContinue).CommandLine"],
                                capture_output=True, text=True, timeout=5,
                            )
                            cmdline = (r.stdout or "").strip().lower()
                            cwd = os.path.abspath(os.path.dirname(__file__)).lower()
                            alive = (
                                "main.py" in cmdline
                                and (cwd in cmdline or "pump_bot" in cmdline)
                            )
                        except Exception:
                            # If we can't verify, err on the side of taking
                            # over — being wrong here means a brief two-bot
                            # window, which the file locks below catch
                            # anyway. Being wrong the other way means
                            # the bot stays down forever.
                            alive = False
                else:
                    os.kill(old_pid, 0)
                    alive = True
            except Exception:
                alive = False

            if alive:
                logger.critical(
                    f"Another pump_bot is already running (PID {old_pid}). "
                    f"Refusing to start a duplicate. Exit 2."
                )
                sys.exit(2)
            else:
                logger.warning(f"Stale PID lock from dead/foreign PID {old_pid}, taking over")

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


def _install_global_error_handlers():
    """Catch silent crashes that bypass the top-level try/except.

    Without this, asyncio task exceptions and thread exceptions die
    inside the event loop with no traceback, leaving watchdog.log with
    nothing but `Bot exited (code=1)`. Routes everything through loguru.
    """
    def _async_handler(loop, context):
        exc = context.get("exception")
        msg = context.get("message", "asyncio error")
        if exc is not None:
            logger.opt(exception=exc).critical(f"[ASYNCIO CRASH] {msg}")
        else:
            logger.critical(f"[ASYNCIO CRASH] {msg} | context={context}")

    asyncio.get_event_loop().set_exception_handler(_async_handler)

    def _sync_handler(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.opt(exception=(exc_type, exc, tb)).critical("[SYS CRASH] uncaught exception")
    sys.excepthook = _sync_handler

    def _thread_handler(args):
        if issubclass(args.exc_type, SystemExit):
            return
        logger.opt(exception=(args.exc_type, args.exc_value, args.exc_traceback)).critical(
            f"[THREAD CRASH] {args.thread.name if args.thread else 'unknown'}"
        )
    threading.excepthook = _thread_handler

    faulthandler.enable()  # dumps C-level stack on segfault to stderr


async def main():
    setup_logging()
    _install_global_error_handlers()
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
    reporter        = ReportLogger(risk_mgr, scorer)
    daily_reporter  = DailyReporter(risk_mgr, scorer)
    web_dash        = WebDashboard(risk_mgr, scorer, dashboard, PAPER_TRADING, report_logger=reporter)

    pumpfun_monitor  = PumpFunMonitor(raw_queue)
    # Tracker piggybacks on the monitor's WS via callback (no own connection).
    pumpfun_tracker  = PumpFunTracker(monitor=pumpfun_monitor)
    # Wallet intel attaches before monitor.run() so it sees every event.
    wallet_intel.attach(pumpfun_monitor)
    # Whale tracker piggybacks on the same shared WS — aggregates SOL
    # volume per wallet so the scorer sees "real money bought this".
    whale_tracker.attach(pumpfun_monitor)
    # Regime filter also subscribes to creates to maintain its sliding window
    # of new-mint rates. No state to seed — bootstrap-safe.
    regime_filter.attach(pumpfun_monitor)
    # Auto-tuner needs the live risk_manager to read closed_trades.
    auto_tuner.attach(risk_mgr)
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
        asyncio.create_task(auto_tuner.run(),                                               name="auto_tuner"),
        asyncio.create_task(daily_reporter.run(),                                           name="daily_report"),
    ]

    # Dev-only: if SYNTHETIC_INJECT is set, push N synthetic tokens through
    # the raw_queue at startup. Used when the live PumpPortal WS is
    # unreachable (sandboxed IPs) to prove the rest of the decision
    # pipeline fires end-to-end. SAFE only in PAPER_TRADING mode.
    _synth_n = os.environ.get("SYNTHETIC_INJECT", "")
    if _synth_n and PAPER_TRADING:
        try:
            n = max(1, int(_synth_n))
            from tools.synthetic_injector import inject_synthetic_tokens
            tasks.append(asyncio.create_task(
                inject_synthetic_tokens(raw_queue, count=n, interval_s=6.0),
                name="synthetic_inject",
            ))
        except Exception as e:
            logger.warning(f"[SYNTHETIC] failed to start injector: {e}")

        # Auto-enable the price mover whenever synthetic injection is on
        # (you ~never want one without the other). Operator can disable
        # explicitly via SYNTHETIC_PRICE_MOVES=0. Synthetic mints have no
        # on-chain price feed; without the mover, every position exits
        # via no_movement at full-friction loss and the exit-logic
        # branches (TP, trailing stop, stop loss) never fire.
        if os.environ.get("SYNTHETIC_PRICE_MOVES", "1").lower() in ("1", "true", "yes", "on"):
            try:
                from tools.synthetic_price_mover import SyntheticPriceMover
                mover = SyntheticPriceMover(risk_mgr, executor)
                tasks.append(asyncio.create_task(
                    mover.run(),
                    name="synthetic_price_mover",
                ))
            except Exception as e:
                logger.warning(f"[SYNTHETIC] failed to start price mover: {e}")
    elif _synth_n and not PAPER_TRADING:
        logger.critical(
            "[SYNTHETIC] Refusing to inject synthetic tokens in LIVE mode "
            "(SYNTHETIC_INJECT only valid with PAPER_TRADING=true)."
        )

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
    auto_tuner.stop()
    daily_reporter.stop()
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
