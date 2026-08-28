"""Tests for usage-limit detection (grafeno.ratelimit)."""

from __future__ import annotations

from grafeno.ratelimit import (
    MAX_WAIT_SECONDS,
    detect_usage_wait,
    looks_like_usage_limit,
    parse_wait_seconds,
)


def test_detects_common_usage_limit_messages():
    assert looks_like_usage_limit("Error: rate limit reached for model")
    assert looks_like_usage_limit("HTTP 429 Too Many Requests")
    assert looks_like_usage_limit("insufficient_quota: you exceeded your current quota")
    assert looks_like_usage_limit("Usage limit reached. Try again later.")


def test_ignores_unrelated_errors():
    assert not looks_like_usage_limit("syntax error on line 3")
    assert not looks_like_usage_limit("permission denied")
    assert detect_usage_wait("connection refused") is None


def test_parses_retry_after_seconds():
    assert parse_wait_seconds("Rate limit. Retry after 45 seconds") == 45.0
    assert parse_wait_seconds("retry-after: 30") == 30.0


def test_parses_try_again_in_minutes_and_hours():
    assert parse_wait_seconds("Please try again in 2m") == 120.0
    assert parse_wait_seconds("quota resets in 1 hour") == 3600.0


def test_wait_is_capped():
    assert parse_wait_seconds("retry after 999999 seconds") == MAX_WAIT_SECONDS


def test_detect_usage_wait_returns_zero_without_hint():
    assert detect_usage_wait("429 Too Many Requests") == 0.0


def test_detect_usage_wait_returns_hint():
    assert detect_usage_wait("rate limit, try again in 90 seconds") == 90.0


def test_detects_session_limit_message():
    message = "You've hit your session limit - resets 2:30pm (America/Bogota)"
    assert looks_like_usage_limit(message)
    wait = detect_usage_wait(message)
    assert wait is not None and 0 < wait <= MAX_WAIT_SECONDS


def test_session_limit_without_time_hint_probes():
    assert detect_usage_wait("Error: session limit reached, try later") == 0.0


def test_session_word_alone_is_not_a_limit():
    assert not looks_like_usage_limit("session started successfully")


def test_seconds_until_clock_same_day():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from grafeno.ratelimit import _seconds_until_clock

    now = datetime(2026, 8, 28, 14, 0, tzinfo=ZoneInfo("America/Bogota"))
    assert _seconds_until_clock(2, 30, "p", "America/Bogota", now) == 1800.0


def test_seconds_until_clock_rolls_to_next_day():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from grafeno.ratelimit import _seconds_until_clock

    now = datetime(2026, 8, 28, 23, 30, tzinfo=ZoneInfo("America/Bogota"))
    assert _seconds_until_clock(12, 15, "a", "America/Bogota", now) == 2700.0


def test_seconds_until_clock_is_capped():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from grafeno.ratelimit import _seconds_until_clock

    now = datetime(2026, 8, 28, 8, 0, tzinfo=ZoneInfo("America/Bogota"))
    assert _seconds_until_clock(9, 30, "p", "America/Bogota", now) == MAX_WAIT_SECONDS


def test_seconds_until_clock_unknown_timezone_returns_none():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from grafeno.ratelimit import _seconds_until_clock

    now = datetime(2026, 8, 28, 14, 0, tzinfo=ZoneInfo("America/Bogota"))
    assert _seconds_until_clock(2, 30, "p", "Marte/Olimpo", now) is None
    assert detect_usage_wait("session limit - resets 2:30pm (Marte/Olimpo)") == 0.0


def test_seconds_until_clock_12am_and_12pm():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from grafeno.ratelimit import _seconds_until_clock

    now = datetime(2026, 8, 28, 0, 0, tzinfo=ZoneInfo("America/Bogota"))
    assert _seconds_until_clock(12, 30, "a", "America/Bogota", now) == 1800.0
    # 12:30pm from 00:00 is 12.5h = 45000s, but the helper caps at MAX_WAIT_SECONDS.
    assert _seconds_until_clock(12, 30, "p", "America/Bogota", now) == MAX_WAIT_SECONDS


def test_parses_wall_clock_reset_24h_without_timezone():
    wait = parse_wait_seconds("quota exceeded; resets 23:59")
    assert wait is not None and 0 < wait <= MAX_WAIT_SECONDS
