"""Project consoles: named shell sessions with a color, persisted per project.

Console definitions live in the project's ``.grafeno.toml`` under
``[[consoles]]``; each entry is a tab of the consoles screen (name, command
and color). Only definitions are persisted: processes are spawned when the
consoles screen is opened.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _toml
from .config import PROJECT_CONFIG_FILE

# Selectable console colors ("" = default theme colors). Values are plain
# Textual color names, applied as tab background and frame border.
CONSOLE_COLORS = ("", "red", "green", "yellow", "blue", "magenta", "cyan")


@dataclass
class ConsoleSpec:
    """Definition of one console tab of a project."""

    name: str
    command: str = ""  # empty = default user shell
    color: str = ""    # empty = default; otherwise one of CONSOLE_COLORS

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "command": self.command, "color": self.color}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConsoleSpec":
        color = str(data.get("color", ""))
        return cls(
            name=str(data.get("name", "")),
            command=str(data.get("command", "")),
            color=color if color in CONSOLE_COLORS else "",
        )


def supported() -> bool:
    """True where a pseudo-terminal can be opened (POSIX); False on Windows."""
    return os.name == "posix"


def default_shell() -> str:
    """User shell on POSIX (``$SHELL`` with ``/bin/sh`` fallback)."""
    return os.environ.get("SHELL") or "/bin/sh"


def load_project(workdir: Path) -> list[ConsoleSpec]:
    """Project consoles from ``<workdir>/.grafeno.toml`` (missing = []).

    Same tolerant pattern as ``references.load_project``: any read/parse
    error returns an empty list.
    """
    path = workdir / PROJECT_CONFIG_FILE
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return _parse_list(data)


def _parse_list(data: dict[str, Any]) -> list[ConsoleSpec]:
    """Extract the ``consoles`` array-of-tables from parsed TOML data."""
    raw = data.get("consoles", [])
    if not isinstance(raw, list):
        return []
    return [ConsoleSpec.from_dict(item) for item in raw if isinstance(item, dict)]


def save_project(workdir: Path, specs: list[ConsoleSpec]) -> None:
    """Write the ``[[consoles]]`` section, preserving every other section of
    the project's ``.grafeno.toml`` (``[editor]``, ``[[references]]``...)."""
    path = workdir / PROJECT_CONFIG_FILE
    data: dict[str, Any] = {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    data["consoles"] = [spec.to_dict() for spec in specs]
    path.write_text(_toml.dumps(_safe(data)), encoding="utf-8")


def _safe(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only values the mini TOML serializer supports (root scalars,
    tables and arrays of tables), so a hand-edited exotic value cannot make
    saving fail."""
    safe: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool, dict)):
            safe[key] = value
        elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
            safe[key] = value
    return safe
