"""
tools/graduation_sniper.py

PAPER graduation sniper for pump.fun — the strategy pivot (2026-06-06).
Tracking layer rewritten 2026-07-04 after discovering PumpPortal gated
`subscribeTokenTrade` behind a paid API key (the free tier silently rejects
it — the first 3-hour run watched 16 graduations happen with zero entries
because no trade events ever arrived).

Data sources — 100% free, all VERIFIED working 2026-07-04:
  - Discovery: frontend-api-v3.pump.fun/coins (recent-trade sort, local
    hot-zone filter) every DISCOVERY_POLL_S
  - Tracking:  frontend-api-v3.pump.fun/coins/{mint} polled every
    CURVE_POLL_S per tracked mint — live real_sol/virtual reserves
  - Exits:     PumpPortal wss subscribeMigration (still free) + the
    poll-based real_sol >= EXIT_REAL_SOL trigger

Thesis (arXiv 2602.14860, n=655,770 tokens): above ~80 real SOL raised with
positive velocity, graduation at ~85 SOL is mechanically near-certain. Buy
the last stretch, sell on-curve just before migration.

Entry gates (all must pass):
  - real SOL raised in [entry_real_sol, ENTRY_MAX_REAL_SOL)
  - 5-min curve velocity >= velocity_floor (real demand)
  - climb quality: >= DIVERSITY_MIN_INTERVALS distinct poll intervals with
    buys over the last DIVERSITY_WINDOW_SOL of raise AND no single interval
    > BUNDLE_MAX_SHARE of it (bundle pushes arrive as one or two spikes;
    organic demand arrives as many small steps — proxy for buyer diversity,
    which the free APIs no longer expose per-trade)
  - >= MIN_TRACK_SECONDS of observed history

Exits: curve-sell at EXIT_REAL_SOL (primary, +3.8%/win verified); migration
event (fallback); stall-stop; timeout.

Fills are HONEST: constant-product math including our own price impact and
pump.fun's 1% fee each way.

Brain: tools/edge_brain.py learns from every close — vetoes losing feature
buckets, blocks repeat-bad creators, never re-enters bad mints, and applies
bounded threshold re-tuning (EDGE_BRAIN_AUTOTUNE=0 to freeze).

Kill switch: create logs/KILL_GRADUATION.

Run: python -m tools.graduation_sniper
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import websockets

from tools.edge_brain import EdgeBrain

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_LOG = os.path.join(ROOT, "logs", "graduation_trades.jsonl")
STATE_FILE = os.path.join(ROOT, "logs", "graduation_state.json")
KILL_FILE = os.path.join(ROOT, "logs", "KILL_GRADUATION")
LIVE_SNAPSHOT = os.path.join(ROOT, "logs", "graduation_live.json")

# --- Curve constants (pump.fun, verified 2026) ------------------------------
VIRTUAL_SOL_INIT = 30.0
GRADUATION_REAL_SOL = 85.0
FEE_PCT = 1.0

# --- Entry gates --------------------------------------------------------------
ENTRY_REAL_SOL = 80.0            # brain-tunable within [80.0, 82.5]
ENTRY_MAX_REAL_SOL = 84.5
VELOCITY_FLOOR_SOL = 1.5         # brain-tunable within [1.0, 3.0]
VELOCITY_WINDOW_S = 300.0
DIVERSITY_WINDOW_SOL = 3.0
DIVERSITY_MIN_INTERVALS = 5      # distinct buy-intervals in the window
BUNDLE_MAX_SHARE = 0.50          # max single-interval share of the window
MIN_TRACK_SECONDS = 60.0

# --- Position management --------------------------------------------------------
SIZE_SOL = 0.25
MAX_CONCURRENT = 2
# 2026-07-16: widened 1.5 -> 3.0 after the first 20-trade readout: 9 of 13
# stall-stops later graduated — the 1.5 stop was whipsawed by routine
# late-curve profit-taking dips on tokens that then completed. The 4 true
# failures dumped hard within seconds, so the wider stop costs little there.
STALL_STOP_SOL = 3.0
TIMEOUT_MIN = 15.0
# PRIMARY exit: curve-sell just before graduation. +3.82%/win vs -6.94%/stall
# verified in the 2026-06-06 unit test; EV positive above ~65% completion.
EXIT_REAL_SOL = 84.5
MIGRATION_HAIRCUT_PCT = 2.0      # fallback: missed 84.5 and it migrated
STALL_HAIRCUT_PCT = 2.0

# --- Ops --------------------------------------------------------------------------
DISCOVERY_POLL_S = 20.0
DISCOVERY_PAGES = 3
HOT_ZONE_MIN_SOL = 65.0
MAX_TRACKED = 12                 # per-mint polling — keep the fan-in modest
CURVE_POLL_S = 3.0               # per-mint curve state refresh
STALE_DROP_S = 1800.0
MANAGE_TICK_S = 5.0
SEED_SOL = 5.0                   # operator-set 2026-07-04: clean 5 SOL book

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
COINS_URL = ("https://frontend-api-v3.pump.fun/coins"
             "?offset={offset}&limit=50&sort=last_trade_timestamp"
             "&order=DESC&includeNsfw=true")
COIN_URL = "https://frontend-api-v3.pump.fun/coins/{mint}"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Accept": "application/json"}


# =============================================================================
# Curve math — honest fills including our own impact
# =============================================================================
def buy_on_curve(v_sol: float, v_tok: float, spend_sol: float) -> float:
    x_net = spend_sol * (1.0 - FEE_PCT / 100.0)
    k = v_sol * v_tok
    return v_tok - k / (v_sol + x_net)


def sell_on_curve(v_sol: float, v_tok: float, tokens: float) -> float:
    k = v_sol * v_tok
    sol_out = v_sol - k / (v_tok + tokens)
    return sol_out * (1.0 - FEE_PCT / 100.0)


# =============================================================================
# Per-mint tracker — poll-driven curve history
# =============================================================================
class Tracker:
    def __init__(self, mint: str, symbol: str, creator: str,
                 v_sol: float, v_tok: float):
        self.mint = mint
        self.symbol = symbol
        self.creator = creator
        self.v_sol = v_sol
        self.v_tok = v_tok
        self.first_seen = time.time()
        self.last_change_ts = time.time()
        # (ts, sol_delta) per poll interval where the curve moved
        self.intervals: deque = deque(maxlen=400)
        self.rejected_reason: str | None = None

    @property
    def real_sol(self) -> float:
        return self.v_sol - VIRTUAL_SOL_INIT

    def on_poll(self, v_sol: float, v_tok: float):
        delta = v_sol - self.v_sol
        self.v_sol = v_sol
        self.v_tok = v_tok
        if abs(delta) > 1e-9:
            self.intervals.append((time.time(), delta))
            self.last_change_ts = time.time()

    def velocity(self, window_s: float) -> float:
        cutoff = time.time() - window_s
        return sum(d for ts, d in self.intervals if ts >= cutoff)

    def climb_quality(self) -> tuple[int, float]:
        """(buy-interval count, max single-interval share) over the last
        DIVERSITY_WINDOW_SOL of positive curve movement, newest-first.
        Organic demand = many small steps; a bundle = one or two spikes."""
        total, count, biggest = 0.0, 0, 0.0
        for ts, d in reversed(self.intervals):
            if d <= 0:
                continue
            take = min(d, DIVERSITY_WINDOW_SOL - total)
            total += take
            count += 1
            biggest = max(biggest, take)
            if total >= DIVERSITY_WINDOW_SOL:
                break
        if total <= 0:
            return 0, 1.0
        return count, biggest / total


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"positions": {},
            "account": {"seed_sol": SEED_SOL, "realized_sol": 0.0}}


def _save_state(s: dict):
    tmp = STATE_FILE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE_FILE)


def _log(rec: dict):
    rec.setdefault("ts", time.time())
    rec.setdefault("strategy", "graduation_sniper")
    with open(TRADES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# =============================================================================
# Sniper
# =============================================================================
class GraduationSniper:
    def __init__(self):
        self.trackers: dict[str, Tracker] = {}
        self.state = _load_state()
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.brain = EdgeBrain()
        self.autotune = os.environ.get("EDGE_BRAIN_AUTOTUNE", "1") != "0"
        self.entry_real_sol = ENTRY_REAL_SOL
        self.velocity_floor = VELOCITY_FLOOR_SOL

    # ---------------- HTTP helpers (run in executor) ----------------
    def _get_json(self, url: str):
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    # ---------------- discovery ----------------
    async def discovery_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                for page_i in range(DISCOVERY_PAGES):
                    try:
                        page = await loop.run_in_executor(
                            self.pool, self._get_json,
                            COINS_URL.format(offset=page_i * 50))
                    except Exception:
                        continue
                    for c in page:
                        mint = c.get("mint")
                        if (not mint or mint in self.trackers
                                or c.get("complete")
                                or c.get("pump_swap_pool")):
                            continue
                        rs = (c.get("real_sol_reserves") or 0) / 1e9
                        if not (HOT_ZONE_MIN_SOL <= rs < GRADUATION_REAL_SOL):
                            continue
                        if len(self.trackers) >= MAX_TRACKED:
                            # keep slots for the hottest — drop coldest idle
                            coldest = min(
                                (t for t in self.trackers.values()
                                 if t.mint not in self.state["positions"]),
                                key=lambda t: t.real_sol, default=None)
                            if coldest and coldest.real_sol < rs:
                                del self.trackers[coldest.mint]
                            else:
                                continue
                        self.trackers[mint] = Tracker(
                            mint, c.get("symbol", "?"), c.get("creator", ""),
                            v_sol=(c.get("virtual_sol_reserves") or 0) / 1e9,
                            v_tok=(c.get("virtual_token_reserves") or 0) / 1e6)
                        print(f"[GRAD] tracking {c.get('symbol','?')} "
                              f"({mint[:8]}) real_sol={rs:.1f}", flush=True)
            except Exception as e:
                print(f"[GRAD] discovery error {type(e).__name__}: "
                      f"{str(e)[:100]}", flush=True)
            await asyncio.sleep(DISCOVERY_POLL_S)

    # ---------------- curve polling (replaces the gated trade stream) --------
    async def curve_poll_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            mints = list(self.trackers.keys())
            if mints:
                async def poll_one(mint: str):
                    t = self.trackers.get(mint)
                    if not t:
                        return
                    try:
                        c = await loop.run_in_executor(
                            self.pool, self._get_json,
                            COIN_URL.format(mint=mint))
                    except Exception:
                        return
                    if not isinstance(c, dict):
                        return
                    if c.get("complete") or c.get("pump_swap_pool"):
                        self._on_migration(mint)
                        return
                    v_sol = (c.get("virtual_sol_reserves") or 0) / 1e9
                    v_tok = (c.get("virtual_token_reserves") or 0) / 1e6
                    if v_sol <= 0 or v_tok <= 0:
                        return
                    t.on_poll(v_sol, v_tok)
                    if (mint in self.state["positions"]
                            and t.real_sol >= EXIT_REAL_SOL):
                        self._close(mint, "pre_grad_exit", 0.0)
                    else:
                        self._maybe_enter(t)

                await asyncio.gather(*(poll_one(m) for m in mints))
            await asyncio.sleep(CURVE_POLL_S)

    # ---------------- migration websocket (free tier still allows this) ------
    async def migration_ws_loop(self):
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(PUMPPORTAL_WS, ping_interval=20,
                                              ping_timeout=15) as ws:
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    print("[GRAD] migration WS connected", flush=True)
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        mint = msg.get("mint")
                        if mint and (mint in self.trackers
                                     or mint in self.state["positions"]):
                            self._on_migration(mint)
            except Exception as e:
                print(f"[GRAD] WS drop {type(e).__name__}: {str(e)[:80]} "
                      f"— reconnect in {backoff:.0f}s", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # ---------------- strategy ----------------
    def _maybe_enter(self, t: Tracker):
        if os.path.exists(KILL_FILE):
            return
        if t.mint in self.state["positions"]:
            return
        if len(self.state["positions"]) >= MAX_CONCURRENT:
            return
        rs = t.real_sol
        if not (self.entry_real_sol <= rs < ENTRY_MAX_REAL_SOL):
            return
        if time.time() - t.first_seen < MIN_TRACK_SECONDS:
            return
        vel = t.velocity(VELOCITY_WINDOW_S)
        if vel < self.velocity_floor:
            if t.rejected_reason != "velocity":
                t.rejected_reason = "velocity"
                _log({"event": "skip", "mint": t.mint, "symbol": t.symbol,
                      "reason": f"velocity {vel:.2f} < {self.velocity_floor}",
                      "real_sol": round(rs, 2)})
            return
        steps, max_share = t.climb_quality()
        if steps < DIVERSITY_MIN_INTERVALS or max_share > BUNDLE_MAX_SHARE:
            if t.rejected_reason != "bundle":
                t.rejected_reason = "bundle"
                _log({"event": "skip", "mint": t.mint, "symbol": t.symbol,
                      "reason": f"climb steps={steps} max_share={max_share:.2f}",
                      "real_sol": round(rs, 2)})
                print(f"[GRAD] SKIP {t.symbol} — bundle-ish climb "
                      f"(steps={steps}, max_share={max_share:.0%})", flush=True)
            return
        ok, why = self.brain.allows(mint=t.mint, creator=t.creator,
                                    entry_real_sol=rs, velocity=vel,
                                    buyers=steps)
        if not ok:
            if t.rejected_reason != why:
                t.rejected_reason = why
                _log({"event": "skip", "mint": t.mint, "symbol": t.symbol,
                      "reason": f"brain:{why}", "real_sol": round(rs, 2)})
                print(f"[GRAD] SKIP {t.symbol} — brain veto: {why}", flush=True)
            return

        # ---- HONEST PAPER FILL ----
        tokens = buy_on_curve(t.v_sol, t.v_tok, SIZE_SOL)
        self.state["positions"][t.mint] = {
            "symbol": t.symbol, "creator": t.creator, "entry_ts": time.time(),
            "entry_v_sol": t.v_sol, "entry_real_sol": rs,
            "size_sol": SIZE_SOL, "tokens": tokens,
            "entry_velocity": round(vel, 3),
            "entry_buyers": steps, "entry_max_share": round(max_share, 3),
        }
        _save_state(self.state)
        _log({"event": "open", "mint": t.mint, "symbol": t.symbol,
              "size_sol": SIZE_SOL, "tokens": tokens,
              "real_sol": round(rs, 2), "velocity_5m": round(vel, 2),
              "buyers": steps, "max_share": round(max_share, 2),
              "fill": "honest_curve"})
        print(f"[GRAD] OPEN {t.symbol} ({t.mint[:8]}) at real_sol={rs:.2f} "
              f"vel={vel:.1f}/5m steps={steps} size={SIZE_SOL} SOL", flush=True)

    def _close(self, mint: str, reason: str, haircut_pct: float):
        pos = self.state["positions"].pop(mint, None)
        if not pos:
            return
        t = self.trackers.get(mint)
        v_sol = t.v_sol if t else pos["entry_v_sol"]
        v_tok = t.v_tok if t else 0
        if v_tok > 0:
            gross = sell_on_curve(v_sol, v_tok, pos["tokens"])
        else:
            gross = pos["size_sol"]
        net = gross * (1.0 - haircut_pct / 100.0)
        pnl = max(-pos["size_sol"], net - pos["size_sol"])
        net_pct = pnl / pos["size_sol"] * 100.0
        self.state["account"]["realized_sol"] = round(
            self.state["account"].get("realized_sol", 0.0) + pnl, 6)
        _save_state(self.state)
        hold = time.time() - pos["entry_ts"]
        _log({"event": "close", "mint": mint, "symbol": pos["symbol"],
              "pnl_sol": round(pnl, 5), "net_pct": round(net_pct, 2),
              "exit_reason": reason, "hold_s": round(hold, 0),
              "exit_real_sol": round(v_sol - VIRTUAL_SOL_INIT, 2),
              "haircut_pct": haircut_pct})
        bal = SEED_SOL + self.state["account"]["realized_sol"]
        print(f"[GRAD] CLOSE {pos['symbol']} {reason} pnl={pnl:+.4f} SOL "
              f"({net_pct:+.1f}%) hold={hold:.0f}s | balance={bal:.3f} SOL",
              flush=True)
        try:
            self.brain.record(
                mint=mint, creator=pos.get("creator", ""),
                entry_real_sol=pos.get("entry_real_sol", 0.0),
                velocity=pos.get("entry_velocity", 0.0),
                buyers=pos.get("entry_buyers", 0),
                pnl_sol=pnl, exit_reason=reason)
            if self.autotune:
                sugg = self.brain.suggest_params()
                if "entry_real_sol" in sugg:
                    self.entry_real_sol = sugg["entry_real_sol"]["value"]
                if "velocity_floor" in sugg:
                    self.velocity_floor = sugg["velocity_floor"]["value"]
                if sugg:
                    print(f"[GRAD] brain re-tune: entry>={self.entry_real_sol} "
                          f"vel>={self.velocity_floor}", flush=True)
        except Exception as e:
            print(f"[GRAD] brain error: {type(e).__name__}: {str(e)[:80]}",
                  flush=True)

    def _on_migration(self, mint: str):
        if mint in self.state["positions"]:
            self._close(mint, "migration", MIGRATION_HAIRCUT_PCT)
        t = self.trackers.pop(mint, None)
        if t:
            print(f"[GRAD] {t.symbol} graduated — untracked", flush=True)

    # ---------------- management ----------------
    async def manage_loop(self):
        last_heartbeat = 0.0
        while True:
            now = time.time()
            if os.path.exists(KILL_FILE):
                print("[GRAD] KILL SWITCH — closing all paper positions",
                      flush=True)
                for mint in list(self.state["positions"].keys()):
                    self._close(mint, "kill_switch", STALL_HAIRCUT_PCT)
                _save_state(self.state)
                raise SystemExit(0)
            for mint in list(self.state["positions"].keys()):
                pos = self.state["positions"][mint]
                t = self.trackers.get(mint)
                if t and t.v_sol <= pos["entry_v_sol"] - STALL_STOP_SOL:
                    self._close(mint, "stall_stop", STALL_HAIRCUT_PCT)
                elif now - pos["entry_ts"] > TIMEOUT_MIN * 60:
                    self._close(mint, "timeout", STALL_HAIRCUT_PCT)
            for mint in list(self.trackers.keys()):
                if mint in self.state["positions"]:
                    continue
                t = self.trackers[mint]
                if (now - t.last_change_ts > STALE_DROP_S
                        or t.real_sol < HOT_ZONE_MIN_SOL - 10):
                    del self.trackers[mint]
            # Live snapshot for the dashboard's graduation radar
            try:
                snap = {
                    "ts": now,
                    "balance_sol": round(SEED_SOL + self.state["account"]
                                         .get("realized_sol", 0.0), 4),
                    "entry_real_sol": self.entry_real_sol,
                    "exit_real_sol": EXIT_REAL_SOL,
                    "open_positions": len(self.state["positions"]),
                    "tokens": sorted([{
                        "symbol": t.symbol, "mint": t.mint,
                        "real_sol": round(t.real_sol, 2),
                        "velocity_5m": round(t.velocity(VELOCITY_WINDOW_S), 2),
                        "steps": t.climb_quality()[0],
                        "max_share": round(t.climb_quality()[1], 2),
                        "in_position": t.mint in self.state["positions"],
                        "flagged": t.rejected_reason or "",
                        "age_s": round(now - t.first_seen, 0),
                    } for t in self.trackers.values()],
                        key=lambda x: -x["real_sol"]),
                }
                tmp = LIVE_SNAPSHOT + ".tmp"
                json.dump(snap, open(tmp, "w"))
                os.replace(tmp, LIVE_SNAPSHOT)
            except Exception:
                pass
            if now - last_heartbeat > 60:
                bal = SEED_SOL + self.state["account"].get("realized_sol", 0.0)
                hot = sorted((t for t in self.trackers.values()),
                             key=lambda x: -x.real_sol)[:3]
                hot_s = " ".join(f"{t.symbol}@{t.real_sol:.1f}" for t in hot)
                print(f"[GRAD] heartbeat tracked={len(self.trackers)} "
                      f"open={len(self.state['positions'])} "
                      f"balance={bal:.3f} SOL | hottest: {hot_s} "
                      f"@ {time.strftime('%H:%M:%S')}", flush=True)
                last_heartbeat = now
            await asyncio.sleep(MANAGE_TICK_S)

    async def run(self):
        bal = SEED_SOL + self.state["account"].get("realized_sol", 0.0)
        print(f"Graduation sniper v2 (poll-tracking) | PAPER | "
              f"seed {SEED_SOL:.3f} SOL (balance {bal:.3f}) | "
              f"entry>={self.entry_real_sol} real SOL | "
              f"vel>={self.velocity_floor}/5m | steps>={DIVERSITY_MIN_INTERVALS} | "
              f"size {SIZE_SOL} SOL x{MAX_CONCURRENT} | kill: {KILL_FILE}",
              flush=True)
        await asyncio.gather(
            self.discovery_loop(),
            self.curve_poll_loop(),
            self.migration_ws_loop(),
            self.manage_loop(),
        )


def main():
    if os.path.exists(KILL_FILE):
        raise SystemExit(f"kill file present ({KILL_FILE}) — remove to start")
    asyncio.run(GraduationSniper().run())


if __name__ == "__main__":
    main()
