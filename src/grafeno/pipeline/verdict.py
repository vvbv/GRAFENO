"""Parsing of the structured reviewer verdict."""

from __future__ import annotations

import re
from enum import Enum


class Verdict(Enum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


_PATTERN = re.compile(r"VERDICT:\s*\*{0,2}\s*(APPROVED|CHANGES_REQUESTED)", re.IGNORECASE)


def parse_verdict(text: str) -> Verdict | None:
    """Search for the verdict from the end of the text (tolerates markdown `**`)."""
    matches = _PATTERN.findall(text or "")
    if not matches:
        return None
    return Verdict(matches[-1].upper())
