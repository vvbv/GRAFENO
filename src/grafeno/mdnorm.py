"""Markdown normalization: compact blank lines and tighten loose lists."""

from __future__ import annotations

import re

_LIST_ITEM_RE = re.compile(r"^(?P<indent> {0,12})(?:[-*+]|\d{1,9}[.)])[ \t]+")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _list_kind(line: str) -> str:
    """Kind of list item: 'ul' (bullet) or 'ol' (ordered)."""
    return "ol" if line.lstrip(" ")[0].isdigit() else "ul"


def normalize_markdown(text: str) -> str:
    """Return a compact version of a Markdown document.

    - Line endings are normalized to LF.
    - Runs of 2+ blank lines collapse into a single blank line.
    - Blank lines between consecutive list items of the same kind and
      indentation are removed (loose lists become tight).
    - Fenced code blocks (``` or ~~~) are preserved verbatim.
    - Trailing blank lines are removed; a non-empty result ends with "\n".

    The function is pure and idempotent.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    in_fence = False
    fence_char = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if in_fence:
            out.append(line)
            if fence and fence.group(1)[0] == fence_char:
                in_fence = False
            i += 1
            continue
        if fence:
            in_fence = True
            fence_char = fence.group(1)[0]
            out.append(line)
            i += 1
            continue
        if line.strip() == "":
            j = i
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            prev = out[-1] if out else ""
            nxt = lines[j] if j < len(lines) else ""
            keep_blank = True
            if prev and nxt:
                prev_item = _LIST_ITEM_RE.match(prev)
                next_item = _LIST_ITEM_RE.match(nxt)
                if (
                    prev_item
                    and next_item
                    and prev_item.group("indent") == next_item.group("indent")
                    and _list_kind(prev) == _list_kind(nxt)
                ):
                    keep_blank = False  # tighten the loose list
            if keep_blank and prev and nxt:
                out.append("")  # a single blank line between blocks
            i = j
            continue
        out.append(line)
        i += 1
    result = "\n".join(out).rstrip("\n")
    return result + "\n" if result else ""
