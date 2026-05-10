"""
logger/daily_report.py

Midnight-local-time email summary. Aggregates the last 24 hours of:
  - Closed trades (count, WR, PnL, biggest winner/loser)
  - New rug patterns added to memory (passive learning + live trades)
  - New ruggers blacklisted
  - New bot wallets crossed the threshold
  - Auto-tuner adjustments
  - Counterfactual filter effectiveness
  - Bot config snapshot

Send via SMTP (Gmail App Password works out of the box). Disabled until
SMTP_HOST + REPORT_EMAIL_TO are set in .env.
"""

import asyncio
import json
import os
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from loguru import logger

from config import (
    REPORT_EMAIL_FROM,
    REPORT_EMAIL_TO,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)


def _enabled() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and REPORT_EMAIL_TO)


def _read_jsonl(path: str, since_ts: float = 0) -> list:
    """Read JSONL, returning rows newer than since_ts (using 'ts' or 'exit_time' field)."""
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("ts") or rec.get("exit_time") or rec.get("resolved_ts") or 0
                if ts >= since_ts:
                    out.append(rec)
    except Exception as e:
        logger.debug(f"[REPORT] read {path} failed: {e}")
    return out


def _read_json(path: str) -> dict | list | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


class DailyReporter:
    def __init__(self, risk_mgr, scorer):
        self.risk_mgr = risk_mgr
        self.scorer = scorer
        self.running = False

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def run(self):
        if not _enabled():
            logger.info("[REPORT] Daily report disabled (SMTP not configured)")
            return
        self.running = True
        logger.info(f"[REPORT] Daily reporter started — recipient: {REPORT_EMAIL_TO}")
        while self.running:
            now = datetime.now()
            target = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=5, microsecond=0
            )
            sleep_s = max(60, (target - now).total_seconds())
            try:
                await asyncio.sleep(sleep_s)
            except asyncio.CancelledError:
                break
            if not self.running:
                break
            try:
                self.send_report()
            except Exception as e:
                logger.warning(f"[REPORT] send failed: {e}")

    def stop(self):
        self.running = False

    # ── Stats gathering ─────────────────────────────────────────────────────
    def _gather(self) -> dict:
        now = time.time()
        day_ago = now - 86400

        trades = _read_jsonl("logs/closed_trades.jsonl", day_ago)
        rugs   = _read_jsonl("logs/rug_patterns.jsonl",  day_ago)
        cf     = _read_jsonl("logs/counterfactual.jsonl", day_ago)

        # Trade aggregates
        wins = sum(1 for t in trades if t.get("pnl_sol", 0) > 0)
        losses = len(trades) - wins
        total_pnl = sum(t.get("pnl_sol", 0) for t in trades)
        wr = (wins / len(trades) * 100) if trades else 0.0

        biggest_win  = max(trades, key=lambda t: t.get("pnl_sol", 0), default=None)
        biggest_loss = min(trades, key=lambda t: t.get("pnl_sol", 0), default=None)

        # Counterfactual: were our filters working today?
        cf_rugs = sum(1 for r in cf if r.get("mc_delta_pct", 0) <= -50)
        cf_wins = sum(1 for r in cf if r.get("mc_delta_pct", 0) >= 100)

        # Memory snapshots
        bot_wallets = _read_json("logs/bot_wallets.json") or {}
        n_bot_wallets = sum(1 for w in bot_wallets.values() if w.get("buys", 0) >= 25)

        ruggers = _read_json("logs/rugger_creators.json") or {}
        if isinstance(ruggers, dict):
            n_ruggers = len(ruggers.get("creators", []))
        else:
            n_ruggers = len(ruggers)

        # Total rug-pattern records (JSONL, count lines directly)
        n_rug_patterns_total = 0
        if os.path.exists("logs/rug_patterns.jsonl"):
            with open("logs/rug_patterns.jsonl", encoding="utf-8") as f:
                n_rug_patterns_total = sum(1 for _ in f)

        # Auto-tuner state
        at_state = _read_json("logs/auto_tune_state.json") or {}

        # Live bot status
        balance = 0.0
        try:
            # We can't await here — use cached/last-known
            balance = getattr(self.risk_mgr.wallet, "_last_known_sol", 0) or 0
        except Exception:
            pass

        return {
            "now":               now,
            "trades":            trades,
            "wins":              wins,
            "losses":            losses,
            "total_pnl":         total_pnl,
            "wr":                wr,
            "biggest_win":       biggest_win,
            "biggest_loss":      biggest_loss,
            "rugs_today":        len(rugs),
            "cf_total":          len(cf),
            "cf_rugs":           cf_rugs,
            "cf_wins":           cf_wins,
            "n_bot_wallets":     n_bot_wallets,
            "n_ruggers":         n_ruggers,
            "n_rug_patterns":    n_rug_patterns_total,
            "auto_tuner":        at_state,
            "balance":           balance,
            "open_positions":    len(self.risk_mgr.positions),
            "scored_count":      getattr(self.scorer, "scored_count", 0),
        }

    # ── Rendering ───────────────────────────────────────────────────────────
    def _render(self, s: dict) -> str:
        date_str = datetime.fromtimestamp(s["now"]).strftime("%Y-%m-%d")

        def sign(v: float) -> str:
            return f"+{v:.4f}" if v >= 0 else f"{v:.4f}"

        bw_sym = s["biggest_win"]["symbol"]  if s["biggest_win"]  else "—"
        bw_pnl = sign(s["biggest_win"]["pnl_sol"])  if s["biggest_win"]  else "—"
        bl_sym = s["biggest_loss"]["symbol"] if s["biggest_loss"] else "—"
        bl_pnl = sign(s["biggest_loss"]["pnl_sol"]) if s["biggest_loss"] else "—"

        cf_rug_pct = (s["cf_rugs"] / s["cf_total"] * 100) if s["cf_total"] else 0
        cf_win_pct = (s["cf_wins"] / s["cf_total"] * 100) if s["cf_total"] else 0

        at = s["auto_tuner"]
        at_offset = at.get("offset", 0)
        at_eff    = at.get("effective", "?")
        at_action = at.get("last_action", "—")

        return f"""
<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#222;max-width:680px;">
  <h2 style="border-bottom:2px solid #333;padding-bottom:6px;">PumpBot Daily — {date_str}</h2>

  <h3>Trading (last 24h)</h3>
  <table cellpadding="6" style="border-collapse:collapse;">
    <tr><td><b>Closed trades</b></td><td>{len(s["trades"])}</td></tr>
    <tr><td><b>Win rate</b></td><td>{s["wr"]:.1f}% ({s["wins"]}W / {s["losses"]}L)</td></tr>
    <tr><td><b>Total PnL (realized)</b></td><td>{sign(s["total_pnl"])} SOL</td></tr>
    <tr><td><b>Biggest win</b></td><td>{bw_sym} ({bw_pnl} SOL)</td></tr>
    <tr><td><b>Biggest loss</b></td><td>{bl_sym} ({bl_pnl} SOL)</td></tr>
    <tr><td><b>Wallet (last known)</b></td><td>{s["balance"]:.4f} SOL</td></tr>
    <tr><td><b>Open positions</b></td><td>{s["open_positions"]}</td></tr>
  </table>

  <h3>What the bot learned today</h3>
  <table cellpadding="6" style="border-collapse:collapse;">
    <tr><td><b>New rug patterns recorded</b></td><td>{s["rugs_today"]}</td></tr>
    <tr><td><b>Total rug patterns in memory</b></td><td>{s["n_rug_patterns"]}</td></tr>
    <tr><td><b>Tracked sniper-bot wallets (≥25 buys)</b></td><td>{s["n_bot_wallets"]}</td></tr>
    <tr><td><b>Total ruggers in blacklist</b></td><td>{s["n_ruggers"]}</td></tr>
    <tr><td><b>Counterfactual: rejections that rugged</b></td><td>{s["cf_rugs"]} / {s["cf_total"]} ({cf_rug_pct:.1f}%) — filters were right</td></tr>
    <tr><td><b>Counterfactual: rejections that pumped &gt;100%</b></td><td>{s["cf_wins"]} / {s["cf_total"]} ({cf_win_pct:.1f}%) — filters cost us</td></tr>
  </table>

  <h3>Auto-tuner</h3>
  <table cellpadding="6" style="border-collapse:collapse;">
    <tr><td><b>Score offset</b></td><td>{at_offset:+d}</td></tr>
    <tr><td><b>Effective threshold</b></td><td>{at_eff}</td></tr>
    <tr><td><b>Last action</b></td><td>{at_action}</td></tr>
    <tr><td><b>Lifetime adjustments</b></td><td>{at.get("adjustment_count", 0)}</td></tr>
  </table>

  <h3>Activity volume</h3>
  <table cellpadding="6" style="border-collapse:collapse;">
    <tr><td><b>Tokens scored today</b></td><td>{s["scored_count"]}</td></tr>
  </table>

  <p style="color:#888;font-size:11px;margin-top:30px;">
    Auto-generated by PumpBot. Data sources: closed_trades.jsonl,
    rug_patterns.jsonl, counterfactual.jsonl, bot_wallets.json,
    rugger_creators.json, auto_tune_state.json.
  </p>
</body></html>
"""

    # ── Send ────────────────────────────────────────────────────────────────
    def send_report(self):
        if not _enabled():
            return
        stats = self._gather()
        html = self._render(stats)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[PumpBot] Daily — {datetime.now().strftime('%Y-%m-%d')} | "\
                        f"{len(stats['trades'])} trades, {stats['wr']:.0f}% WR, "\
                        f"{stats['total_pnl']:+.3f} SOL"
        msg["From"]    = REPORT_EMAIL_FROM
        msg["To"]      = REPORT_EMAIL_TO
        msg.attach(MIMEText(html, "html"))

        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
            logger.success(f"[REPORT] Sent to {REPORT_EMAIL_TO}")
        except Exception as e:
            logger.warning(f"[REPORT] SMTP send failed: {e}")
