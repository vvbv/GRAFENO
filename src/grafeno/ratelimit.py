"""Detection of CLI usage/quota exhaustion and retry-after hints.

Pure logic, no I/O: the driver base scans the error output of a failed run
with ``detect_usage_wait`` to decide whether the orchestrator should wait
and retry instead of failing the phase.
"""

from __future__ import annotations

import re

# Default wait between probes when the CLI does not say when quota resets.
PROBE_SECONDS = 60.0
# Maximum consecutive usage-limit retries before giving up (per phase run).
MAX_ATTEMPTS = 30
# Parsed waits are capped so a bogus "retry after 999999s" cannot stall a task.
MAX_WAIT_SECONDS = 3600.0

# Substrings (lowercased) that signal an exhausted usage/quota limit.
_USAGE_PATTERNS = (
    "rate limit",
    "ratelimit",
    "rate_limit",
    "too many requests",
    "429",
    "quota exceeded",
    "quota has been exceeded",
    "insufficient_quota",
    "usage limit",
    "usage_limit",
    "limit reached",
    "limit exceeded",
    "credits exhausted",
    "out of credits",
    "credit balance",
    "billing",
    "exceeded your current quota",
    "you have exhausted",
    "capacity",  # e.g. "model is at capacity"
)

# "retry after 45" / "retry-after: 45" / "retry in 45 seconds"
_RE_RETRY_AFTER = re.compile(
    r"retry[- ]after[:\s]+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)?",
    re.IGNORECASE,
)
# "try again in 2m" / "available again in 1 hour" / "wait 30 seconds"
_RE_AGAIN_IN = re.compile(
    r"(?:try again in|available(?: again)? in|resets? in|wait(?:ing)?)\s*"
    r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
}


def looks_like_usage_limit(text: str) -> bool:
    """True if ``text`` looks like a usage/quota exhaustion message."""
    lowered = text.lower()
    return any(pattern in lowered for pattern in _USAGE_PATTERNS)


def parse_wait_seconds(text: str) -> float | None:
    """Extract the suggested wait in seconds from an error message.

    Returns ``None`` when the message carries no usable time hint.
    The result is capped at ``MAX_WAIT_SECONDS``.
    """
    for pattern in (_RE_RETRY_AFTER, _RE_AGAIN_IN):
        match = pattern.search(text)
        if not match:
            continue
        value = float(match.group(1))
        unit = (match.group(2) or "s").lower()
        seconds = value * _UNIT_SECONDS.get(unit, 1.0)
        if seconds <= 0:
            return None
        return min(seconds, MAX_WAIT_SECONDS)
    return None


def detect_usage_wait(text: str) -> float | None:
    """Classify an error text: seconds to wait, ``0.0`` or ``None``.

    - ``None``: the text is NOT a usage-limit error (fail normally).
    - ``0.0``: usage limit detected but the CLI gave no time hint
      (the caller should probe with ``PROBE_SECONDS``).
    - ``> 0``: usage limit detected with an explicit wait hint.
    """
    if not looks_like_usage_limit(text):
        return None
    parsed = parse_wait_seconds(text)
    return parsed if parsed is not None else 0.0
