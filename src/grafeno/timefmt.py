"""Formateo de duraciones para logs y TUI."""

from __future__ import annotations


def format_duration(seconds: float) -> str:
    """Formatea una duración: `42s`, `3m 05s`, `1h 02m 03s`."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"
