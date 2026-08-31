"""Project consoles: named shell sessions with a color, persisted per project.

Console definitions live in the project's ``.grafeno.toml`` under
``[[consoles]]``; each entry is a tab of the consoles screen (name, command
and color). Only definitions are persisted: processes are spawned when the
consoles screen is opened.
"""

from __future__ import annotations

import os
import subprocess
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


def external_terminal_command(workdir: str) -> list[str] | None:
    """Command that opens a new external terminal window in ``workdir``.

    Reuses the terminal detection of ``editor``; returns None when the
    terminal is unknown (best effort, like the editor opening).
    """
    # Lazy import: tests patch ``grafeno.editor.detect_terminal`` and a module
    # level import would defeat that patch. Keeps the domain module decoupled
    # from the editor module at import time too.
    from .editor import detect_terminal

    terminal = detect_terminal()
    if not terminal.window_command:
        return None
    if terminal.name == "tmux":
        # tmux runs inside a server: cwd must be passed explicitly with -c
        # because the new-window does not inherit the parent's cwd.
        return terminal.window_command + ["-c", workdir]
    if terminal.name == "terminal.app":
        # Terminal.app knows how to open a folder in a new window directly.
        return ["open", "-a", "Terminal", workdir]
    # Ghostty/wezterm/kitty/alacritty/iterm: append the shell so the new
    # window has something to run; cwd is inherited from ``Popen(cwd=...)``.
    return terminal.window_command + [default_shell()]


def open_external_terminal(workdir: str) -> bool:
    """Open a new terminal window with a shell in ``workdir`` (best effort)."""
    command = external_terminal_command(workdir)
    if command is None:
        return False
    try:
        subprocess.Popen(
            command,
            cwd=workdir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True
