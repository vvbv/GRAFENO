"""Root workspaces (working directories): the catalogue of known projects.

A workspace is a root folder whose first-level subfolders are considered
projects even when no GRAFENO task exists for them. Two levels: global
(``Config.workspaces`` in ``~/.grafeno/config.toml``) and per project
(``workspaces`` array in ``<workdir>/.grafeno.toml``), merged by
``resolve``. Workspaces never restrict where a task runs: they only list
the projects (TUI projects screen, web Projects tab, Telegram bot). All
functions are best-effort and never raise on missing folders or
malformed files.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import PROJECT_CONFIG_FILE


@dataclass(frozen=True)
class Root:
    """A configured working directory and its state on disk."""

    entry: str    # as configured (e.g. "~/code")
    path: str     # expanded + resolved absolute path ("" when unresolvable)
    exists: bool  # True when ``path`` is an existing directory


@dataclass(frozen=True)
class Project:
    """A project: a first-level subfolder of a working directory."""

    name: str        # folder name
    path: Path       # folder path (``workspace / name``)
    workspace: Path  # working directory where it was found


def load_project(workdir: Path) -> list[str]:
    """Workspace entries from ``<workdir>/.grafeno.toml`` (missing/invalid = [])."""
    path = workdir / PROJECT_CONFIG_FILE
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    raw = data.get("workspaces", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str)]


def entry_key(entry: str) -> str:
    """Comparison key of a configured entry: expanded and normalized
    (case-folded on case-insensitive systems)."""
    return os.path.normcase(os.path.normpath(os.path.expanduser(entry.strip())))


def normalize_entries(entries: Iterable[str]) -> list[str]:
    """Clean a list of configured entries, keeping their order.

    Each entry is stripped and normalized with ``os.path.normpath`` (no
    trailing separator; ``~`` is kept unexpanded); empty entries and
    duplicates (same ``entry_key``) are dropped.
    """
    result: list[str] = []
    seen: set[str] = set()
    for raw in entries:
        text = str(raw).strip()
        if not text:
            continue
        entry = os.path.normpath(text)
        key = entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def effective_entries(global_workspaces: list[str], workdir: Path | None = None) -> list[str]:
    """Configured entries: the global ones first, then the project level of ``workdir``."""
    entries = list(global_workspaces)
    if workdir is not None:
        entries.extend(load_project(workdir))
    return entries


def roots(entries: Iterable[str]) -> list[Root]:
    """State of each configured entry, in order.

    Entries are expanded (``~``) and resolved; empty entries and duplicates
    (same resolved path) are dropped. Never raises.
    """
    result: list[Root] = []
    seen: set[str] = set()
    for raw in entries:
        text = str(raw).strip()
        if not text:
            continue
        try:
            path = Path(text).expanduser().resolve()
        except (OSError, RuntimeError):  # unknown ~user, symlink loop...
            result.append(Root(entry=text, path="", exists=False))
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(Root(entry=text, path=key, exists=path.is_dir()))
    return result


def resolve(global_workspaces: list[str], workdir: Path | None = None) -> list[Path]:
    """Effective workspaces: global + project level, expanded and deduped.

    Entries use ``expanduser``+``resolve``; non-existing or non-directory
    entries are dropped. Order: global first, project entries after.
    """
    return [
        Path(root.path)
        for root in roots(effective_entries(global_workspaces, workdir))
        if root.exists
    ]


def projects(workspaces: list[Path]) -> list[Project]:
    """Projects of each workspace: its first-level subdirectories.

    Workspaces keep their order; inside each one the folders are sorted by
    name, case-insensitive. Hidden folders (dot-prefixed) and
    non-directories are skipped; a folder reachable from two workspaces
    appears only once, under the first one.
    """
    result: list[Project] = []
    seen: set[Path] = set()
    for workspace in workspaces:
        try:
            children = sorted(
                workspace.iterdir(), key=lambda child: (child.name.casefold(), child.name)
            )
        except OSError:
            continue
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                if not child.is_dir():
                    continue
                key = child.resolve()
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            result.append(Project(name=child.name, path=child, workspace=workspace))
    return result


def discover(workspaces: list[Path]) -> list[Path]:
    """First-level subdirectories of each workspace, sorted and deduped.

    Same rules as ``projects`` (only the folder paths).
    """
    return [project.path for project in projects(workspaces)]
