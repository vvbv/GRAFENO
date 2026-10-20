"""Tests of the timefmt module."""

from __future__ import annotations

from grafeno.timefmt import format_countdown


def test_format_countdown():
    assert format_countdown(0) == "0m"
    assert format_countdown(59) == "0m"
    assert format_countdown(65 * 60) == "1h 05m"
    assert format_countdown(26 * 3600 + 180) == "1d 02h"
    assert format_countdown(-500) == "0m"
