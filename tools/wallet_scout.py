"""
tools/wallet_scout.py

Continuous wallet-discovery + monitoring agent. Replaces the one-shot
discovery passes we've been running manually. Once started, it self-runs:

  Every ITERATION_INTERVAL_S (default 1 hour):
    1. Refresh candidate pool from multiple curated sources:
         - GMGN top-PnL leaderboard (7d + 30d)
         - Currently-watched roster (re-verify they still qualify)
         - Any wallets in the running watchlist from prior iterations
    2. For each candidate, ensure Helius cache is fresh (deepen if stale)
    3. Profile + apply strict edge criterion + recency filter
    4. Update `logs/_wallet_journal.jsonl` with this iteration's verdict
    5. Compute roster changes via probation logic:
         - Promote: qualified in PROBATION_QUALIFIES consecutive iterations
         - Demote: disqualified in PROBATION_DISQUALIFIES consecutive iterations
         - Hard cap at MAX_ROSTER_SIZE (rank by mean_drop_best)
    6. Write updated `logs/streaming_roster.json` atomically; the streaming
       follower hot-reloads on mtime change.
    7. Append PROMOTED / DEMOTED action records to the journal.

Designed to run as a long-lived background process. Paper-only by design —
roster changes only affect the paper follower, never live execution.

Run: python -m tools.wallet_scout
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics as st
import time
import urllib.request

from dotenv import dotenv_values

from concurrent.futures import ThreadPoolExecutor

from tools.copy_replay import positions_full, RAW_CACHE
from tools.discover_edge_wallets import (
    profile_wallet, passes_strict,
    MIN_N, MIN_SPAN_DAYS, MIN_MEAN_PCT, MIN_WIN_RATE,
    MIN_MEAN_DROP_BEST, MIN_MEDIAN_HOLD_S,
)
from tools.fetch_deeper import fetch_deep
from tools.discover_gmgn import fetch_rank as gmgn_fetch_rank, passes_filter as gmgn_prefilter
from tools.creator_audit import detect_self_rugger
from tools.helius_compat import get_address_transactions as _free_addr_txns

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL = os.path.join(ROOT, "logs", "_wallet_journal.jsonl")
ROSTER = os.path.join(ROOT, "logs", "streaming_roster.json")
SCOUT_STATE = os.path.join(ROOT, "logs", "_scout_state.json")
BOT_WALLETS_FILE = os.path.join(ROOT, "logs", "bot_wallets.json")  # ~8,599 known addrs
BLEEDERS_FILE = os.path.join(ROOT, "logs", "_bleeders_blacklist.json")  # never re-promote

# Broader-intake settings (operator-set 2026-05-25: "ship all 4")
BOT_WALLETS_SWEEP_WORKERS = 12   # concurrency for the 8k-wallet recency sweep
BIRDEYE_TRENDING_LIMIT = 30      # how many trending tokens to fetch top traders from
BIRDEYE_TOP_TRADERS_PER_TOKEN = 5
BIRDEYE_SLEEP = 1.2              # Birdeye standard tier rate limit
COBUYER_MINTS_PER_WALLET = 5     # for each roster wallet, look at its last N mints
COBUYER_BUYERS_PER_MINT = 30

# Free-RPC degraded-mode caps (Helius out of credit → mainnet-beta rate limits)
# When _key() returns empty, the discovery sources fall back to free RPC via
# helius_compat. Free RPC throttles at ~10 req/s so we scope down aggressively.
FREE_RPC_SWEEP_CAP = 200         # only check the N most-recent bot_wallets when free-RPC
FREE_RPC_SWEEP_WORKERS = 3       # mainnet-beta tolerates ~3-4 concurrent threads
FREE_RPC_COBUYER_BUYERS = 10     # per-mint buyer fetch cap when free-RPC (was 30)

# Cadence — operator-set 2026-05-28: 15 min. Balances Helius credit burn vs
# discovery latency. The 8k-wallet bot_wallets sweep takes ~14min on Helius
# Enhanced, so this cycle is essentially "sweep finishes → 1 min pause → next
# sweep". Healthier credit burn rate than the 5-min setting while still keeping
# new-winner promotion latency under ~30min worst-case.
ITERATION_INTERVAL_S = 900.0    # 15 min (was 300 → too aggressive)
ITERATION_INTERVAL_MIN = 15     # human-readable

# Probation — loosened 2026-05-27. Single-qualify means a candidate that
# ranks top-N for ONE iteration is promoted immediately (was 2 consecutive).
# Trade-off: more new wallets faster, slightly higher rate of dud promotions
# (which the 2-iter demotion + creator_audit + blacklist still cleans up).
PROBATION_QUALIFIES = 1         # consecutive iterations in TOP_N before promotion (was 2)
PROBATION_DISQUALIFIES = 2      # consecutive iters outside TOP_N before demotion
TOP_N = 30                      # the agent picks the best N candidates by edge score (was 20)
MAX_ROSTER_SIZE = 35            # hard cap on absolute roster size (was 25)

# Minimum floors — wallets below these can't be scored at all (data too thin)
SCORE_FLOOR_N = 10
SCORE_FLOOR_SPAN_DAYS = 3.0

# Cache freshness — re-fetch a wallet's history only if older than this
CACHE_STALE_AFTER_S = 24 * 3600

# Recency — wallet must be active on pump.fun within this many days
ACTIVE_RECENCY_DAYS = 7

# Candidate-source caps
GMGN_LIMIT = 100                # top-N from each period


def _key():
    """Helius API key. When Helius credits are exhausted the key is commented
    out in .env; we soft-fail (return empty) instead of SystemExit so the
    scout can still iterate over already-cached wallets and apply the
    cross-purge / blacklist logic. Calls that genuinely need Helius will
    fail with 401/429 and be caught by their own try/except blocks."""
    return dotenv_values(os.path.join(ROOT, ".env")).get("HELIUS_API_KEY", "")


def _now() -> float:
    return time.time()


def _load_scout_state() -> dict:
    """Tracks: per-wallet running counts of qualified / disqualified consecutive iterations."""
    if os.path.exists(SCOUT_STATE):
        try:
            return json.load(open(SCOUT_STATE))
        except Exception:
            pass
    return {"iter": 0, "wallets": {}}


def _save_scout_state(s: dict):
    tmp = SCOUT_STATE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, SCOUT_STATE)


def _journal_append(rec: dict):
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _write_roster(addrs: list[str]):
    """Atomic write so the follower's hot-reload sees a consistent file."""
    tmp = ROSTER + ".tmp"
    json.dump(sorted(addrs), open(tmp, "w"), indent=1)
    os.replace(tmp, ROSTER)


def _read_roster() -> set[str]:
    if not os.path.exists(ROSTER):
        return set()
    try:
        return set(json.load(open(ROSTER)))
    except Exception:
        return set()


def _is_recent_active(wallet: str, key: str) -> bool:
    """Cheap: any pump.fun SWAP in last ACTIVE_RECENCY_DAYS days?

    2026-05-28 RE-ROUTE: previously hit Helius Enhanced (~144k calls/day on
    scout intake). The scout runs on a 15-min cycle — recency probes are not
    latency-critical, so we now use the free public RPC primary path (via
    rpc_pool round-robin to mainnet-beta). Helius Enhanced kept as fallback
    only if the free path returns nothing AND we still have a key — covers
    rare cases where mainnet-beta omits a real recent swap.
    """
    txns = None
    # PRIMARY: free RPC (mainnet-beta via rpc_pool)
    try:
        txns = _free_addr_txns(wallet, limit=10)
    except Exception:
        txns = None
    # FALLBACK: Helius Enhanced only if free path empty AND key present
    if (not txns) and key:
        url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={key}&limit=10"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout"}), timeout=20) as r:
                txns = json.load(r)
        except Exception:
            return False
    cutoff = _now() - ACTIVE_RECENCY_DAYS * 86400
    return any(
        t.get("source") in ("PUMP_FUN", "PUMP_AMM", "PUMPSWAP")
        and t.get("type") == "SWAP"
        and t.get("timestamp", 0) >= cutoff
        for t in (txns or [])
    )


def _ensure_cache(wallet: str, key: str) -> dict | None:
    """Return a wallet profile via the cheapest available source.

    2026-05-28 rewrite: replaces the old fetch_deep call (which was burning
    ~576k Helius calls/day) with the wallet_profile_cheap tiered fallback:

      Tier 1: local raw_txns cache       (FREE, ~100% hit rate after warm-up)
      Tier 2: wallet_realized_pnl cache  (FREE, partial profile, soft signal)
      Tier 3: free public RPC            (FREE, slow, full profile)
      Tier 4: Helius shallow (1 page)    (1 call, vs fetch_deep's ~4)

    Expected daily Helius cost drop: ~576k → ~25-30k for this function.
    """
    from tools.wallet_profile_cheap import cheap_profile
    p, src = cheap_profile(wallet, key, allow_free_rpc=False,
                            allow_shallow=bool(key))
    return p


def _bot_wallets_recency_active(key: str) -> set[str]:
    """Concurrent recency sweep over the bot_wallets universe.

    Helius mode: all 8,599 wallets checked with 12 workers (~3-5 min/iter).
    Free-RPC mode: capped at FREE_RPC_SWEEP_CAP wallets with 3 workers,
    prioritizing wallets we've already cached recently (i.e. likely-active
    ones) so the limited free-RPC budget hits the highest-signal candidates.
    """
    if not os.path.exists(BOT_WALLETS_FILE):
        return set()
    try:
        bw = list(json.load(open(BOT_WALLETS_FILE)).keys())
    except Exception:
        return set()

    workers = BOT_WALLETS_SWEEP_WORKERS
    if not key:
        # Degraded mode: prioritize wallets whose raw_txns cache exists
        # (we've seen them active before), then truncate to FREE_RPC_SWEEP_CAP.
        cached, uncached = [], []
        for w in bw:
            if os.path.exists(os.path.join(RAW_CACHE, w + ".json")):
                cached.append(w)
            else:
                uncached.append(w)
        # Sort cached by mtime desc (most-recently-active first), then take cap
        cached.sort(key=lambda w: os.path.getmtime(os.path.join(RAW_CACHE, w + ".json")),
                    reverse=True)
        bw = (cached + uncached)[:FREE_RPC_SWEEP_CAP]
        workers = FREE_RPC_SWEEP_WORKERS
        print(f"  [intake] FREE-RPC mode: capping bot_wallets sweep to "
              f"{len(bw)} wallets, {workers} workers", flush=True)

    active: set[str] = set()

    def _check(w):
        return w if _is_recent_active(w, key) else None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for w in ex.map(_check, bw):
            if w:
                active.add(w)
    return active


def _birdeye_trending_traders(birdeye_key: str) -> set[str]:
    """Fetch trending pump.fun-eligible tokens from Birdeye and collect their
    top-PnL traders as candidates. Throttled to Birdeye's standard rate limit."""
    if not birdeye_key:
        return set()
    out: set[str] = set()
    try:
        url = (f"https://public-api.birdeye.so/defi/token_trending"
               f"?sort_by=rank&sort_type=asc&offset=0&limit={BIRDEYE_TRENDING_LIMIT}")
        req = urllib.request.Request(url, headers={"X-API-KEY": birdeye_key,
                                                    "x-chain": "solana",
                                                    "User-Agent": "scout"})
        d = json.load(urllib.request.urlopen(req, timeout=20))
        tokens = [(t.get("address") or "") for t in (d.get("data", {}).get("tokens") or [])
                  if t.get("address")]
    except Exception:
        return out
    time.sleep(BIRDEYE_SLEEP)
    for addr in tokens:
        try:
            url = (f"https://public-api.birdeye.so/defi/v2/tokens/top_traders"
                   f"?address={addr}&time_frame=24h&sort_type=desc&sort_by=volume"
                   f"&offset=0&limit={BIRDEYE_TOP_TRADERS_PER_TOKEN}")
            req = urllib.request.Request(url, headers={"X-API-KEY": birdeye_key,
                                                        "x-chain": "solana",
                                                        "User-Agent": "scout"})
            d = json.load(urllib.request.urlopen(req, timeout=20))
            for it in (d.get("data", {}).get("items") or []):
                w = it.get("owner") or it.get("address")
                # bias toward profitable when realizedPnl is available
                rp = it.get("realizedPnl")
                if w and (rp is None or rp > 0):
                    out.add(w)
        except Exception:
            continue
        time.sleep(BIRDEYE_SLEEP)
    return out


def _cobuyer_expansion(roster_wallets: set[str], key: str) -> set[str]:
    """For each roster wallet, look at its last few pump.fun mint buys, fetch
    other early buyers of those mints, and aggregate non-roster co-buyers."""
    out: set[str] = set()
    for w in roster_wallets:
        cp = os.path.join(RAW_CACHE, w + ".json")
        if not os.path.exists(cp):
            continue
        try:
            txns = json.load(open(cp))
        except Exception:
            continue
        # collect last N mints this wallet bought
        seen_mints = []
        for t in txns:
            if t.get("source") not in ("PUMP_FUN", "PUMP_AMM", "PUMPSWAP") or t.get("type") != "SWAP":
                continue
            for tt in t.get("tokenTransfers", []) or []:
                m = tt.get("mint")
                if m and m not in ("So11111111111111111111111111111111111111112",) \
                   and tt.get("toUserAccount") == w:
                    if m not in seen_mints:
                        seen_mints.append(m)
                    break
            if len(seen_mints) >= COBUYER_MINTS_PER_WALLET:
                break
        # 2026-05-28 RE-ROUTE: scout cycle is 15min, co-buyer mint queries are
        # not latency-critical. Free RPC primary; Helius Enhanced only if free
        # path returns nothing AND we have a key.
        buyers_limit = FREE_RPC_COBUYER_BUYERS  # always use the conservative cap
        for m in seen_mints:
            # PRIMARY: free RPC
            try:
                page = _free_addr_txns(m, limit=buyers_limit)
            except Exception:
                page = None
            # FALLBACK: Helius Enhanced
            if not page and key:
                try:
                    url = (f"https://api.helius.xyz/v0/addresses/{m}/transactions"
                           f"?api-key={key}&limit={buyers_limit}&type=SWAP")
                    req = urllib.request.Request(url, headers={"User-Agent": "scout-cobuy"})
                    page = json.load(urllib.request.urlopen(req, timeout=20))
                except Exception:
                    page = None
            if not page:
                continue
            for tx in page or []:
                for tt in tx.get("tokenTransfers", []) or []:
                    if tt.get("mint") == m:
                        b = tt.get("toUserAccount")
                        if b and b != w and b not in roster_wallets:
                            out.add(b)
    return out


def _collect_candidates(scout_state: dict, current_roster: set[str],
                        key: str, birdeye_key: str) -> set[str]:
    """Build the candidate pool for this iteration.

    Sources (all four operator-shipped 2026-05-25):
      1. Current roster (re-verify)
      2. Watchlist accumulated in scout_state from prior iterations
      3. GMGN top-PnL leaderboards (7d + 30d)
      4. bot_wallets.json recency-active sweep (8,599 addrs, 12-worker concurrent)
      5. Birdeye trending-token top traders
      6. Co-buyers of current roster wallets
    """
    # Load bleeders blacklist — never consider these as candidates again
    bleeders: set[str] = set()
    if os.path.exists(BLEEDERS_FILE):
        try:
            bleeders = set(json.load(open(BLEEDERS_FILE)))
        except Exception:
            pass

    cands: set[str] = set()
    cands |= current_roster
    cands |= set(scout_state.get("wallets", {}).keys())
    # GMGN curated
    for period in ("7d", "30d"):
        try:
            entries = gmgn_fetch_rank(period, GMGN_LIMIT)
        except Exception:
            entries = []
        for e in entries or []:
            ok, _ = gmgn_prefilter(e, period)
            if ok:
                addr = e.get("address") or e.get("wallet_address")
                if addr:
                    cands.add(addr)
    n_after_gmgn = len(cands)
    # 4) bot_wallets recency sweep — DISABLED 2026-05-28 per operator directive.
    # This source was probing 8,599 wallets per iteration with Helius Enhanced REST
    # = ~825k calls/day, which exhausted the new Helius key in <24hr (HTTP 429
    # "max usage reached"). Removed entirely. Other discovery sources (GMGN,
    # Birdeye, co-buyer expansion of roster, journal watchlist) remain active.
    # Re-enable by uncommenting the block below if/when on an unlimited Helius
    # plan. The function _bot_wallets_recency_active() is still defined for
    # ad-hoc use.
    # t0 = time.time()
    # bw_active = _bot_wallets_recency_active(key)
    # print(f"  [intake] bot_wallets sweep: +{len(bw_active - cands)} new candidates "
    #       f"({len(bw_active)} active total, {time.time()-t0:.0f}s)", flush=True)
    # cands |= bw_active
    print(f"  [intake] bot_wallets sweep: DISABLED (Helius credit conservation)", flush=True)
    # 5) Birdeye trending traders
    t0 = time.time()
    bd = _birdeye_trending_traders(birdeye_key)
    print(f"  [intake] Birdeye trending: +{len(bd - cands)} new ({time.time()-t0:.0f}s)", flush=True)
    cands |= bd
    # 6) Co-buyers of roster
    t0 = time.time()
    cb = _cobuyer_expansion(current_roster, key)
    print(f"  [intake] co-buyers of roster: +{len(cb - cands)} new ({time.time()-t0:.0f}s)", flush=True)
    cands |= cb
    # Hard filter: blacklist applied LAST so nothing slips through any source
    if bleeders:
        before = len(cands)
        cands -= bleeders
        if before > len(cands):
            print(f"  [intake] blacklist filtered: -{before - len(cands)} bleeder(s)", flush=True)
    print(f"  [intake] TOTAL candidates: {len(cands)} (was {n_after_gmgn} before new sources)",
          flush=True)
    return cands


def _score_wallet(p: dict) -> float:
    """Composite edge score. Replaces the binary strict-pass test so the agent
    can rank candidates and pick the best N — even when no wallet clears an
    absolute bar. Higher = better. Returns -inf for wallets below absolute
    minimum floors (sample/span too thin to score).

      score = mean_pct * win_rate
              * sample_size_weight     (saturates at n=50)
              * span_weight            (saturates at 14d)
              * hold_factor            (1.0 at >=120s, scales down for fast wallets)
              * concentration_factor   (mean_drop_best / mean — penalizes lottery wins)

    The result has units of "expected % per trade adjusted for confidence,
    copyability, and concentration." A wallet with mean +30%, 65% win, n=40,
    span 10d, hold 120s, mxb=20% gives ~30 * 0.65 * 0.8 * 0.71 * 1.0 * 0.67 ≈ 7.4.
    Compare against the bare-mean only (30 * 0.65 = 19.5) which would over-rank
    a concentrated wallet with thin sample.
    """
    if not p:
        return float("-inf")
    n = p.get("n", 0)
    span = p.get("span_days", 0)
    if n < SCORE_FLOOR_N or span < SCORE_FLOOR_SPAN_DAYS:
        return float("-inf")
    mean = p.get("mean_pct", 0)
    win = p.get("win_rate", 0)
    mxb = p.get("mean_drop_best", 0)
    hold = p.get("med_hold_s", 0)
    if mean <= 0 or win <= 0:
        return float("-inf")
    n_weight = min(1.0, n / 50.0)
    span_weight = min(1.0, span / 14.0)
    hold_factor = min(1.0, hold / 120.0)
    conc_factor = max(0.0, min(1.0, mxb / mean)) if mean > 0 else 0.0
    return mean * win * n_weight * span_weight * hold_factor * conc_factor


def _iteration(scout_state: dict, key: str, birdeye_key: str = "") -> dict:
    """One scan + roster update. Returns a summary."""
    iter_n = scout_state["iter"] + 1
    scout_state["iter"] = iter_n
    iter_ts = _now()
    current_roster = _read_roster()

    # Cross-purge: if a previously-promoted wallet has since landed on the
    # bleeders blacklist (e.g. self-rugger detection ran post-promotion, or
    # a manual add), kick it out NOW — don't wait for probation demotion.
    # This closes the gap where a roster wallet sits in the blacklist for
    # iterations because the scout only filters *new* candidates.
    bl_purge: set[str] = set()
    if os.path.exists(BLEEDERS_FILE):
        try:
            bl_purge = set(json.load(open(BLEEDERS_FILE))) & current_roster
        except Exception:
            pass
    if bl_purge:
        current_roster = current_roster - bl_purge
        # Persist immediately so the streaming follower hot-reloads
        _write_roster(sorted(current_roster))
        for w in bl_purge:
            _journal_append({"iter": iter_n, "ts": iter_ts,
                             "action": "CROSS-PURGE", "wallet": w,
                             "reason": "roster wallet found on blacklist"})
            print(f"  [cross-purge] removed {w[:12]} (was on roster + blacklist)", flush=True)

    cands = _collect_candidates(scout_state, current_roster, key, birdeye_key)
    print(f"[SCOUT iter {iter_n}] candidates: {len(cands)} ({len(current_roster)} from roster)", flush=True)

    profiled = {}
    for w in cands:
        # Recency gate first — cheap, avoids deep-fetching dormant wallets
        if w not in current_roster and not _is_recent_active(w, key):
            continue
        p = _ensure_cache(w, key)
        if not p:
            continue
        # Self-rugger check: if this wallet creates AND trades its own tokens,
        # auto-blacklist before it ever scores. Re-uses the already-fetched cache.
        try:
            cp = os.path.join(RAW_CACHE, w + ".json")
            if os.path.exists(cp):
                txns = json.load(open(cp))
                is_rugger, stats = detect_self_rugger(w, txns)
                if is_rugger:
                    bl_path = os.path.join(ROOT, "logs", "_bleeders_blacklist.json")
                    bl = set()
                    if os.path.exists(bl_path):
                        try: bl = set(json.load(open(bl_path)))
                        except Exception: pass
                    if w not in bl:
                        bl.add(w)
                        json.dump(sorted(bl), open(bl_path, "w"), indent=1)
                    _journal_append({"iter": scout_state["iter"], "ts": _now(),
                                      "action": "AUTO-BLACKLIST", "wallet": w,
                                      "reason": f"self-rugger (created {stats['created']}, "
                                                f"traded {stats['traded']}, "
                                                f"self-ratio {stats['self_ratio']})"})
                    print(f"  [creator_audit] BLACKLISTED {w[:12]}: {stats}", flush=True)
                    continue
        except Exception as e:
            print(f"  [creator_audit] error on {w[:8]}: {e}", flush=True)
        profiled[w] = p

    print(f"[SCOUT iter {iter_n}] profiled (recent-active + has history): {len(profiled)}", flush=True)

    wallets_state = scout_state.setdefault("wallets", {})

    # Score every profiled wallet; the agent picks the top N by composite edge
    # score rather than refusing to act when nothing clears a strict bar.
    scored = [(w, p, _score_wallet(p)) for w, p in profiled.items()]
    # Drop wallets below absolute floors (score = -inf)
    scored = [(w, p, s) for w, p, s in scored if s != float("-inf")]
    scored.sort(key=lambda x: x[2], reverse=True)
    top_n_set = set(w for w, _, _ in scored[:TOP_N])

    print(f"[SCOUT iter {iter_n}] scored {len(scored)} wallets, top {TOP_N}:")
    for w, p, s in scored[:TOP_N]:
        print(f"  score={s:>7.2f}  {w[:12]}  n={p['n']:>3} mean={p['mean_pct']:+.1f}% "
              f"win={p['win_rate']*100:>2.0f}% mxb={p['mean_drop_best']:+.1f}% hold={int(p['med_hold_s'])}s",
              flush=True)

    for w, p, s in scored:
        ws = wallets_state.setdefault(w, {"q_consec": 0, "dq_consec": 0, "last_iter": 0})
        in_top = w in top_n_set
        if in_top:
            ws["q_consec"] = ws.get("q_consec", 0) + 1
            ws["dq_consec"] = 0
        else:
            ws["q_consec"] = 0
            ws["dq_consec"] = ws.get("dq_consec", 0) + 1
        ws["last_iter"] = iter_n
        ws["last_profile"] = p
        ws["last_score"] = s
        _journal_append({
            "iter": iter_n, "ts": iter_ts, "wallet": w,
            "profile": p, "score": round(s, 3), "in_top_n": in_top,
            "q_consec": ws["q_consec"], "dq_consec": ws["dq_consec"],
        })

    # Promotions: in top N AND q_consec >= PROBATION_QUALIFIES AND not yet on roster
    to_add = [w for w in top_n_set
              if w not in current_roster
              and wallets_state[w]["q_consec"] >= PROBATION_QUALIFIES]
    # Demotions: in roster AND dq_consec >= PROBATION_DISQUALIFIES
    to_remove = [w for w in current_roster
                 if wallets_state.get(w, {}).get("dq_consec", 0) >= PROBATION_DISQUALIFIES]

    new_roster = (current_roster | set(to_add)) - set(to_remove)

    # Absolute cap (hard ceiling); rank by score, keep best
    if len(new_roster) > MAX_ROSTER_SIZE:
        ranked = sorted(
            new_roster,
            key=lambda w: wallets_state.get(w, {}).get("last_score", -1e9),
            reverse=True,
        )
        keep = set(ranked[:MAX_ROSTER_SIZE])
        for w in (new_roster - keep):
            _journal_append({"iter": iter_n, "ts": iter_ts, "action": "capped_out",
                              "wallet": w, "reason": f"roster cap {MAX_ROSTER_SIZE}"})
        new_roster = keep

    # Log promotions / demotions
    for w in (new_roster - current_roster):
        _journal_append({"iter": iter_n, "ts": iter_ts, "action": "PROMOTED",
                          "wallet": w, "reason": f"qualified {wallets_state[w]['q_consec']} consec iters"})
    for w in (current_roster - new_roster):
        reason = (f"disqualified {wallets_state.get(w,{}).get('dq_consec',0)} consec iters"
                  if wallets_state.get(w, {}).get("dq_consec", 0) >= PROBATION_DISQUALIFIES
                  else "capped out")
        _journal_append({"iter": iter_n, "ts": iter_ts, "action": "DEMOTED",
                          "wallet": w, "reason": reason})

    _write_roster(sorted(new_roster))
    _save_scout_state(scout_state)

    return {
        "iter": iter_n, "candidates": len(cands), "profiled": len(profiled),
        "scored": len(scored), "top_n": len(top_n_set),
        "added": sorted(new_roster - current_roster),
        "removed": sorted(current_roster - new_roster),
        "roster_size": len(new_roster),
    }


async def run(interval_s: float):
    key = _key()
    birdeye_key = dotenv_values(os.path.join(ROOT, ".env")).get("BIRDEYE_API_KEY", "")
    print(f"[SCOUT] starting — interval {interval_s/60:.0f} min, "
          f"probation: {PROBATION_QUALIFIES}↑/{PROBATION_DISQUALIFIES}↓, top_n={TOP_N}, cap {MAX_ROSTER_SIZE}, "
          f"birdeye_key={'YES' if birdeye_key else 'no'}", flush=True)
    while True:
        state = _load_scout_state()
        try:
            t0 = time.time()
            summary = _iteration(state, key, birdeye_key)
            elapsed = time.time() - t0
            print(f"[SCOUT iter {summary['iter']}] done in {elapsed:.0f}s | "
                  f"scored={summary['scored']} top_n={summary['top_n']} roster={summary['roster_size']} "
                  f"added={summary['added']} removed={summary['removed']}", flush=True)
        except Exception as e:
            print(f"[SCOUT] iteration error: {type(e).__name__}: {str(e)[:200]}", flush=True)
        # Sleep until next iteration (cap at interval_s minus elapsed)
        await asyncio.sleep(max(60.0, interval_s))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=ITERATION_INTERVAL_S,
                    help=f"seconds between iterations (default {ITERATION_INTERVAL_S:.0f})")
    ap.add_argument("--once", action="store_true", help="run a single iteration then exit")
    args = ap.parse_args()
    key = _key()
    birdeye_key = dotenv_values(os.path.join(ROOT, ".env")).get("BIRDEYE_API_KEY", "")
    if args.once:
        state = _load_scout_state()
        summary = _iteration(state, key, birdeye_key)
        print(f"\n=== ONE-SHOT SUMMARY ===\n{json.dumps(summary, indent=2)}")
        return
    asyncio.run(run(args.interval))


if __name__ == "__main__":
    main()
