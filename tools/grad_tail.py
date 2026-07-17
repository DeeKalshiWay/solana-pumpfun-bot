"""
tools/grad_tail.py

PAPER post-migration tail-hold — edge expansion #3 (2026-07-16).

The first minutes after a pump.fun token migrates to PumpSwap are a
forced-seller flush: curve snipers (the trade we run ourselves) dump in
unison at a known timestamp. Bundle-pushed junk keeps bleeding; tokens with
real organic demand get bought back in minutes 3-5 as the overhang clears.

Our selection edge is the pre-graduation telemetry the curve sniper already
collects: the sniper only hands us tokens whose climb looked organic (many
small buy-steps, no single-interval spike, positive velocity). We then wait
for the flush and paper-buy the confirmed bounce.

Price feed: Dexscreener free API (no key), PumpSwap pair priceNative (SOL).
Fills: mid price +/- FILL_COST_PCT each way (fee + slippage estimate — this
is cruder than the curve sniper's honest constant-product fills, so treat
results as directional until validated against real pool math).

Ledger: state["tail"] inside graduation_state.json — separate from the
curve sniper's book so the two strategies grade independently.
Events in graduation_trades.jsonl: tail_open / tail_close / tail_pass,
strategy "graduation_tail".
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request

# --- Entry (all measured against the first PumpSwap price we see) -----------
ENTRY_MIN_WAIT_S = 180.0     # let the sniper dump play out first
ENTRY_WINDOW_S = 480.0       # no setup by minute 8 -> pass
FLUSH_MIN_PCT = 15.0         # require a real flush below baseline...
BOUNCE_CONFIRM_PCT = 5.0     # ...and a confirmed bounce off the low

# --- Position ----------------------------------------------------------------
SIZE_SOL = 0.25
TP_PCT = 15.0
SL_PCT = 10.0
HOLD_MAX_S = 600.0           # out by minute 10 after entry regardless
FILL_COST_PCT = 1.5          # per side: pool fee + slippage estimate
TX_FEE_SOL = 0.0007          # network base + priority fee per tx

# Friction asymmetry rules (2026-07-17, operator directive):
# - entries fill one poll AFTER the signal, at the moved price (a real buy
#   chases the bounce it just detected)
# - TP fills are CAPPED at the trigger price: you sell passing through
#   +TP_PCT, you don't get credited a 5s gap spike (NOLAN "+51.7%" was
#   this illusion)
# - SL fills stay at the observed (gapped-down) price — losses keep gap risk

# --- Ops ---------------------------------------------------------------------
POLL_S = 5.0
MAX_WATCHES = 3
MAX_FETCH_MISSES = 12        # ~1 min of consecutive API failures -> drop
DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Accept": "application/json"}


class TailHolder:
    """Owns the post-migration watches. The curve sniper calls
    on_graduation() for organic-cohort tokens; run() drives the watches."""

    def __init__(self, log_fn, state: dict, save_fn, pool):
        self._log = log_fn
        self.state = state
        self._save = save_fn
        self.pool = pool
        self.watches: dict[str, dict] = {}

    @property
    def active_count(self) -> int:
        return len(self.watches)

    # ---------------- intake (called by the sniper) ----------------
    def on_graduation(self, mint: str, symbol: str, features: dict):
        if mint in self.watches or len(self.watches) >= MAX_WATCHES:
            return
        self.watches[mint] = {
            "symbol": symbol, "t0": time.time(), "features": features,
            "baseline": None, "low": None, "misses": 0,
            "pos": None,  # {"entry_px","entry_ts","tokens"}
        }
        print(f"[TAIL] watching {symbol} ({mint[:8]}) post-migration "
              f"(steps={features.get('steps')} vel={features.get('velocity_5m')})",
              flush=True)

    # ---------------- price feed ----------------
    def _fetch_price(self, mint: str) -> float | None:
        req = urllib.request.Request(DEX_URL.format(mint=mint), headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        pairs = d.get("pairs") or []
        best = None
        for p in pairs:
            if p.get("dexId") == "pumpswap":
                best = p
                break
        if best is None and pairs:
            best = max(pairs, key=lambda p: (p.get("liquidity") or {})
                       .get("usd") or 0)
        if not best:
            return None
        try:
            px = float(best.get("priceNative") or 0)
        except (TypeError, ValueError):
            return None
        return px if px > 0 else None

    # ---------------- watch lifecycle ----------------
    def _drop(self, mint: str, reason: str, w: dict):
        base, low = w.get("baseline"), w.get("low")
        flush = (100.0 * (base - low) / base) if base and low else None
        self._log({"event": "tail_pass", "mint": mint, "symbol": w["symbol"],
                   "reason": reason,
                   "max_flush_pct": round(flush, 1) if flush else None,
                   "strategy": "graduation_tail"})
        del self.watches[mint]

    def _open(self, mint: str, w: dict, px: float, flush: float,
              bounce: float, chase_pct: float):
        entry_px = px * (1 + FILL_COST_PCT / 100.0)
        w["pos"] = {"entry_px": entry_px, "entry_ts": time.time(),
                    "tokens": SIZE_SOL / entry_px, "fees_sol": TX_FEE_SOL}
        self._log({"event": "tail_open", "mint": mint, "symbol": w["symbol"],
                   "size_sol": SIZE_SOL, "entry_px": entry_px,
                   "chase_pct": round(chase_pct, 2),
                   "flush_pct": round(flush, 1), "bounce_pct": round(bounce, 1),
                   "secs_after_grad": round(time.time() - w["t0"], 0),
                   "features": w["features"], "strategy": "graduation_tail"})
        print(f"[TAIL] OPEN {w['symbol']} after {flush:.0f}% flush / "
              f"{bounce:.1f}% bounce ({chase_pct:+.1f}% chased in flight), "
              f"size {SIZE_SOL} SOL", flush=True)

    def _close(self, mint: str, w: dict, px: float, reason: str):
        pos = w["pos"]
        exit_px = px * (1 - FILL_COST_PCT / 100.0)
        fees = pos.get("fees_sol", 0.0) + TX_FEE_SOL
        pnl = pos["tokens"] * exit_px - SIZE_SOL - fees
        net_pct = pnl / SIZE_SOL * 100.0
        tail = self.state.setdefault("tail", {"realized_sol": 0.0})
        tail["realized_sol"] = round(tail.get("realized_sol", 0.0) + pnl, 6)
        self._save()
        self._log({"event": "tail_close", "mint": mint, "symbol": w["symbol"],
                   "pnl_sol": round(pnl, 5), "net_pct": round(net_pct, 2),
                   "exit_reason": reason,
                   "hold_s": round(time.time() - pos["entry_ts"], 0),
                   "strategy": "graduation_tail"})
        print(f"[TAIL] CLOSE {w['symbol']} {reason} pnl={pnl:+.4f} SOL "
              f"({net_pct:+.1f}%) | tail balance "
              f"{tail['realized_sol']:+.4f} SOL", flush=True)
        del self.watches[mint]

    def _tick_one(self, mint: str, w: dict, px: float | None):
        now = time.time()
        if px is None:
            w["misses"] += 1
            if w["misses"] >= MAX_FETCH_MISSES:
                if w["pos"]:  # can't price it any more — flat-close honestly
                    self._close(mint, w, w["pos"]["entry_px"], "feed_lost")
                else:
                    self._drop(mint, "feed_lost", w)
            return
        w["misses"] = 0
        if w["baseline"] is None:
            w["baseline"] = w["low"] = px
            return
        w["low"] = min(w["low"], px)
        age = now - w["t0"]

        if w["pos"] is None:
            pe = w.pop("pending_entry", None)
            if pe is not None:
                # tx was in flight for one poll — fill at the moved price
                chase_pct = 100.0 * (px / pe["signal_px"] - 1.0)
                self._open(mint, w, px, pe["flush"], pe["bounce"], chase_pct)
                return
            flush = 100.0 * (w["baseline"] - w["low"]) / w["baseline"]
            bounce = 100.0 * (px - w["low"]) / w["low"] if w["low"] else 0.0
            if (age >= ENTRY_MIN_WAIT_S and flush >= FLUSH_MIN_PCT
                    and bounce >= BOUNCE_CONFIRM_PCT):
                w["pending_entry"] = {"signal_px": px, "flush": flush,
                                      "bounce": bounce}
            elif age > ENTRY_WINDOW_S:
                self._drop(mint, "no_setup", w)
        else:
            chg = 100.0 * (px - w["pos"]["entry_px"]) / w["pos"]["entry_px"]
            if chg >= TP_PCT:
                # sell on the way through the trigger — no gap-spike credit
                capped = w["pos"]["entry_px"] * (1 + TP_PCT / 100.0)
                self._close(mint, w, min(px, capped), "take_profit")
            elif chg <= -SL_PCT:
                self._close(mint, w, px, "stop_loss")
            elif now - w["pos"]["entry_ts"] > HOLD_MAX_S:
                self._close(mint, w, px, "timeout")

    # ---------------- driver ----------------
    async def run(self):
        loop = asyncio.get_running_loop()
        while True:
            for mint in list(self.watches.keys()):
                w = self.watches.get(mint)
                if not w:
                    continue
                try:
                    px = await loop.run_in_executor(
                        self.pool, self._fetch_price, mint)
                except Exception:
                    px = None
                try:
                    self._tick_one(mint, w, px)
                except Exception as e:
                    print(f"[TAIL] tick error {w['symbol']}: "
                          f"{type(e).__name__}: {str(e)[:80]}", flush=True)
            await asyncio.sleep(POLL_S)
