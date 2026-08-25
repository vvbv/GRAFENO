"""Context references: named resources (local directories or URLs) that tasks
can attach as inspiration/context for the pipeline agents.

Three levels: global (``~/.grafeno/references.toml``), project
(``<workdir>/.grafeno.toml``, ``[[references]]`` section) and per-task (stored
in ``task.toml``). A task may exclude the global and/or project level.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _toml, paths
from .config import PROJECT_CONFIG_FILE

if TYPE_CHECKING:
    from .models import Task


@dataclass
class Reference:
    """A named context resource: local directory path or URL."""

    name: str
    description: str = ""
    path: str = ""  # local directory or URL

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "path": self.path}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Reference":
        return cls(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            path=str(data.get("path", "")),
        )


def load_global() -> list[Reference]:
    """Global references from ``~/.grafeno/references.toml`` (missing = [])."""
    path = paths.references_path()
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return _parse_list(data)


def save_global(references: list[Reference]) -> None:
    """Write the global references file (empty list removes the file body)."""
    payload = {"references": [ref.to_dict() for ref in references]}
    paths.references_path().write_text(_toml.dumps(payload), encoding="utf-8")


def load_project(workdir: Path) -> list[Reference]:
    """Project references from ``<workdir>/.grafeno.toml`` (missing = []).

    Same tolerant pattern as ``config._project_overrides``: any read/parse
    error returns an empty list.
    """
    path = workdir / PROJECT_CONFIG_FILE
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return _parse_list(data)


def _parse_list(data: dict[str, Any]) -> list[Reference]:
    """Extract the ``references`` array-of-tables from parsed TOML data."""
    raw = data.get("references", [])
    if not isinstance(raw, list):
        return []
    return [Reference.from_dict(item) for item in raw if isinstance(item, dict)]


def resolve(task: "Task") -> list[Reference]:
    """Effective references of a task: global + project + own, honoring the
    per-task exclusion flags. Order: global, project, task."""
    result: list[Reference] = []
    if task.use_global_references:
        result.extend(load_global())
    if task.use_project_references:
        result.extend(load_project(Path(task.workdir)))
    result.extend(task.references)
    return result
