"""Tests del formateo compacto de tokens."""

from __future__ import annotations

from grafeno.tokenfmt import format_tokens


def test_format_tokens():
    assert format_tokens(0) == "0"
    assert format_tokens(999) == "999"
    assert format_tokens(1200) == "1.2k"
    assert format_tokens(3_400_000) == "3.4M"
