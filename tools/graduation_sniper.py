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

Thesis, REVISED 2026-07-22 after the 54-trade readout. The original thesis
(arXiv 2602.14860, n=655,770 tokens) was that above ~80 real SOL raised with
positive velocity, graduation at ~85 SOL is mechanically near-certain — so buy
the last stretch and ride it through migration. The completion half replicated
(88% of the mints we entered graduated) but the trade half did NOT: holding to
migration won 3 of 16 and lost money, while scalping out before graduation won
15 of 19 and made money. Graduation is not the payday; the last stretch of the
curve is. This is now a pure on-curve scalp — enter with a mandatory runway to
the exit, sell into the climb, and never deliberately hold to migration.

Entry gates (all must pass):
  - real SOL raised in [entry_real_sol, ENTRY_MAX_REAL_SOL)
  - 5-min curve velocity >= velocity_floor (real demand)
  - climb quality: >= DIVERSITY_MIN_INTERVALS distinct poll intervals with
    buys over the last DIVERSITY_WINDOW_SOL of raise AND no single interval
    > BUNDLE_MAX_SHARE of it (bundle pushes arrive as one or two spikes;
    organic demand arrives as many small steps — proxy for buyer diversity,
    which the free APIs no longer expose per-trade)
  - >= MIN_TRACK_SECONDS of observed history

Exits: curve-sell at EXIT_REAL_SOL (primary, and the only intended one — it
sits MIN_RUNWAY_SOL clear of graduation so the scalp wins the race); migration
event (a MISS, taken at a haircut, not a plan); disaster-stop; timeout. The
stall-stop is disabled — see the scalp-band block for the readout that killed
it.

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
from tools.grad_alerts import GradAlerts
from tools.grad_tail import TailHolder

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_LOG = os.path.join(ROOT, "logs", "graduation_trades.jsonl")
STATE_FILE = os.path.join(ROOT, "logs", "graduation_state.json")
KILL_FILE = os.path.join(ROOT, "logs", "KILL_GRADUATION")
LIVE_SNAPSHOT = os.path.join(ROOT, "logs", "graduation_live.json")

# --- Curve constants (pump.fun, verified 2026) ------------------------------
VIRTUAL_SOL_INIT = 30.0
GRADUATION_REAL_SOL = 85.0
FEE_PCT = 1.0

# --- Real-life friction (2026-07-17, operator directive: replicate every
# --- source of live friction — the copy-trading audit lesson) ----------------
# Orders no longer fill at the observed curve state. A decision at poll T
# fills at poll T+1 (~CURVE_POLL_S later) — models send->land latency on a
# curve that moved while the tx was in flight. Consequences this correctly
# reproduces: entries chase rising curves, the 84.5 exit can LOSE the race
# to migration (falls back to the migration haircut), and 6-second wins
# stop being free money.
TX_FEE_SOL = 0.0007              # base + priority fee per SUBMITTED tx,
                                 # charged on reverts too (p50-p75 guess for
                                 # hot pump.fun mints; tune from live data)
ENTRY_SLIPPAGE_BOUND_PCT = 2.0   # buy reverts if token price moved > this
                                 # against us between decision and landing

# --- Entry gates --------------------------------------------------------------
# 2026-07-21 REWIRE, validated on our own shadow dataset (n=298) after the
# research sweep: arXiv 2602.14860's "fewer trades to a given SOL level =
# higher graduation odds" replicates here — vel>=3 completes at 81-86%
# (with hour filter) while our old many-small-buys "organic" gate selected
# a 70% pool (wash-traded bait mimics organic climbs; real momentum is few
# big buys). Old gate: steps>=5, max_share<=0.5, vel>=1.5.
ENTRY_REAL_SOL = 80.0            # brain-tunable within [80.0, ENTRY_MAX_REAL_SOL]
# ENTRY_MAX_REAL_SOL is DERIVED from the exit — see the scalp-band block below.
# It is never a free parameter: an entry with less than MIN_RUNWAY_SOL of curve
# between it and the exit cannot clear the round-trip fee, so it is not a trade.
VELOCITY_FLOOR_SOL = 3.0         # brain-tunable within [2.5, 8.0]
VELOCITY_WINDOW_S = 300.0
DIVERSITY_WINDOW_SOL = 3.0
ENTRY_MAX_SHARE_CAP = 0.90       # only block near-total single-buyer pushes
ENTRY_MAX_AGE_H = 24.0           # zombie gate: median successful token fills
                                 # the curve in minutes (arXiv 2602.14860);
                                 # month-old tokens limping into the zone are
                                 # a different (bad) population
# Creator-history gate (2026-07-21): the paper found creator identity
# predictive late-curve, and the ecosystem's serial-rugger signature is
# "many launches, zero graduations". Verified free endpoint: /coins?creator=
CREATOR_SERIAL_MIN_COINS = 5     # this many prior launches with zero grads
                                 # -> skip (serial launcher, no successes)
# Old organic-climb thresholds — still used by the TAIL-HOLD cohort filter
# (post-migration bounce needs holder breadth, a different thesis; its own
# small sample is positive, so it keeps the strict definition)
DIVERSITY_MIN_INTERVALS = 5      # distinct buy-intervals in the window
BUNDLE_MAX_SHARE = 0.50          # max single-interval share of the window
MIN_TRACK_SECONDS = 60.0

# --- Position management --------------------------------------------------------
SIZE_SOL = 0.25
MAX_CONCURRENT = 2

# =============================================================================
# 2026-07-22 SCALP-ONLY REWIRE (operator directive after the 54-trade readout)
# =============================================================================
# The book was -0.7678 SOL over 54 trades at a 33% win rate. The diagnosis is
# NOT entry quality — of the entered mints with a known outcome, 30/34 (88%)
# went on to graduate, well above the 71% shadow-population rate. The entry
# gates work. Every SOL of the loss came from the exit/stop structure:
#
#   exit reason      n   wins   net SOL
#   pre_grad_exit   19   15     +0.108     <- the only thing that made money
#   migration       16    3     -0.088     <- the "thesis played out" branch
#   stall_stop      15    0     -0.343     <- pure loss, zero wins
#   disaster_stop    5    0     -0.457     <- 1 trade (Jimothy) was -0.234
#
# Three structural defects, all fixed in this block:
#
# 1. NO RUNWAY GATE — the big one. ENTRY_MAX_REAL_SOL was 84.5, identical to
#    EXIT_REAL_SOL, so nothing stopped an entry from being opened with no room
#    left to profit. pump.fun charges 1% each way, so a SIZE_SOL scalp needs
#    ~1.7 SOL of curve advance just to break even. 11 of 55 entries (20%) were
#    opened INSIDE that dead band and were mathematically incapable of profit
#    at the moment of entry — FOXGAR opened at 84.31 with 0.19 SOL of runway.
#    Entry max is now DERIVED from the exit, so this cannot regress.
#
# 2. THE STALL STOP WAS PURE LOSS. 15 fires, 0 wins, -0.343 SOL. It sold
#    routine late-curve dips at ~-2% on tokens that overwhelmingly went on to
#    graduate (the whipsaw monitor caught 2 of 7 tagged stops graduating after
#    we bailed, and PLEROMA survived a 13.45 SOL dump to migrate anyway).
#    Disabled. The disaster stop caught all 4 genuine deaths on its own; that
#    is where real risk control lives.
#
# 3. HOLDING TO MIGRATION IS THE LOSING BRANCH. The exit sat 0.5 SOL below
#    graduation, so 16 of the 35 non-stopped trades lost the race to migration
#    and took the haircut instead of the scalp. The exit now sits
#    MIN_RUNWAY_SOL clear of graduation — the scalp wins the race by design.
#
# Modelled EV per trade at these settings (p(reach exit)=0.88, death rate 7%,
# death loss capped at the observed ex-tail -22%): approximately +1.9%. That is
# a thin edge, not a fat one, and it is a MODEL — the paper book is what
# decides. See tests/test_graduation_scalp.py for the invariants.
STALL_STOP_ENABLED = False       # defect 2 — see above. Constants kept so the
STALL_STOP_SOL = 3.0             # whipsaw monitor and the report stay readable
STALL_CONFIRM_S = 300.0          # and so re-enabling is a one-line change.
DISASTER_STOP_SOL = 5.0          # tightened 8.0 -> 5.0: the tail is what makes
                                 # this strategy negative-EV. Jimothy alone
                                 # (-93.5%) cost 18 winning scalps. Whipsaw
                                 # cost is accepted in exchange for the cap.
TIMEOUT_MIN = 8.0                # a scalp that has not covered MIN_RUNWAY_SOL
                                 # at vel>=3 SOL/5min is not working; 15 min
                                 # was long enough to sit through a full cycle.

# --- The scalp band ---------------------------------------------------------
# Round-trip cost is 2% of notional (1% pump.fun fee each way) plus 2 tx fees.
# Measured against the curve, break-even needs ~1.70 SOL of advance and it is
# near-constant across the band (1.68 SOL at an 80.0 entry, 1.71 at 82.5) — the
# fee dominates, so a "runway" gate is the right shape of gate.
#
# MIN_RUNWAY_SOL is set at 2.5, not at break-even, because a break-even-plus-
# epsilon trade is not worth taking: at a 7% death rate and the observed
# ex-tail death loss of -22%, a scalp must net ~+1.4% just to carry its share
# of the deaths. 2.0 was tried first and rejected — it admitted entries netting
# +0.54%, which is EV-negative against that death rate. Net at the worst
# allowed entry is now +1.44%, rising to +3.31% at the bottom of the band.
#
# Cost of the tighter gate: the band narrows to [80.0, 81.0), which held 35% of
# historical entries. Acceptable — 45% of all decision-zone observations occur
# inside it, because tokens are first SEEN entering the zone near 80.
EXIT_REAL_SOL = 83.5             # PRIMARY exit: curve-sell, never hold to grad
MIN_RUNWAY_SOL = 2.5             # required curve between entry and exit
ENTRY_MAX_REAL_SOL = EXIT_REAL_SOL - MIN_RUNWAY_SOL      # = 81.0, derived
MIGRATION_HAIRCUT_PCT = 2.0      # fallback: lost the race and it migrated
STALL_HAIRCUT_PCT = 2.0

# The shadow dataset's decision zone is deliberately NOT narrowed with the
# entry band — the 411-example completion dataset is only comparable across
# re-tunes if the zone definition stays fixed. Shadow snapshots keep sampling
# the original [80.0, 84.5) zone even though we now only trade [80.0, 81.5).
SHADOW_ZONE_MAX_REAL_SOL = 84.5

# Stamped onto every open/close so the next readout can separate scalp-era
# trades from the 54 old-structure ones already in the log. Without this the
# before/after comparison the rewire is a bet on cannot be made cleanly —
# stall_stop and no-runway entries simply stop existing, which would silently
# flatter any pooled statistic. Bump this on any future structural change.
STRATEGY_ERA = "scalp_v3"

# --- Ops --------------------------------------------------------------------------
# --- Hour filter (2026-07-21) -----------------------------------------------
# Driven by our own shadow-completion dataset: completion rate by UTC hour
# ranges 50%-100% (n=264 at ship time) while post-friction economics need
# ~81% wins. Entries are blocked in hours that complete below the floor;
# shadow snapshots keep collecting in blocked hours, so an hour that
# improves un-blocks itself on the next refresh.
HOUR_FILTER_MIN_N = 8            # shadow outcomes needed before an hour is judged
HOUR_FILTER_FLOOR = 0.72         # block entries when the hour completes below this
HOUR_STATS_REFRESH_S = 3600.0

DISCOVERY_POLL_S = 20.0
DISCOVERY_PAGES = 3
HOT_ZONE_MIN_SOL = 65.0
MAX_TRACKED = 12                 # per-mint polling — keep the fan-in modest
CURVE_POLL_S = 3.0               # per-mint curve state refresh
STALE_DROP_S = 1800.0
MANAGE_TICK_S = 5.0
SELL_ORPHAN_S = 30.0             # a submitted sell should fill at the next
                                 # curve poll (~CURVE_POLL_S). If it is still
                                 # pending after this long the mint's poll is
                                 # failing — force the close from manage_loop
                                 # against last-known curve state rather than
                                 # hold an unbounded position. See the
                                 # orphaned-sell backstop in manage_loop.
SEED_SOL = 5.0                   # operator-set 2026-07-04: clean 5 SOL book

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
COINS_URL = ("https://frontend-api-v3.pump.fun/coins"
             "?offset={offset}&limit=50&sort=last_trade_timestamp"
             "&order=DESC&includeNsfw=true")
COIN_URL = "https://frontend-api-v3.pump.fun/coins/{mint}"
CREATOR_URL = ("https://frontend-api-v3.pump.fun/coins"
               "?offset=0&limit=50&creator={creator}")
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
                 v_sol: float, v_tok: float, coin: dict | None = None):
        self.mint = mint
        self.symbol = symbol
        self.creator = creator
        # Free API signals (recorded into the shadow dataset; only age is
        # gated on so far — the rest accumulate until our data judges them)
        coin = coin or {}
        self.created_ts = (coin.get("created_timestamp") or 0) / 1000.0
        self.koth_ts = (coin.get("king_of_the_hill_timestamp") or 0) / 1000.0
        self.reply_count = coin.get("reply_count") or 0
        self.is_live = bool(coin.get("is_currently_live"))
        # Creator history — loaded async after tracking starts (None=unknown)
        self.creator_coins: int | None = None
        self.creator_grads: int | None = None
        # Zone dump/recovery telemetry (92% of tokens dump pre-grad per the
        # research; recording whether/how deep to learn post-dump timing)
        self.peak_v_sol = v_sol
        self.max_dump_sol = 0.0
        self.v_sol = v_sol
        self.v_tok = v_tok
        self.first_seen = time.time()
        self.last_change_ts = time.time()
        # (ts, sol_delta) per poll interval where the curve moved
        self.intervals: deque = deque(maxlen=400)
        self.rejected_reason: str | None = None
        # Shadow-completion dataset: features frozen the first time this
        # token qualifies for the decision zone (see _maybe_shadow_snap)
        self.shadow_snap: dict | None = None

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
        if v_sol > self.peak_v_sol:
            self.peak_v_sol = v_sol
        else:
            self.max_dump_sol = max(self.max_dump_sol,
                                    self.peak_v_sol - v_sol)

    @property
    def dump_recovered(self) -> bool:
        """A >=2 SOL flush happened and price is back near the peak —
        the post-dump entry signature from the research sweep."""
        return (self.max_dump_sol >= 2.0
                and self.v_sol >= self.peak_v_sol - 0.5)

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
            "account": {"seed_sol": SEED_SOL, "realized_sol": 0.0},
            "recent_stops": {}}


def _save_state(s: dict):
    tmp = STATE_FILE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE_FILE)


def _log(rec: dict):
    rec.setdefault("ts", time.time())
    rec.setdefault("strategy", "graduation_sniper")
    if rec.get("event") in ("open", "close", "skip", "entry_fail"):
        rec.setdefault("era", STRATEGY_ERA)
    with open(TRADES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _read_closes() -> list:
    """Every close record on disk. Only called when the daily digest fires —
    never on the manage tick."""
    out = []
    try:
        with open(TRADES_LOG, encoding="utf-8") as f:
            for line in f:
                if '"close"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("event") == "close":
                    out.append(r)
    except OSError:
        pass
    return out


# =============================================================================
# Sniper
# =============================================================================
class GraduationSniper:
    def __init__(self):
        self.trackers: dict[str, Tracker] = {}
        # In-flight orders: decided at one poll, filled at the next
        self.pending: dict[str, dict] = {}
        self.state = _load_state()
        self.state.setdefault("recent_stops", {})
        self.state["account"].setdefault("fees_sol", 0.0)
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.brain = EdgeBrain()
        self.state.setdefault("tail", {"realized_sol": 0.0})
        self.tail = TailHolder(log_fn=_log, state=self.state,
                               save_fn=lambda: _save_state(self.state),
                               pool=self.pool)
        self.autotune = os.environ.get("EDGE_BRAIN_AUTOTUNE", "1") != "0"
        self._hour_stats: dict[int, list] = {}
        self._hour_stats_ts = 0.0
        self._creator_cache: dict[str, tuple[int, int]] = {}
        self.entry_real_sol = ENTRY_REAL_SOL
        self.velocity_floor = VELOCITY_FLOOR_SOL
        self.alerts = GradAlerts()

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
                                or c.get("pump_swap_pool")
                                or c.get("is_banned")):
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
                            v_tok=(c.get("virtual_token_reserves") or 0) / 1e6,
                            coin=c)
                        asyncio.get_running_loop().create_task(
                            self._load_creator_history(self.trackers[mint]))
                        print(f"[GRAD] tracking {c.get('symbol','?')} "
                              f"({mint[:8]}) real_sol={rs:.1f}", flush=True)
                        self.alerts.note_discovery()   # dead-man: discovery ok
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
                    self.alerts.note_poll()     # dead-man: the feed answered
                    self._maybe_shadow_snap(t)
                    self._execute_pending(t)
                    if (mint in self.state["positions"]
                            and t.real_sol >= EXIT_REAL_SOL):
                        self._place_sell(mint, "pre_grad_exit", 0.0)
                    elif mint not in self.state["positions"]:
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
                                     or mint in self.state["positions"]
                                     or mint in self.state["recent_stops"]):
                            self._on_migration(mint)
            except Exception as e:
                print(f"[GRAD] WS drop {type(e).__name__}: {str(e)[:80]} "
                      f"— reconnect in {backoff:.0f}s", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # ---------------- shadow completion dataset ----------------
    # We only trade ~3 tokens/day but watch 50-100 through the hot zone.
    # Snapshot every token the FIRST time it would clear the basic entry
    # window (fixed 80 SOL, not the brain-tuned threshold, so the dataset
    # stays comparable across re-tunes) and later log whether it graduated.
    # This measures completion rate by hour/velocity/climb-quality ~30x
    # faster than trading does — the dataset the entry thresholds and the
    # tail-hold selection filter are tuned from.
    def _maybe_shadow_snap(self, t: Tracker):
        if t.shadow_snap is not None:
            return
        rs = t.real_sol
        if not (80.0 <= rs < SHADOW_ZONE_MAX_REAL_SOL):
            return
        if time.time() - t.first_seen < MIN_TRACK_SECONDS:
            return
        steps, max_share = t.climb_quality()
        now = time.time()
        t.shadow_snap = {
            "ts": now, "real_sol": round(rs, 2),
            "velocity_5m": round(t.velocity(VELOCITY_WINDOW_S), 2),
            "steps": steps, "max_share": round(max_share, 2),
            "hour_utc": time.gmtime().tm_hour,
            "age_min": round((now - t.created_ts) / 60.0, 1)
                       if t.created_ts else None,
            "koth_min": round((now - t.koth_ts) / 60.0, 1)
                        if t.koth_ts else None,
            "replies": t.reply_count, "live": t.is_live,
            "creator_coins": t.creator_coins,
            "creator_grads": t.creator_grads,
        }

    def _shadow_outcome(self, t: Tracker, outcome: str):
        if not t.shadow_snap:
            return
        _log({"event": "shadow_outcome", "mint": t.mint, "symbol": t.symbol,
              "outcome": outcome,
              "secs_to_outcome": round(time.time() - t.shadow_snap["ts"], 0),
              "max_dump_sol": round(t.max_dump_sol, 2),
              "dump_recovered": t.dump_recovered,
              "snap": t.shadow_snap})
        t.shadow_snap = None

    # ---------------- creator history ----------------
    async def _load_creator_history(self, t: Tracker):
        """(prior launches, prior graduations) for the token's creator via
        the free ?creator= endpoint. Cached per creator; None until loaded."""
        if not t.creator:
            return
        cached = self._creator_cache.get(t.creator)
        if cached:
            t.creator_coins, t.creator_grads = cached
            return
        try:
            coins = await asyncio.get_running_loop().run_in_executor(
                self.pool, self._get_json,
                CREATOR_URL.format(creator=t.creator))
        except Exception:
            return
        if not isinstance(coins, list):
            return
        prior = [c for c in coins if c.get("mint") != t.mint]
        grads = sum(1 for c in prior
                    if c.get("complete") or c.get("pump_swap_pool")
                    or c.get("raydium_pool"))
        t.creator_coins, t.creator_grads = len(prior), grads
        if len(self._creator_cache) > 500:
            self._creator_cache.clear()
        self._creator_cache[t.creator] = (len(prior), grads)

    # ---------------- hour filter ----------------
    def _hour_completion(self, hour: int) -> tuple[int, float]:
        """(n, completion rate) for a UTC hour from our shadow dataset,
        recomputed from the trades log at most once per hour."""
        now = time.time()
        if now - self._hour_stats_ts > HOUR_STATS_REFRESH_S:
            stats: dict[int, list] = {}
            try:
                with open(TRADES_LOG, encoding="utf-8") as f:
                    for line in f:
                        if '"shadow_outcome"' not in line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        h = (r.get("snap") or {}).get("hour_utc")
                        if h is None:
                            continue
                        d = stats.setdefault(int(h), [0, 0])
                        d[0] += 1
                        d[1] += int(r.get("outcome") == "graduated")
            except OSError:
                pass
            self._hour_stats = stats
            self._hour_stats_ts = now
        d = self._hour_stats.get(hour)
        if not d or d[0] == 0:
            return 0, 1.0
        return d[0], d[1] / d[0]

    def _shadow_rate_str(self) -> str:
        """Population completion rate across all hours, for the daily digest.
        Reuses the hour-stats cache — no extra file read."""
        self._hour_completion(0)          # ensure the cache is warm
        n = sum(d[0] for d in self._hour_stats.values())
        g = sum(d[1] for d in self._hour_stats.values())
        return f"{g}/{n} ({g / n:.0%})" if n else "no data"

    # ---------------- strategy ----------------
    def _maybe_enter(self, t: Tracker):
        if os.path.exists(KILL_FILE):
            return
        if t.mint in self.state["positions"] or t.mint in self.pending:
            return
        if len(self.state["positions"]) >= MAX_CONCURRENT:
            return
        rs = t.real_sol
        if rs < self.entry_real_sol:
            return          # still climbing into the zone — not a rejection
        # Past this point the token is in the zone and WILL be judged (entered
        # or skipped). That is the signal the dead-man watches: tokens tracked
        # but never judged means the pipeline stalled, which is invisible to a
        # process watchdog.
        self.alerts.note_decision()
        if rs >= ENTRY_MAX_REAL_SOL:
            # Defect 1: too far up the curve for the scalp to clear its own
            # round-trip fee. Logged (it used to return silently, which is why
            # 20% of entries were opened into a guaranteed loss unnoticed).
            if t.rejected_reason != "no_runway":
                t.rejected_reason = "no_runway"
                _log({"event": "skip", "mint": t.mint, "symbol": t.symbol,
                      "reason": f"no_runway {EXIT_REAL_SOL - rs:.2f}"
                                f"<{MIN_RUNWAY_SOL} SOL to exit",
                      "real_sol": round(rs, 2)})
            return
        if time.time() - t.first_seen < MIN_TRACK_SECONDS:
            return
        if t.created_ts and (time.time() - t.created_ts) > ENTRY_MAX_AGE_H * 3600:
            if t.rejected_reason != "age":
                t.rejected_reason = "age"
                age_h = (time.time() - t.created_ts) / 3600
                _log({"event": "skip", "mint": t.mint, "symbol": t.symbol,
                      "reason": f"zombie_age {age_h:.0f}h",
                      "real_sol": round(rs, 2)})
                print(f"[GRAD] SKIP {t.symbol} — zombie: {age_h:.0f}h old",
                      flush=True)
            return
        if (t.creator_coins is not None
                and t.creator_coins >= CREATOR_SERIAL_MIN_COINS
                and (t.creator_grads or 0) == 0):
            if t.rejected_reason != "creator":
                t.rejected_reason = "creator"
                _log({"event": "skip", "mint": t.mint, "symbol": t.symbol,
                      "reason": f"serial_creator {t.creator_coins} launches "
                                f"0 grads",
                      "real_sol": round(rs, 2)})
                print(f"[GRAD] SKIP {t.symbol} — serial creator: "
                      f"{t.creator_coins} launches, 0 graduations",
                      flush=True)
            return
        hour = time.gmtime().tm_hour
        n_h, rate_h = self._hour_completion(hour)
        if n_h >= HOUR_FILTER_MIN_N and rate_h < HOUR_FILTER_FLOOR:
            if t.rejected_reason != "hour":
                t.rejected_reason = "hour"
                _log({"event": "skip", "mint": t.mint, "symbol": t.symbol,
                      "reason": f"hour_filter:{hour:02d}utc"
                                f"(rate={rate_h:.0%},n={n_h})",
                      "real_sol": round(rs, 2)})
                print(f"[GRAD] SKIP {t.symbol} — hour filter: {hour:02d}:00 "
                      f"UTC completes {rate_h:.0%} (n={n_h}) < "
                      f"{HOUR_FILTER_FLOOR:.0%}", flush=True)
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
        if max_share > ENTRY_MAX_SHARE_CAP:
            if t.rejected_reason != "bundle":
                t.rejected_reason = "bundle"
                _log({"event": "skip", "mint": t.mint, "symbol": t.symbol,
                      "reason": f"single_buyer_push max_share={max_share:.2f}",
                      "real_sol": round(rs, 2)})
                print(f"[GRAD] SKIP {t.symbol} — single-buyer push "
                      f"(max_share={max_share:.0%})", flush=True)
            return
        brain_feats = {
            "velocity_5m": vel, "max_share": max_share, "steps": steps,
            "entry_real_sol": rs, "hour_utc": hour,
            "age_min": (time.time() - t.created_ts) / 60.0
                       if t.created_ts else None,
            "replies": t.reply_count, "creator_grads": t.creator_grads,
            "creator_coins": t.creator_coins,
        }
        ok, why = self.brain.allows(mint=t.mint, creator=t.creator,
                                    entry_real_sol=rs, velocity=vel,
                                    buyers=steps, features=brain_feats)
        if not ok:
            if t.rejected_reason != why:
                t.rejected_reason = why
                _log({"event": "skip", "mint": t.mint, "symbol": t.symbol,
                      "reason": f"brain:{why}", "real_sol": round(rs, 2)})
                print(f"[GRAD] SKIP {t.symbol} — brain veto: {why}", flush=True)
            return

        # ---- SUBMIT (friction model: fills at the NEXT poll, not now) ----
        self.pending[t.mint] = {
            "type": "buy", "decision_ts": time.time(),
            "decision_price": t.v_sol / t.v_tok,
            "decision_real_sol": rs,
            "velocity": round(vel, 3), "buyers": steps,
            "max_share": round(max_share, 3),
            "max_dump_sol": round(t.max_dump_sol, 2),
            "dump_recovered": t.dump_recovered,
        }

    # ---------------- friction-model execution ----------------
    def _charge_fee(self):
        acct = self.state["account"]
        acct["fees_sol"] = round(acct.get("fees_sol", 0.0) + TX_FEE_SOL, 6)

    def _execute_pending(self, t: Tracker):
        """Fill the order submitted at the previous poll against the curve
        state observed NOW — the tx was in flight while the curve moved."""
        order = self.pending.pop(t.mint, None)
        if not order or order["type"] != "buy":
            if order:  # pending sell — route through close at current state
                self._close(t.mint, order["reason"], order["haircut"])
            return
        self._charge_fee()  # submitted txs pay the fee, filled or reverted
        if len(self.state["positions"]) >= MAX_CONCURRENT:
            return  # slot got taken while in flight; fee already paid
        price_now = t.v_sol / t.v_tok
        moved_pct = (price_now / order["decision_price"] - 1.0) * 100.0
        if moved_pct > ENTRY_SLIPPAGE_BOUND_PCT:
            acct = self.state["account"]
            acct["realized_sol"] = round(
                acct.get("realized_sol", 0.0) - TX_FEE_SOL, 6)
            _save_state(self.state)
            _log({"event": "entry_fail", "mint": t.mint, "symbol": t.symbol,
                  "reason": "tx_revert_slippage",
                  "moved_pct": round(moved_pct, 2),
                  "fee_sol": TX_FEE_SOL,
                  "real_sol": round(t.real_sol, 2)})
            print(f"[GRAD] REVERT {t.symbol} buy — price moved "
                  f"{moved_pct:+.1f}% in flight (fee {TX_FEE_SOL} SOL)",
                  flush=True)
            return
        rs = t.real_sol
        land_delay = time.time() - order["decision_ts"]
        tokens = buy_on_curve(t.v_sol, t.v_tok, SIZE_SOL)
        self.state["positions"][t.mint] = {
            "symbol": t.symbol, "creator": t.creator, "entry_ts": time.time(),
            "entry_v_sol": t.v_sol, "entry_real_sol": rs,
            "size_sol": SIZE_SOL, "tokens": tokens,
            "fees_sol": TX_FEE_SOL,
            "entry_velocity": order["velocity"],
            "entry_buyers": order["buyers"],
            "entry_max_share": order["max_share"],
        }
        _save_state(self.state)
        _log({"event": "open", "mint": t.mint, "symbol": t.symbol,
              "size_sol": SIZE_SOL, "tokens": tokens,
              "real_sol": round(rs, 2),
              "quote_real_sol": order["decision_real_sol"],
              "land_delay_s": round(land_delay, 1),
              "moved_in_flight_pct": round(moved_pct, 2),
              "velocity_5m": round(order["velocity"], 2),
              "buyers": order["buyers"], "max_share": order["max_share"],
              "max_dump_sol": order.get("max_dump_sol"),
              "dump_recovered": order.get("dump_recovered"),
              "creator_coins": t.creator_coins,
              "creator_grads": t.creator_grads,
              "fill": "honest_curve_delayed"})
        print(f"[GRAD] OPEN {t.symbol} ({t.mint[:8]}) at real_sol={rs:.2f} "
              f"(quoted {order['decision_real_sol']:.2f}, "
              f"{moved_pct:+.1f}% in flight) size={SIZE_SOL} SOL", flush=True)
        self.alerts.on_open(t.symbol, t.mint, rs, SIZE_SOL,
                            order["velocity"], EXIT_REAL_SOL - rs)

    def _place_sell(self, mint: str, reason: str, haircut_pct: float):
        """Submit a sell — it lands at the next poll's curve state. If the
        token migrates first, the migration path (with haircut) wins the
        race, exactly like a live sell losing to graduation."""
        cur = self.pending.get(mint)
        if cur and cur["type"] == "sell":
            return
        self.pending[mint] = {"type": "sell", "reason": reason,
                              "haircut": haircut_pct,
                              "decision_ts": time.time()}

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
        self._charge_fee()  # the sell tx itself
        fees = pos.get("fees_sol", 0.0) + TX_FEE_SOL
        pnl = max(-(pos["size_sol"] + fees), net - pos["size_sol"] - fees)
        net_pct = pnl / pos["size_sol"] * 100.0
        self.state["account"]["realized_sol"] = round(
            self.state["account"].get("realized_sol", 0.0) + pnl, 6)
        # Whipsaw monitor: remember abandoned positions so we can log whether
        # the token graduates after we bailed (the stop-tuning ground truth).
        if reason in ("stall_stop", "timeout", "disaster_stop"):
            self.state["recent_stops"][mint] = {
                "symbol": pos["symbol"], "stop_ts": time.time(),
                "pnl_sol": round(pnl, 5), "reason": reason}
        _save_state(self.state)
        hold = time.time() - pos["entry_ts"]
        _log({"event": "close", "mint": mint, "symbol": pos["symbol"],
              "pnl_sol": round(pnl, 5), "net_pct": round(net_pct, 2),
              "exit_reason": reason, "hold_s": round(hold, 0),
              "exit_real_sol": round(v_sol - VIRTUAL_SOL_INIT, 2),
              "haircut_pct": haircut_pct, "fees_sol": round(fees, 5)})
        bal = SEED_SOL + self.state["account"]["realized_sol"]
        print(f"[GRAD] CLOSE {pos['symbol']} {reason} pnl={pnl:+.4f} SOL "
              f"({net_pct:+.1f}%) hold={hold:.0f}s | balance={bal:.3f} SOL",
              flush=True)
        self.alerts.on_close(pos["symbol"], reason, pnl, net_pct, hold, bal)
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
                    # Clamp below the derived runway ceiling. edge_brain's
                    # ENTRY_BOUNDS top out at 82.5, above ENTRY_MAX_REAL_SOL
                    # (81.5) — an unclamped suggestion would raise the entry
                    # floor above the ceiling and silently freeze all trading.
                    self.entry_real_sol = min(sugg["entry_real_sol"]["value"],
                                              ENTRY_MAX_REAL_SOL - 0.5)
                if "velocity_floor" in sugg:
                    self.velocity_floor = sugg["velocity_floor"]["value"]
                if sugg:
                    print(f"[GRAD] brain re-tune: entry>={self.entry_real_sol} "
                          f"vel>={self.velocity_floor}", flush=True)
        except Exception as e:
            print(f"[GRAD] brain error: {type(e).__name__}: {str(e)[:80]}",
                  flush=True)

    def _on_migration(self, mint: str):
        order = self.pending.pop(mint, None)
        if order and order["type"] == "buy":
            # buy tx landed after graduation -> reverted, fee still paid
            self._charge_fee()
            acct = self.state["account"]
            acct["realized_sol"] = round(
                acct.get("realized_sol", 0.0) - TX_FEE_SOL, 6)
            _save_state(self.state)
            _log({"event": "entry_fail", "mint": mint,
                  "reason": "tx_revert_migrated", "fee_sol": TX_FEE_SOL})
        if mint in self.state["positions"]:
            # covers open positions AND in-flight sells that lost the race
            self._close(mint, "migration", MIGRATION_HAIRCUT_PCT)
        t = self.trackers.pop(mint, None)
        if t:
            print(f"[GRAD] {t.symbol} graduated — untracked", flush=True)
            self._shadow_outcome(t, "graduated")
            # Tail-hold handoff: only the organic cohort — our pre-grad
            # telemetry is the selection filter for the post-flush bounce
            steps, max_share = t.climb_quality()
            vel = t.velocity(VELOCITY_WINDOW_S)
            if (steps >= DIVERSITY_MIN_INTERVALS
                    and max_share <= BUNDLE_MAX_SHARE and vel >= 1.0):
                self.tail.on_graduation(mint, t.symbol, {
                    "steps": steps, "max_share": round(max_share, 2),
                    "velocity_5m": round(vel, 2)})
        rs = self.state["recent_stops"].pop(mint, None)
        if rs:
            elapsed = round(time.time() - rs.get("stop_ts", 0), 0)
            _log({"event": "post_stop_grad", "mint": mint,
                  "symbol": rs.get("symbol", "?"),
                  "stopped_pnl_sol": rs.get("pnl_sol"),
                  "stop_reason": rs.get("reason"),
                  "stop_to_grad_s": elapsed})
            print(f"[GRAD] WHIPSAW {rs.get('symbol','?')} graduated "
                  f"{elapsed:.0f}s after our {rs.get('reason')} "
                  f"({rs.get('pnl_sol'):+.4f} SOL left on the table)",
                  flush=True)
            self.alerts.on_whipsaw(rs.get("symbol", "?"), rs.get("reason", "?"),
                                   elapsed, rs.get("pnl_sol") or 0.0)
            _save_state(self.state)

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
                self.alerts.on_kill(
                    SEED_SOL + self.state["account"].get("realized_sol", 0.0))
                raise SystemExit(0)
            for mint in list(self.state["positions"].keys()):
                pos = self.state["positions"][mint]
                t = self.trackers.get(mint)
                dd = (pos["entry_v_sol"] - t.v_sol) if t else 0.0
                if dd >= DISASTER_STOP_SOL:
                    self._place_sell(mint, "disaster_stop", STALL_HAIRCUT_PCT)
                elif STALL_STOP_ENABLED and dd >= STALL_STOP_SOL:
                    since = pos.setdefault("underwater_since", now)
                    if now - since >= STALL_CONFIRM_S:
                        self._place_sell(mint, "stall_stop", STALL_HAIRCUT_PCT)
                else:
                    pos.pop("underwater_since", None)
                if (mint in self.state["positions"]
                        and now - pos["entry_ts"] > TIMEOUT_MIN * 60):
                    self._place_sell(mint, "timeout", STALL_HAIRCUT_PCT)
                # Orphaned-sell backstop. _execute_pending only runs from
                # curve_poll_loop, so a sell placed against a mint whose poll
                # is failing (API error, non-dict body, v_sol<=0) would sit in
                # self.pending forever — _place_sell early-returns on an
                # existing sell, so it never retries. PLEROMA hung this way for
                # 3782s through a 13.45 SOL drawdown with BOTH an 8 SOL
                # disaster stop and a 15-min timeout armed, and only closed
                # when it migrated. It got lucky at -2.27%; a rug would have
                # been -100%. Force the close here once the order is overdue.
                order = self.pending.get(mint)
                if (order and order["type"] == "sell"
                        and now - order["decision_ts"] > SELL_ORPHAN_S):
                    self.pending.pop(mint, None)
                    age = now - order["decision_ts"]
                    print(f"[GRAD] orphaned sell for {pos['symbol']} "
                          f"({order['reason']}) — forcing close after "
                          f"{age:.0f}s", flush=True)
                    self.alerts.on_orphaned_sell(pos["symbol"],
                                                 order["reason"], age)
                    self._close(mint, order["reason"], order["haircut"])
            for mint in list(self.trackers.keys()):
                if mint in self.state["positions"]:
                    continue
                t = self.trackers[mint]
                if t.real_sol < HOT_ZONE_MIN_SOL - 10:
                    self._shadow_outcome(t, "died")     # dumped out of zone
                    del self.trackers[mint]
                elif now - t.last_change_ts > STALE_DROP_S:
                    self._shadow_outcome(t, "stalled")  # 30 min, no trades
                    del self.trackers[mint]
            # Prune in-flight buys whose token left the tracked set
            for mint in list(self.pending.keys()):
                if (self.pending[mint]["type"] == "buy"
                        and mint not in self.trackers):
                    del self.pending[mint]
            # Expire whipsaw-monitor entries after 24h (didn't graduate = a
            # good stop; the absence of a post_stop_grad event is the label)
            expired = [m for m, r in self.state["recent_stops"].items()
                       if now - r.get("stop_ts", 0) > 86400]
            if expired:
                for m in expired:
                    del self.state["recent_stops"][m]
                _save_state(self.state)
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
            # Dead-man + daily digest (Trigger 5). Both swallow their own
            # errors internally; wrapped anyway so alerting can never stop
            # position management from running.
            try:
                self.alerts.check_dead_man(tracked=len(self.trackers), now=now)
                self.alerts.maybe_digest(
                    now=now,
                    balance=SEED_SOL + self.state["account"].get(
                        "realized_sol", 0.0),
                    seed=SEED_SOL, closes_fn=_read_closes,
                    shadow_rate_fn=self._shadow_rate_str,
                    tail_sol=self.state.get("tail", {}).get(
                        "realized_sol", 0.0),
                    open_positions=len(self.state["positions"]))
            except Exception as e:
                print(f"[GRAD] alert error: {type(e).__name__}: {str(e)[:80]}",
                      flush=True)
            if now - last_heartbeat > 60:
                bal = SEED_SOL + self.state["account"].get("realized_sol", 0.0)
                hot = sorted((t for t in self.trackers.values()),
                             key=lambda x: -x.real_sol)[:3]
                hot_s = " ".join(f"{t.symbol}@{t.real_sol:.1f}" for t in hot)
                print(f"[GRAD] heartbeat tracked={len(self.trackers)} "
                      f"open={len(self.state['positions'])} "
                      f"tail={self.tail.active_count} "
                      f"balance={bal:.3f} SOL | hottest: {hot_s} "
                      f"@ {time.strftime('%H:%M:%S')}", flush=True)
                last_heartbeat = now
            await asyncio.sleep(MANAGE_TICK_S)

    async def run(self):
        bal = SEED_SOL + self.state["account"].get("realized_sol", 0.0)
        print(f"Graduation sniper v3 (SCALP-ONLY) | PAPER | "
              f"seed {SEED_SOL:.3f} SOL (balance {bal:.3f}) | "
              f"scalp band [{self.entry_real_sol}, {ENTRY_MAX_REAL_SOL}) "
              f"-> exit {EXIT_REAL_SOL} (runway >={MIN_RUNWAY_SOL} SOL) | "
              f"vel>={self.velocity_floor}/5m | max_share<={ENTRY_MAX_SHARE_CAP} | "
              f"size {SIZE_SOL} SOL x{MAX_CONCURRENT} | "
              f"stall_stop={'on' if STALL_STOP_ENABLED else 'OFF'} "
              f"disaster={DISASTER_STOP_SOL} | kill: {KILL_FILE}",
              flush=True)
        self.alerts.on_start(balance=bal, entry_lo=self.entry_real_sol,
                             entry_hi=ENTRY_MAX_REAL_SOL,
                             exit_at=EXIT_REAL_SOL)
        await asyncio.gather(
            self.discovery_loop(),
            self.curve_poll_loop(),
            self.migration_ws_loop(),
            self.manage_loop(),
            self.tail.run(),
        )


def main():
    if os.path.exists(KILL_FILE):
        raise SystemExit(f"kill file present ({KILL_FILE}) — remove to start")
    asyncio.run(GraduationSniper().run())


if __name__ == "__main__":
    main()
