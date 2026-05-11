"""
detector/x_feed.py

Bridges the standalone `x-monitor/pump_fun_x_monitor.py` agent's JSONL
output into the live scorer. The X monitor runs as a separate process
(see x-monitor/README.md) and appends pump-related tweets to a JSONL
log. This module tails that log and exposes a cheap in-memory lookup:

    x_feed.has_hype_for(token) -> bool

The scorer calls it during enrichment; if it returns True the token
gets `x_hype_match = True`, which the four-factor scorer reads as a
community signal AND the fusion engine reads as the X half of the
"X-mention + on-chain accumulation" alignment.

Why not stream directly from X here?
-----------------------------------
Rate limits and complexity. The X monitor already handles auth, query
construction, dedup, and rate-limit backoff. Tail-reading its JSONL
keeps the bot process simple, avoids a second X API key requirement,
and lets the operator run the monitor on a different machine entirely
(JSONL over a shared volume / synced file).

Matching strategy
-----------------
- Treat each line as a tweet with optional `pump_link` and free-text
  `text`.
- Index tickers and mints we've recently seen in tweets within
  `RECENT_WINDOW_SEC` (default 1 hour). Older entries age out.
- `has_hype_for(token)` checks the token's symbol AND mint against the
  index. Match is case-insensitive on symbol; mint match is exact
  substring (mints are unique).
- Symbol noise filter: 1- or 2-char symbols are ignored (too many
  false-positive substring matches like 'IS', 'TO', 'OK').
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from loguru import logger

# Default location matches x-monitor/pump_fun_x_monitor.py LOG_FILE_DEFAULT.
# Operator can override via env var if their monitor writes elsewhere.
DEFAULT_LOG_PATH   = os.environ.get(
    "X_MONITOR_LOG_FILE",
    "x-monitor/pump_fun_pumps.jsonl",
)
RECENT_WINDOW_SEC  = int(os.environ.get("X_HYPE_WINDOW_SEC", 3600))
POLL_MIN_INTERVAL  = 5.0   # seconds between disk reads — cheap cache
MIN_TICKER_LEN     = 3     # avoid false-positive 1-2 char hits

# Pull $TICKER or bare-word TICKER patterns from tweet text. Conservative
# to keep noise out — must be uppercase, 3-10 chars, alphanumeric.
_TICKER_RE = re.compile(r"\$?\b([A-Z][A-Z0-9]{2,9})\b")

# Mints are 32-44 base58 chars. Coarse but specific enough.
_MINT_RE = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")


class XFeed:
    """In-memory tail of the X monitor's JSONL output.

    Thread-safety: not thread-safe. The scorer is single-task per token
    inside an asyncio loop; concurrent calls don't corrupt the dicts
    (writes are atomic at dict level), but the file read serializes
    naturally via _last_poll.
    """

    def __init__(self, log_path: str = DEFAULT_LOG_PATH):
        self.log_path = log_path
        self._tickers: dict[str, float]  = {}   # TICKER -> last_seen_ts
        self._mints:   dict[str, float]  = {}   # mint   -> last_seen_ts
        self._last_offset = 0
        self._last_poll   = 0.0
        self._missing_logged = False

    def _maybe_refresh(self):
        now = time.time()
        if now - self._last_poll < POLL_MIN_INTERVAL:
            return
        self._last_poll = now

        path = Path(self.log_path)
        if not path.exists():
            if not self._missing_logged:
                logger.debug(
                    f"[X-FEED] {self.log_path} not present yet — running without X hype data"
                )
                self._missing_logged = True
            return

        try:
            size = path.stat().st_size
        except OSError:
            return

        # File got truncated/rotated — start over.
        if size < self._last_offset:
            self._last_offset = 0

        if size == self._last_offset:
            self._expire(now)
            return

        try:
            with open(path, encoding="utf-8") as f:
                f.seek(self._last_offset)
                for line in f:
                    self._ingest_line(line, now)
                self._last_offset = f.tell()
        except OSError as e:
            logger.debug(f"[X-FEED] read error: {e}")

        self._expire(now)

    def _ingest_line(self, line: str, now: float):
        line = line.strip()
        if not line:
            return
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return
        text = (rec.get("text") or "") + " " + (rec.get("pump_link") or "")
        if not text.strip():
            return

        # Tickers — uppercase 3-10 char alphanumeric words.
        for m in _TICKER_RE.finditer(text):
            tk = m.group(1).upper()
            if len(tk) >= MIN_TICKER_LEN:
                self._tickers[tk] = now

        # Mints — base58 32-44 chars. pump_link often contains the mint as
        # part of the URL path (https://pump.fun/coin/<mint>).
        for m in _MINT_RE.finditer(text):
            mint = m.group(1)
            self._mints[mint] = now

    def _expire(self, now: float):
        cutoff = now - RECENT_WINDOW_SEC
        # Build new dicts rather than mutating during iteration.
        self._tickers = {k: ts for k, ts in self._tickers.items() if ts >= cutoff}
        self._mints   = {k: ts for k, ts in self._mints.items()   if ts >= cutoff}

    # ── Public API ────────────────────────────────────────────────────────

    def has_hype_for(self, token: dict) -> bool:
        """True if the token's symbol or mint appeared in a recent tweet."""
        self._maybe_refresh()
        if not self._tickers and not self._mints:
            return False

        symbol = (token.get("symbol") or "").strip().upper()
        mint   = (token.get("mint")   or "").strip()

        if mint and mint in self._mints:
            return True
        if symbol and len(symbol) >= MIN_TICKER_LEN and symbol in self._tickers:
            return True
        return False

    def last_seen(self, symbol: str) -> float | None:
        """Diagnostic: when (epoch sec) did we last see this ticker."""
        self._maybe_refresh()
        return self._tickers.get((symbol or "").strip().upper())

    def stats(self) -> dict:
        self._maybe_refresh()
        return {
            "log_path":          self.log_path,
            "tickers_tracked":   len(self._tickers),
            "mints_tracked":     len(self._mints),
            "window_sec":        RECENT_WINDOW_SEC,
            "log_present":       Path(self.log_path).exists(),
        }


# Singleton — imported by signal_scorer.
x_feed = XFeed()
