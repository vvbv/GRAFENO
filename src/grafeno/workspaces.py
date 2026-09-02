"""Root workspaces: discovery of projects that have no tasks yet.

A workspace is a root folder whose first-level subfolders are considered
projects even when no GRAFENO task exists for them. Two levels: global
(``Config.workspaces`` in ``~/.grafeno/config.toml``) and per project
(``workspaces`` array in ``<workdir>/.grafeno.toml``), merged by
``resolve``. All functions are best-effort and never raise on missing
folders or malformed files.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .config import PROJECT_CONFIG_FILE


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


def resolve(global_workspaces: list[str], workdir: Path | None = None) -> list[Path]:
    """Effective workspaces: global + project level, expanded and deduped.

    Entries use ``expanduser``+``resolve``; non-existing or non-directory
    entries are dropped. Order: global first, project entries after.
    """
    entries = list(global_workspaces)
    if workdir is not None:
        entries.extend(load_project(workdir))
    result: list[Path] = []
    seen: set[Path] = set()
    for entry in entries:
        try:
            path = Path(entry).expanduser().resolve()
        except OSError:
            continue
        if path in seen or not path.is_dir():
            continue
        seen.add(path)
        result.append(path)
    return result


def discover(workspaces: list[Path]) -> list[Path]:
    """First-level subdirectories of each workspace, sorted and deduped.

    Hidden folders (dot-prefixed) and non-directories are skipped; a
    folder reachable from two workspaces appears only once.
    """
    result: list[Path] = []
    seen: set[Path] = set()
    for workspace in workspaces:
        try:
            children = sorted(workspace.iterdir())
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
            result.append(child)
    return result
