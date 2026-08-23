"""Formateo compacto de conteos de tokens para la UI."""

from __future__ import annotations


def format_tokens(count: int) -> str:
    """Formatea un conteo de tokens de forma compacta (p.ej. 1.2k, 3.4M)."""
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1_000:.1f}k"
    return f"{count / 1_000_000:.1f}M"
