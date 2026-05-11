"""Tests for detector.x_feed — the JSONL bridge between the standalone
x-monitor agent and the live scorer.

These tests avoid the singleton (which loads from a real path at import)
and instantiate XFeed directly against tmp_path fixtures.
"""
import json
import time

from detector.x_feed import XFeed


def _line(text: str, pump_link: str = "") -> str:
    return json.dumps({"text": text, "pump_link": pump_link}) + "\n"


class TestTickerExtraction:
    def test_dollar_ticker(self, tmp_path):
        log = tmp_path / "x.jsonl"
        log.write_text(_line("Buying $WIFE right now, fellas"))
        feed = XFeed(str(log))
        assert feed.has_hype_for({"symbol": "WIFE"})

    def test_bare_uppercase_word(self, tmp_path):
        log = tmp_path / "x.jsonl"
        log.write_text(_line("MOONROCKET is going parabolic"))
        feed = XFeed(str(log))
        assert feed.has_hype_for({"symbol": "MOONROCKET"})

    def test_short_ticker_ignored(self, tmp_path):
        """1-2 char extracts must NOT match — too many false positives
        (e.g. 'IS', 'OK', 'TO' in normal English text)."""
        log = tmp_path / "x.jsonl"
        log.write_text(_line("This is going to moon"))
        feed = XFeed(str(log))
        assert not feed.has_hype_for({"symbol": "IS"})
        assert not feed.has_hype_for({"symbol": "TO"})

    def test_lowercase_does_not_match(self, tmp_path):
        log = tmp_path / "x.jsonl"
        log.write_text(_line("pump.fun is great"))
        feed = XFeed(str(log))
        # 'pump' / 'fun' are lowercase — should not register as tickers.
        assert not feed.has_hype_for({"symbol": "PUMP"})


class TestMintExtraction:
    def test_mint_in_pump_link(self, tmp_path):
        log = tmp_path / "x.jsonl"
        mint = "AbCdEfGhJkLmNoPqRsTuVwXyZ23456789aBcDeFgH"
        log.write_text(_line(
            "Check this out",
            pump_link=f"https://pump.fun/coin/{mint}",
        ))
        feed = XFeed(str(log))
        assert feed.has_hype_for({"mint": mint})

    def test_mint_in_free_text(self, tmp_path):
        log = tmp_path / "x.jsonl"
        mint = "AbCdEfGhJkLmNoPqRsTuVwXyZ23456789aBcDeFgH"
        log.write_text(_line(f"see {mint} for the pump"))
        feed = XFeed(str(log))
        assert feed.has_hype_for({"mint": mint})


class TestMissingFile:
    def test_returns_false_when_file_absent(self, tmp_path):
        feed = XFeed(str(tmp_path / "nope.jsonl"))
        assert feed.has_hype_for({"symbol": "TEST"}) is False

    def test_stats_reports_missing(self, tmp_path):
        feed = XFeed(str(tmp_path / "nope.jsonl"))
        s = feed.stats()
        assert s["log_present"] is False
        assert s["tickers_tracked"] == 0


class TestIncrementalTail:
    """The feed should pick up new lines appended after the last poll
    without re-reading the file from the start every time."""

    def test_appended_line_is_seen(self, tmp_path, monkeypatch):
        # Bypass the 5s poll throttle so we can drive the file twice
        # in rapid succession.
        monkeypatch.setattr("detector.x_feed.POLL_MIN_INTERVAL", 0.0)

        log = tmp_path / "x.jsonl"
        log.write_text(_line("first $ALPHA tweet"))
        feed = XFeed(str(log))
        assert feed.has_hype_for({"symbol": "ALPHA"})
        assert not feed.has_hype_for({"symbol": "BRAVO"})

        with open(log, "a", encoding="utf-8") as f:
            f.write(_line("second $BRAVO tweet"))

        assert feed.has_hype_for({"symbol": "BRAVO"})
        assert feed.has_hype_for({"symbol": "ALPHA"})


class TestExpiry:
    """Tickers older than RECENT_WINDOW_SEC must age out."""

    def test_expired_ticker_no_longer_matches(self, tmp_path, monkeypatch):
        monkeypatch.setattr("detector.x_feed.POLL_MIN_INTERVAL", 0.0)
        monkeypatch.setattr("detector.x_feed.RECENT_WINDOW_SEC", 1)

        log = tmp_path / "x.jsonl"
        log.write_text(_line("$STALE going up"))
        feed = XFeed(str(log))
        assert feed.has_hype_for({"symbol": "STALE"})

        time.sleep(1.1)
        # Trigger another refresh (no new lines, just an expiry pass).
        assert not feed.has_hype_for({"symbol": "STALE"})


class TestRotationSafety:
    """If the JSONL is truncated/rotated, the offset must reset so we
    don't seek past EOF and miss every subsequent line."""

    def test_truncation_resets_offset(self, tmp_path, monkeypatch):
        monkeypatch.setattr("detector.x_feed.POLL_MIN_INTERVAL", 0.0)
        log = tmp_path / "x.jsonl"
        log.write_text(_line("$ORIG tweet that will be rotated away"))
        feed = XFeed(str(log))
        assert feed.has_hype_for({"symbol": "ORIG"})

        # Simulate logrotate / monitor restart with `>` redirect.
        log.write_text(_line("$NEW tweet"))
        assert feed.has_hype_for({"symbol": "NEW"})


class TestMalformedLines:
    def test_bad_json_does_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr("detector.x_feed.POLL_MIN_INTERVAL", 0.0)
        log = tmp_path / "x.jsonl"
        log.write_text("not json at all\n" + _line("$GOOD tweet"))
        feed = XFeed(str(log))
        assert feed.has_hype_for({"symbol": "GOOD"})
