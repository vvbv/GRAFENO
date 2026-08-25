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
