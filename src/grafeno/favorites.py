"""Favorite projects of the web panel (``~/.grafeno/project-favorites.toml``).

Stored on the server so every browser of the instance shares them. Entries
are normalized workdirs (same form as ``server.actions._norm_dir``). Best
effort: a missing or unreadable file means no favorites.
"""

from __future__ import annotations

import os
import tomllib
from typing import Any

from . import _toml, paths


def normalize(workdir: str) -> str:
    """Comparable form of a workdir (expanded, normalized, no trailing slash)."""
    return os.path.normpath(os.path.expanduser(workdir.strip()))


def load() -> list[str]:
    """Saved favorites in insertion order (missing or unreadable file = [])."""
    path = paths.project_favorites_path()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    raw = data.get("favorites", [])
    return _clean(raw) if isinstance(raw, list) else []


def save(favorites: list[str]) -> None:
    """Write the favorites file (normalized, without empties or duplicates)."""
    payload = {"favorites": _clean(favorites)}
    path = paths.project_favorites_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_toml.dumps(payload), encoding="utf-8")


def set_favorite(workdir: str, favorite: bool) -> list[str]:
    """Mark or unmark ``workdir``; returns the saved list."""
    key = normalize(workdir)
    current = [item for item in load() if item != key]
    if favorite:
        current.append(key)
    save(current)
    return _clean(current)


def _clean(values: list[Any]) -> list[str]:
    """Normalized non-empty strings, first occurrence kept."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        key = normalize(value)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result
