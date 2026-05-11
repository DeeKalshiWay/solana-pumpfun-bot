"""
logger/web_server.py
Local HTTP dashboard for the pump bot. Serves a single-page web UI at
http://127.0.0.1:8765/  that reads live state from the running bot.

API endpoints:
  GET /              - dashboard.html
  GET /api/status    - bot status, balance, stats
  GET /api/positions - currently open positions
  GET /api/trades    - closed trades (most recent first)
  GET /api/signals   - recent scored tokens (most recent first)
  GET /api/creators  - creator leaderboard
"""

import asyncio
import base64
import binascii
import hmac
import os
import socket
import time

from aiohttp import web
from loguru import logger

from analyzer.auto_tuner import auto_tuner
from analyzer.counterfactual import counterfactual
from analyzer.score_bins import aggregate_by_score
from detector.creator_tracker import creator_tracker
from detector.influencer_monitor import influencer_monitor
from detector.wallet_intel import wallet_intel


def _get_lan_ip() -> str:
    """Best-effort detection of this machine's LAN IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))   # doesn't actually send, just opens a route
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "your-pc-ip"


WEB_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
WEB_HOST = "0.0.0.0"   # listen on all interfaces (LAN-accessible)
WEB_PORT = 8765

# Optional HTTP Basic Auth. If both env vars are set, every dashboard route
# (HTML, API, control POSTs) requires the credentials. Recommended whenever
# WEB_HOST is anything other than 127.0.0.1, since the control endpoints can
# emergency-stop the bot or force-sell open positions.
_AUTH_USER = os.environ.get("DASHBOARD_AUTH_USER", "")
_AUTH_PASS = os.environ.get("DASHBOARD_AUTH_PASS", "")
_AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASS)


def _check_basic_auth(header_value: str) -> bool:
    """Constant-time check of a 'Basic <base64>' Authorization header."""
    if not header_value or not header_value.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header_value.split(" ", 1)[1], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    user, _, pw = decoded.partition(":")
    return (
        hmac.compare_digest(user, _AUTH_USER)
        and hmac.compare_digest(pw, _AUTH_PASS)
    )


class WebDashboard:
    """
    Holds references to the live components so endpoints can read state.
    Started as an asyncio task from main.py.
    """

    def __init__(self, risk_manager, signal_scorer, dashboard, paper_trading: bool, report_logger=None):
        self.risk_mgr      = risk_manager
        self.scorer        = signal_scorer
        self.dashboard     = dashboard       # logger.dashboard.Dashboard (recent_signals)
        self.paper_trading = paper_trading
        self.report_logger = report_logger
        self.start_time    = time.time()
        self._runner: web.AppRunner | None = None

    # ── Routes ────────────────────────────────────────────────────────────────
    async def index(self, request):
        path = os.path.join(WEB_DIR, "dashboard.html")
        if not os.path.exists(path):
            return web.Response(text="dashboard.html missing", status=500)
        return web.FileResponse(path)

    async def api_status(self, request):
        stats   = self.risk_mgr.get_stats()
        balance = await self.risk_mgr.wallet.get_sol_balance()
        start   = self.risk_mgr.starting_sol_balance or 1.0
        # Balance-based PnL is the source of truth — captures pre-persistence gains too.
        balance_pnl_sol = round(balance - start, 6)
        balance_pnl_pct = round((balance - start) / start * 100, 2) if start > 0 else 0

        # Mark-to-market: add current value of open positions (current_price × tokens_held)
        # so the headline number reflects unrealized gains/losses instead of just wallet flow.
        mtm_open_value = 0.0
        for pos in self.risk_mgr.positions.values():
            try:
                mtm_open_value += float(pos.current_price or 0) * float(pos.tokens_held or 0)
            except Exception:
                pass
        mtm_total_sol = balance + mtm_open_value
        mtm_pnl_sol   = round(mtm_total_sol - start, 6)
        mtm_pnl_pct   = round((mtm_total_sol - start) / start * 100, 2) if start > 0 else 0

        return web.json_response({
            "paper_trading":   self.paper_trading,
            "uptime_seconds":  int(time.time() - self.start_time),
            "balance_sol":     round(balance, 6),
            "starting_sol":    round(start, 6),
            "open_positions":  stats["open_positions"],
            "closed_trades":   stats["closed_trades"],
            "win_rate":        round(stats["win_rate"] * 100, 1),
            "total_pnl_sol":   balance_pnl_sol,
            "total_pnl_pct":   balance_pnl_pct,
            "mtm_open_value_sol": round(mtm_open_value, 6),
            "mtm_total_sol":   round(mtm_total_sol, 6),
            "mtm_pnl_sol":     mtm_pnl_sol,
            "mtm_pnl_pct":     mtm_pnl_pct,
            "record_pnl_sol":  round(stats["total_pnl_sol"], 6),
            "total_exposure":  round(stats["total_exposure"], 6),
            "emergency_stop":  stats["emergency_stop"],
            "paused":          stats.get("paused", False),
            "pause_reason":    stats.get("pause_reason", ""),
            "consecutive_losses": stats.get("consecutive_losses", 0),
            "scored_count":    self.scorer.scored_count,
            "auto_tuner":      auto_tuner.stats(),
        })

    async def api_positions(self, request):
        out = []
        for mint, pos in self.risk_mgr.positions.items():
            out.append({
                "mint":           mint,
                "symbol":         pos.symbol,
                "creator":        pos.creator,
                "sol_invested":   round(pos.sol_invested, 6),
                "tokens_held":    pos.tokens_held,
                "entry_price":    pos.entry_price_sol,
                "current_price":  pos.current_price,
                "highest_price":  pos.highest_price,
                "pnl_pct":        round(pos.pnl_pct, 2),
                "age_minutes":    round(pos.age_minutes, 1),
                "score":          pos.score,
                "tp_levels_hit":  pos.tp_levels_hit,
            })
        return web.json_response(out)

    async def api_trades(self, request):
        # Most recent first, capped at 50
        trades = list(reversed(self.risk_mgr.closed_trades))[:50]
        return web.json_response(trades)

    async def api_signals(self, request):
        out = []
        for token in self.dashboard.recent_signals[:30]:
            out.append({
                "symbol":         token.get("symbol", "???"),
                "mint":           token.get("mint", ""),
                "score":          token.get("score", 0),
                "market_cap_sol": token.get("market_cap_sol", 0),
                "initial_buy":    token.get("initial_buy_sol", 0),
                "curve_pct":      token.get("bonding_curve_pct", 0),
                "queued":         bool(token.get("queued_for_buy")),
                "reject_reason":  token.get("reject_reason", ""),
                "scored_at":      token.get("scored_at", 0),
                "breakdown":      token.get("score_breakdown", {}),
            })
        return web.json_response(out)

    async def api_creators(self, request):
        return web.json_response({
            "top10":      creator_tracker.get_leaderboard(10),
            "total_known": len(creator_tracker._db),
        })

    async def api_intel(self, request):
        """Tier 4 intel feeds: bot wallets, bundles, influencer mentions."""
        bot_wallets = sorted(
            [(addr, w.get("buys", 0)) for addr, w in wallet_intel._wallets.items()
             if w.get("buys", 0) >= 10],
            key=lambda x: x[1], reverse=True
        )[:25]

        # Recent bundle decisions (last 25)
        bundle_items = list(wallet_intel._bundle_decided.items())[-25:]

        # Recent influencer mentions
        influencer_hits = [
            {"symbol": k, "handle": v.get("handle", ""), "ts": v.get("ts", 0)}
            for k, v in influencer_monitor._mentions.items()
        ]

        return web.json_response({
            "bot_wallets":      [{"addr": a[:14] + "...", "buys": n} for a, n in bot_wallets],
            "bot_wallet_count": wallet_intel.get_known_bots_count(),
            "wallets_tracked":  len(wallet_intel._wallets),
            "bundles": [
                {"mint": m[:8] + "...", "is_bundled": b}
                for m, b in bundle_items
            ],
            "bundles_seen":      len(wallet_intel._bundle_decided),
            "bundles_flagged":   sum(1 for v in wallet_intel._bundle_decided.values() if v),
            "influencer_hits":   influencer_hits,
            "influencer_handles": influencer_monitor._handles,
        })

    async def api_learn(self, request):
        """
        Two learning loops:
          counterfactual: outcomes of REJECTED tokens by reject reason
          score_bins:     outcomes of TAKEN trades bucketed by entry score
        """
        cf = counterfactual.aggregate()
        sb = aggregate_by_score()
        return web.json_response({
            "counterfactual": cf,
            "score_bins":     sb,
            "pending_resolutions": len(counterfactual._pending),
        })

    # ── Control endpoints (POST) ──────────────────────────────────────────────
    async def api_emergency_stop(self, request):
        """
        Big red button. Forces emergency_stop_active = True so:
          - calculate_position_size returns 0 → no new buys
          - _check_emergency_stop loop force-sells every open position
        Body: {"confirm": true}  (defensive against stray GETs / random hits)
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not body.get("confirm"):
            return web.json_response({"ok": False, "error": "missing confirm"}, status=400)
        self.risk_mgr.emergency_stop_active = True
        logger.critical("[DASHBOARD] EMERGENCY STOP triggered via web UI — force-selling all positions")
        return web.json_response({"ok": True, "emergency_stop": True})

    async def api_force_sell(self, request):
        """
        Sell one open position immediately. Body: {"mint": "...", "confirm": true}
        Uses risk_mgr._force_sell which executes a 100% market sell and
        runs the normal close_position bookkeeping (DB write, alerts, etc.).
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not body.get("confirm"):
            return web.json_response({"ok": False, "error": "missing confirm"}, status=400)
        mint = body.get("mint", "")
        if not mint:
            return web.json_response({"ok": False, "error": "missing mint"}, status=400)
        if mint not in self.risk_mgr.positions:
            return web.json_response({"ok": False, "error": "position not found"}, status=404)
        symbol = self.risk_mgr.positions[mint].symbol
        logger.warning(f"[DASHBOARD] Force-sell requested via web UI: {symbol} ({mint[:8]}...)")
        # Don't await — the sell can take a few seconds and we don't want
        # the HTTP request hanging. Fire-and-forget; UI polls /api/positions.
        asyncio.create_task(self.risk_mgr._force_sell(mint, "manual_force_sell"))
        return web.json_response({"ok": True, "mint": mint, "symbol": symbol})

    async def api_emergency_resume(self, request):
        """Clear the emergency stop. Body: {"confirm": true}"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not body.get("confirm"):
            return web.json_response({"ok": False, "error": "missing confirm"}, status=400)
        self.risk_mgr.emergency_stop_active = False
        # Also clear force-sell in case auto-drawdown trigger had set it
        self.risk_mgr.emergency_force_sell = False
        logger.warning("[DASHBOARD] Emergency stop cleared via web UI — trading resumed")
        return web.json_response({"ok": True, "emergency_stop": False})

    async def api_report(self, request):
        """Return all snapshots + a verdict for the dashboard chart."""
        if not self.report_logger:
            return web.json_response({
                "snapshots": [],
                "verdict":   {"status": "off", "label": "DISABLED", "color": "gold",
                              "message": "Report logger not running."},
            })
        snaps   = self.report_logger.load_all()
        verdict = self.report_logger.verdict(snaps)
        return web.json_response({
            "snapshots": snaps,
            "verdict":   verdict,
            "needed_hours_for_verdict": 24 * 7,
        })

    # ── Server lifecycle ──────────────────────────────────────────────────────
    @web.middleware
    async def _auth_middleware(self, request, handler):
        # Pre-flight requests can't carry an Authorization header; let CORS handle them.
        if not _AUTH_ENABLED or request.method == "OPTIONS":
            return await handler(request)
        if not _check_basic_auth(request.headers.get("Authorization", "")):
            return web.Response(
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="pump-bot dashboard", charset="UTF-8"'},
                text="Unauthorized",
            )
        return await handler(request)

    @web.middleware
    async def _cors_middleware(self, request, handler):
        # Allow the GitHub Pages portfolio site to fetch /api/* across origins.
        # Read-only API; no credentials sent.
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return resp

    async def run(self):
        app = web.Application(middlewares=[self._auth_middleware, self._cors_middleware])
        app.router.add_get("/",              self.index)
        app.router.add_get("/api/status",    self.api_status)
        app.router.add_get("/api/positions", self.api_positions)
        app.router.add_get("/api/trades",    self.api_trades)
        app.router.add_get("/api/signals",   self.api_signals)
        app.router.add_get("/api/creators",  self.api_creators)
        app.router.add_get("/api/intel",     self.api_intel)
        app.router.add_get("/api/learn",     self.api_learn)
        app.router.add_get("/api/report",    self.api_report)
        app.router.add_post("/api/emergency_stop",   self.api_emergency_stop)
        app.router.add_post("/api/emergency_resume", self.api_emergency_resume)
        app.router.add_post("/api/force_sell",       self.api_force_sell)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, WEB_HOST, WEB_PORT)
        await site.start()
        lan_ip = _get_lan_ip()
        logger.success("Web dashboard running:")
        logger.success(f"  Local:    http://127.0.0.1:{WEB_PORT}/")
        logger.success(f"  LAN/phone: http://{lan_ip}:{WEB_PORT}/")
        if _AUTH_ENABLED:
            logger.success(f"  Basic Auth: ENABLED (user='{_AUTH_USER}')")
        elif WEB_HOST != "127.0.0.1":
            logger.warning(
                "Dashboard auth is DISABLED and bound to {host}: anyone on the network can "
                "trigger emergency-stop / force-sell. Set DASHBOARD_AUTH_USER and "
                "DASHBOARD_AUTH_PASS to require Basic Auth.", host=WEB_HOST,
            )

        # Keep alive until cancelled
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await self._runner.cleanup()
            raise

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
