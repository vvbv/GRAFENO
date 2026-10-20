"""Duration formatting for logs and TUI."""

from __future__ import annotations


def format_duration(seconds: float) -> str:
    """Format a duration as ``42s``, ``3m 05s``, ``1h 02m 03s``."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_countdown(seconds: float) -> str:
    """Coarse distance for schedules: ``45m``, ``6h 05m``, ``2d 03h``."""
    total = max(0, int(seconds)) // 60
    days, rem = divmod(total, 1440)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"
