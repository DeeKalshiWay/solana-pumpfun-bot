"""
detector/whale_tracker.py

Tracks wallets by SOL VOLUME (lifetime SOL deployed + trade size) — a
complement to wallet_intel which classifies wallets by win-rate outcome.
A whale isn't necessarily smart; it just moves enough size that the
order itself is signal (or counter-signal on the sell side).

Subscribes to the SAME PumpPortal WS that wallet_intel uses (one shared
connection — pump.fun 403-bans multi-connection IPs) via the public
monitor.subscribe_trade() / subscribe_create() API.

Persistence
-----------
logs/whale_wallets.json — rolling aggregate of every wallet seen, capped
at MAX_TRACKED_WALLETS (LRU eviction by last_seen) so the JSON doesn't
grow unbounded on a busy network.

API
---
  whale_tracker.is_whale(addr) -> bool
      True if lifetime SOL buys ≥ WHALE_MIN_LIFETIME_SOL or avg trade
      size ≥ WHALE_MIN_AVG_TRADE_SOL.
  whale_tracker.whale_buyers_in_window(mint) -> list[str]
      Whale addresses that bought this mint within WHALE_WINDOW_S of
      creation. Same shape as wallet_intel.smart_buyers_in_window.
  whale_tracker.stats() -> dict
      For the dashboard /api/intel panel.
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from typing import Any

from loguru import logger

WHALE_STATE_FILE          = "logs/whale_wallets.json"
WHALE_MIN_LIFETIME_SOL    = float(os.environ.get("WHALE_MIN_LIFETIME_SOL",   "10.0"))
WHALE_MIN_AVG_TRADE_SOL   = float(os.environ.get("WHALE_MIN_AVG_TRADE_SOL",  "1.0"))
# Mints that have never seen any buy in this window after creation are
# kept anyway — the scorer might still ask. But entries older than this
# get pruned from the per-mint buyer cache.
WHALE_WINDOW_S            = float(os.environ.get("WHALE_WINDOW_S",           "10.0"))
# Buys that arrive before the matching create get queued for this long
# waiting for the create event. WS ordering on PumpPortal is normally fine
# but reorgs / network jitter can flip the order on tight launches.
WHALE_PENDING_GRACE_S     = float(os.environ.get("WHALE_PENDING_GRACE_S",    "5.0"))
# Eviction caps. Whale wallets are tiny (≤ a few hundred globally) but the
# tracker SEES every buyer; non-whale rows are evicted LRU by last_seen.
MAX_TRACKED_WALLETS       = int(os.environ.get("WHALE_MAX_TRACKED",          "5000"))
MAX_MINT_BUYERS           = int(os.environ.get("WHALE_MAX_MINT_BUYERS",      "2000"))


class WhaleTracker:
    """Aggregate-by-volume sibling to wallet_intel.

    Hot path is _on_trade — keep it short. Persistence is throttled
    to once every ~100 events to avoid disk thrash on busy mints.
    """

    def __init__(self):
        # addr -> dict(buys_sol, sells_sol, trade_count, first_seen, last_seen)
        self._wallets: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # mint -> list[(addr, ts)] within WHALE_WINDOW_S of mint creation
        self._mint_buyers: dict[str, list[tuple[str, float]]] = {}
        # mint -> create_ts (so we know when the buy window started)
        self._mint_created: dict[str, float] = {}
        # WS messages can arrive out of order on busy networks — buy events
        # sometimes land before their matching create. Buffer such buys
        # briefly so we can replay them once create finally arrives. Without
        # this, ~5–10% of early whale buys on volatile launches are silently
        # dropped. Entries older than WHALE_PENDING_GRACE_S are pruned.
        self._pending_buys: dict[str, list[tuple[str, float, float]]] = {}
        self._writes_since_save = 0
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not os.path.exists(WHALE_STATE_FILE):
            return
        try:
            with open(WHALE_STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            wallets = data.get("wallets", {})
            if isinstance(wallets, dict):
                # restore in original order; entries kept by last_seen
                items = sorted(wallets.items(),
                               key=lambda kv: kv[1].get("last_seen", 0))
                for addr, row in items:
                    if isinstance(row, dict):
                        self._wallets[addr] = row
            logger.info(
                f"[WHALE] Loaded {len(self._wallets)} wallets · "
                f"{sum(1 for w in self._wallets.values() if self._is_whale_row(w))} classified as whales"
            )
        except Exception as e:
            logger.warning(f"[WHALE] load failed: {e}")

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(WHALE_STATE_FILE), exist_ok=True)
            tmp = WHALE_STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "wallets":  dict(self._wallets),
                    "saved_at": time.time(),
                }, f)
            os.replace(tmp, WHALE_STATE_FILE)
        except Exception as e:
            logger.debug(f"[WHALE] save failed: {e}")

    # ── Classification ───────────────────────────────────────────────────────

    @staticmethod
    def _is_whale_row(w: dict[str, Any]) -> bool:
        buys = float(w.get("buys_sol", 0))
        n    = max(int(w.get("trade_count", 0)), 1)
        avg  = buys / n
        return buys >= WHALE_MIN_LIFETIME_SOL or avg >= WHALE_MIN_AVG_TRADE_SOL

    def is_whale(self, addr: str) -> bool:
        w = self._wallets.get(addr)
        return bool(w) and self._is_whale_row(w)

    # ── WS subscription ──────────────────────────────────────────────────────

    def attach(self, monitor) -> None:
        """Hook into the shared PumpPortal WS via the monitor's pub/sub."""
        try:
            monitor.subscribe_create(self._on_create)
            monitor.subscribe_trade(self._on_trade)
            logger.info("[WHALE] Attached to shared PumpPortal WS")
        except Exception as e:
            logger.warning(f"[WHALE] attach error: {e}")

    def _on_create(self, data: dict) -> None:
        mint = data.get("mint")
        if not mint:
            return
        now = time.time()
        self._mint_created[mint] = now
        # Drain any buys we received before the create event. They're valid
        # only if they arrived within the create window we're about to open
        # for this mint.
        pending = self._pending_buys.pop(mint, None)
        if not pending:
            return
        for addr, sol_amount, ts in pending:
            if (now - ts) > WHALE_PENDING_GRACE_S:
                continue   # too old; drop
            w = self._wallets.get(addr)
            if w and self._is_whale_row(w):
                buyers = self._mint_buyers.setdefault(mint, [])
                if not any(a == addr for a, _ in buyers):
                    buyers.append((addr, ts))

    def _on_trade(self, data: dict) -> None:
        """Hot path: keep it short. Aggregate SOL volume per wallet,
        and remember whale buyers per mint inside the early window."""
        tx_type = data.get("txType")
        if tx_type not in ("buy", "sell"):
            return
        addr   = data.get("traderPublicKey")
        mint   = data.get("mint")
        amount = float(data.get("solAmount", 0) or 0)
        if not addr or amount <= 0:
            return

        w = self._wallets.get(addr)
        now = time.time()
        if w is None:
            w = {
                "buys_sol":    0.0,
                "sells_sol":   0.0,
                "trade_count": 0,
                "first_seen":  now,
                "last_seen":   now,
            }
            self._wallets[addr] = w
        if tx_type == "buy":
            w["buys_sol"] += amount
        else:
            w["sells_sol"] += amount
        w["trade_count"] += 1
        w["last_seen"]    = now
        # LRU bump
        self._wallets.move_to_end(addr)

        # Per-mint whale buyer cache for the scorer's window query.
        if tx_type == "buy" and mint:
            created = self._mint_created.get(mint)
            if created is None:
                # Buy arrived before the create event (WS reorder / jitter).
                # Buffer it; _on_create drains the pending list on its way
                # in. Only whales need this — pleb buys for unknown mints
                # are no signal.
                if self._is_whale_row(w):
                    self._pending_buys.setdefault(mint, []).append((addr, amount, now))
                    # Prune entries older than the grace window to bound memory.
                    cutoff = now - WHALE_PENDING_GRACE_S
                    self._pending_buys[mint] = [
                        e for e in self._pending_buys[mint] if e[2] >= cutoff
                    ]
            else:
                in_window = (now - created) <= WHALE_WINDOW_S
                if in_window and self._is_whale_row(w):
                    buyers = self._mint_buyers.setdefault(mint, [])
                    # Dedup per mint — same whale buying twice doesn't double-count.
                    if not any(a == addr for a, _ in buyers):
                        buyers.append((addr, now))

        # Eviction + throttled save.
        self._writes_since_save += 1
        if len(self._wallets) > MAX_TRACKED_WALLETS:
            # Drop oldest by LRU but PIN classified whales — a whale that
            # goes dormant for an hour while 5000 noise wallets cycle
            # through shouldn't lose its classification. Iterate from
            # oldest; skip whales; pop the first non-whale found.
            n_to_drop = len(self._wallets) - MAX_TRACKED_WALLETS
            for _ in range(n_to_drop):
                victim = None
                for cand_addr, cand_row in self._wallets.items():
                    if not self._is_whale_row(cand_row):
                        victim = cand_addr
                        break
                if victim is None:
                    # Cache is entirely whales — that's a > 5000 whale
                    # global pop. Tune MAX_TRACKED_WALLETS up, but for
                    # now drop the oldest entry to bound memory.
                    self._wallets.popitem(last=False)
                else:
                    self._wallets.pop(victim, None)
        if len(self._mint_buyers) > MAX_MINT_BUYERS:
            # Prune the oldest mint buckets by their earliest buyer ts.
            oldest = sorted(
                self._mint_buyers.items(),
                key=lambda kv: kv[1][0][1] if kv[1] else 0,
            )[: max(1, MAX_MINT_BUYERS // 4)]
            for k, _ in oldest:
                self._mint_buyers.pop(k, None)
                self._mint_created.pop(k, None)
        if self._writes_since_save >= 100:
            self._writes_since_save = 0
            self._save()

    # ── Public lookups (called by signal_scorer per token) ───────────────────

    def whale_buyers_in_window(self, mint: str) -> list[str]:
        """Whale addresses that bought this mint within WHALE_WINDOW_S of
        its create event. Empty when the mint wasn't tracked or no whale
        showed up. Order = first-seen ascending."""
        rows = self._mint_buyers.get(mint, [])
        if not rows:
            return []
        return [addr for addr, _ in rows]

    def whale_buy_volume(self, mint: str) -> float:
        """Sum of buy-side SOL across whale wallets in the window. Used by
        the fusion engine to differentiate 1 whale @ 0.5 SOL from 1 whale
        @ 5 SOL — both fire whale_confirmed but only one is a real signal."""
        rows = self._mint_buyers.get(mint, [])
        if not rows:
            return 0.0
        # We don't keep per-mint amount in _mint_buyers (just addr+ts) to
        # keep the hot path small. Approximate via the wallet's avg trade
        # size — overcounts when a whale also trades other mints in the
        # window, but the lower-bound (avg) is good enough for the
        # fusion gate.
        total = 0.0
        for addr, _ in rows:
            w = self._wallets.get(addr)
            if not w:
                continue
            n = max(int(w.get("trade_count", 1)), 1)
            total += float(w.get("buys_sol", 0)) / n
        return total

    # ── Diagnostics for the dashboard /api/intel ─────────────────────────────

    def stats(self) -> dict:
        whales = [(a, w) for a, w in self._wallets.items() if self._is_whale_row(w)]
        whales.sort(key=lambda kv: kv[1].get("buys_sol", 0), reverse=True)
        return {
            "tracked_wallets":  len(self._wallets),
            "whale_count":      len(whales),
            "mints_with_whale": sum(1 for v in self._mint_buyers.values() if v),
            "top_whales": [
                {
                    "addr":        a[:8] + "...",
                    "buys_sol":    round(float(w.get("buys_sol", 0)), 2),
                    "trade_count": int(w.get("trade_count", 0)),
                }
                for a, w in whales[:10]
            ],
        }


# Singleton — imported by signal_scorer + main.py (for attach)
whale_tracker = WhaleTracker()
