"""
tools/preflight.py — pre-live self-check for pump_bot.

Runs ~10 fast checks against the local environment and reports
green/warn/fail for each. Exits non-zero if any FAIL fires; warnings
are surfaced but non-blocking. Designed to be run before each live
session, before flipping `PAPER_TRADING=False`, and as a CI step.

Usage:
  python -m tools.preflight                # default (live-readiness mode)
  python -m tools.preflight --paper        # paper-mode (relaxed)
  python -m tools.preflight --json         # machine-readable output
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Allow running as `python tools/preflight.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# Load .env with override so values there beat any stale system-level env
# vars (e.g., a long-set Windows user-environment SOLANA_PRIVATE_KEY).
# Mirrors config.py's behavior so preflight sees the same values the bot
# actually runs with.
load_dotenv(override=True)

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""

    @property
    def is_blocker(self) -> bool:
        return self.status == FAIL


# ── Individual checks ────────────────────────────────────────────────────
def check_env_file() -> Result:
    if not Path(".env").exists():
        return Result("env file", FAIL, ".env not found in repo root")
    return Result("env file", PASS, ".env found")


def check_private_key() -> Result:
    pk = os.getenv("SOLANA_PRIVATE_KEY", "")
    if not pk or pk == "YOUR_PRIVATE_KEY_HERE":
        return Result("solana private key", FAIL, "SOLANA_PRIVATE_KEY missing or placeholder")
    if len(pk) < 80:
        return Result("solana private key", WARN, f"key length {len(pk)} looks short for base58")
    return Result("solana private key", PASS, f"set ({pk[:6]}…{pk[-4:]})")


def check_helius_key() -> Result:
    k = os.getenv("HELIUS_API_KEY", "")
    if not k or k == "YOUR_HELIUS_KEY_HERE":
        return Result("helius api key", WARN,
                      "HELIUS_API_KEY missing — falling back to public RPC (slower, rate-limited)")
    return Result("helius api key", PASS, "set")


async def check_rpc_reachable() -> Result:
    rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getSlot"}
    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(rpc_url, json=payload,
                              timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
        dt_ms = (time.monotonic() - t0) * 1000
        if "result" not in data:
            return Result("rpc reachable", FAIL, f"got {data} (no result)")
        if dt_ms > 800:
            return Result("rpc reachable", WARN, f"slot ok but {dt_ms:.0f} ms (slow)")
        return Result("rpc reachable", PASS, f"slot {data['result']} in {dt_ms:.0f} ms")
    except Exception as e:
        return Result("rpc reachable", FAIL, f"{type(e).__name__}: {e}")


async def check_wallet_balance(min_sol: float) -> Result:
    pk = os.getenv("SOLANA_PRIVATE_KEY", "")
    if not pk or pk == "YOUR_PRIVATE_KEY_HERE":
        return Result("wallet balance", FAIL, "no private key — cannot derive pubkey")
    try:
        import base58
        from solders.keypair import Keypair  # type: ignore
        kp = Keypair.from_bytes(base58.b58decode(pk))
        pubkey = str(kp.pubkey())
    except Exception as e:
        return Result("wallet balance", FAIL, f"keypair derive failed: {e}")

    rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey]}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(rpc_url, json=payload,
                              timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
        lamports = data.get("result", {}).get("value", 0)
        sol = lamports / 1e9
    except Exception as e:
        return Result("wallet balance", FAIL, f"{type(e).__name__}: {e}")

    if sol < min_sol:
        return Result("wallet balance", FAIL,
                      f"{sol:.6f} SOL on {pubkey[:6]}… (below {min_sol} SOL threshold)")
    return Result("wallet balance", PASS, f"{sol:.6f} SOL on {pubkey[:6]}…")


def check_paper_mode_intent(want_live: bool) -> Result:
    import config
    if want_live and config.PAPER_TRADING:
        return Result("PAPER_TRADING flag", FAIL,
                      "live mode requested but config.PAPER_TRADING is True — "
                      "set False before going live")
    if not want_live and not config.PAPER_TRADING:
        return Result("PAPER_TRADING flag", FAIL,
                      "paper mode requested but config.PAPER_TRADING is False — "
                      "real trading is enabled")
    state = "live (real money)" if not config.PAPER_TRADING else "paper (sim)"
    return Result("PAPER_TRADING flag", PASS, f"PAPER_TRADING={config.PAPER_TRADING} ({state})")


def check_emergency_stop(want_live: bool) -> Result:
    import config
    pct = config.EMERGENCY_STOP_DRAWDOWN_PCT
    if want_live and pct > 25:
        return Result("emergency stop", WARN,
                      f"EMERGENCY_STOP_DRAWDOWN_PCT={pct}% — recommend ≤25% for first month live")
    if pct >= 100 or pct <= 0:
        return Result("emergency stop", FAIL, f"EMERGENCY_STOP_DRAWDOWN_PCT={pct} is nonsensical")
    return Result("emergency stop", PASS, f"{pct}%")


def check_risk_sizing(wallet_sol: float | None) -> Result:
    import config
    cap_pct = config.MAX_POSITION_PCT
    cap_sol = config.MAX_SOL_PER_TRADE
    if cap_pct <= 0 or cap_pct > 1:
        return Result("risk sizing", FAIL, f"MAX_POSITION_PCT={cap_pct} out of [0,1]")
    if cap_sol <= 0:
        return Result("risk sizing", FAIL, f"MAX_SOL_PER_TRADE={cap_sol}")
    if wallet_sol is not None and cap_sol > wallet_sol * cap_pct:
        return Result(
            "risk sizing", WARN,
            f"MAX_SOL_PER_TRADE={cap_sol} exceeds wallet({wallet_sol:.3f}) × pct({cap_pct})"
            f" = {wallet_sol*cap_pct:.3f} — pct will bind",
        )
    return Result("risk sizing", PASS,
                  f"per-trade ≤ {cap_sol} SOL, ≤ {cap_pct*100:.1f}% of wallet")


def check_tp_ladder() -> Result:
    import config
    levels = config.TAKE_PROFIT_LEVELS
    if not levels:
        return Result("tp ladder", FAIL, "TAKE_PROFIT_LEVELS empty")
    gains = [lvl["gain_pct"] for lvl in levels]
    if gains != sorted(gains):
        return Result("tp ladder", FAIL, f"non-monotonic: {gains}")
    sells = sum(lvl["sell_pct"] for lvl in levels)
    if sells >= 100:
        return Result("tp ladder", FAIL, f"sells sum to {sells}% — nothing rides for moonshot")
    return Result("tp ladder", PASS,
                  f"{len(levels)} levels, {sells}% sold across ladder, {100-sells}% rides")


def check_trade_db() -> Result:
    db_path = Path("logs/trades.db")
    if not db_path.exists():
        return Result("trade db", WARN,
                      "logs/trades.db missing — run `python -m tools.migrate_jsonl_to_sqlite`")
    try:
        con = sqlite3.connect(str(db_path))
        n = con.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]
        con.close()
    except Exception as e:
        return Result("trade db", FAIL, f"{type(e).__name__}: {e}")
    return Result("trade db", PASS, f"{n} closed trades indexed")


def check_pid_lock() -> Result:
    pid_file = Path("logs/bot.pid")
    if not pid_file.exists():
        return Result("pid lock", PASS, "no lock file (will be created on bot start)")
    try:
        old_pid = int(pid_file.read_text().strip())
    except Exception:
        return Result("pid lock", WARN, "pid lock unreadable — will be overwritten on next start")

    if os.name == "nt":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter \"ProcessId={old_pid}\" "
                 f"-ErrorAction SilentlyContinue).CommandLine"],
                capture_output=True, text=True, timeout=5,
            )
            cmdline = (r.stdout or "").strip().lower()
            cwd = os.path.abspath(os.path.dirname(__file__) + "/..").lower()
            is_pump = "main.py" in cmdline and (cwd in cmdline or "pump_bot" in cmdline)
            if cmdline and is_pump:
                return Result("pid lock", PASS, f"held by live pump_bot PID {old_pid}")
            if cmdline and not is_pump:
                return Result("pid lock", WARN,
                              f"PID {old_pid} is alive but not pump_bot — stale lock; "
                              f"the fix in main.py will take it over on next start")
            return Result("pid lock", PASS, f"stale lock from dead PID {old_pid}")
        except Exception as e:
            return Result("pid lock", WARN, f"verify failed: {e}")
    return Result("pid lock", PASS, "(non-Windows)")


async def check_bot_running() -> Result:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:8765/api/status",
                             timeout=aiohttp.ClientTimeout(total=3)) as r:
                data = await r.json()
        uptime = data.get("uptime_seconds", 0)
        bal = data.get("balance_sol", 0)
        return Result("bot process", PASS,
                      f"alive, uptime {uptime//60}m, balance {bal:.2f} SOL")
    except Exception:
        return Result("bot process", WARN,
                      "dashboard API unreachable on :8765 — bot may not be running")


# ── Driver ───────────────────────────────────────────────────────────────
async def run_checks(want_live: bool, min_balance: float) -> list[Result]:
    results: list[Result] = []

    # Cheap synchronous checks first.
    results.append(check_env_file())
    results.append(check_private_key())
    results.append(check_helius_key())
    results.append(check_paper_mode_intent(want_live))
    results.append(check_emergency_stop(want_live))
    results.append(check_tp_ladder())
    results.append(check_trade_db())
    results.append(check_pid_lock())

    # Network checks (parallel).
    rpc_res, bal_res, bot_res = await asyncio.gather(
        check_rpc_reachable(),
        check_wallet_balance(min_balance),
        check_bot_running(),
    )
    results.append(rpc_res)
    results.append(bal_res)

    wallet_sol = None
    if bal_res.status == PASS:
        try:
            wallet_sol = float(bal_res.detail.split()[0])
        except Exception:
            wallet_sol = None
    results.append(check_risk_sizing(wallet_sol))
    results.append(bot_res)

    return results


def render_text(results: list[Result], want_live: bool) -> str:
    glyph = {PASS: "[+]", WARN: "[!]", FAIL: "[X]"}
    lines = [
        f"\npump_bot preflight - mode={'LIVE' if want_live else 'PAPER'}\n"
        + "-" * 60,
    ]
    for r in results:
        lines.append(f"  {glyph[r.status]} {r.status:<4}  {r.name:<22} {r.detail}")
    lines.append("-" * 60)
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    summary = " ".join(f"{by_status.get(s, 0)} {s}" for s in (PASS, WARN, FAIL))
    lines.append(f"  summary: {summary}")
    if any(r.is_blocker for r in results):
        lines.append("\nBLOCKERS PRESENT — do not proceed to live trading.")
    elif any(r.status == WARN for r in results):
        lines.append("\nWARNINGS — review above before going live.")
    else:
        lines.append("\nAll green. Cleared for takeoff.")
    return "\n".join(lines)


def main() -> int:
    # Force UTF-8 stdout — Windows default cp1252 chokes on the
    # em-dash / ellipsis / >= chars used in human-readable detail strings.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--paper", action="store_true",
                    help="Validate against paper-mode expectations (relaxed)")
    ap.add_argument("--min-balance", type=float, default=0.05,
                    help="Minimum acceptable wallet balance in SOL (default 0.05)")
    ap.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = ap.parse_args()

    want_live = not args.paper
    results = asyncio.run(run_checks(want_live, args.min_balance))

    if args.json:
        print(json.dumps(
            {"mode": "live" if want_live else "paper",
             "results": [{"name": r.name, "status": r.status, "detail": r.detail}
                         for r in results]},
            indent=2))
    else:
        print(render_text(results, want_live))

    return 1 if any(r.is_blocker for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
