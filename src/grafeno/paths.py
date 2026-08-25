"""GRAFENO paths under the user's home (~/.grafeno).

The base directory can be overridden with the ``GRAFENO_HOME`` environment
variable (useful for tests).
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "GRAFENO_HOME"


def home() -> Path:
    """GRAFENO base directory (defaults to ``~/.grafeno``)."""
    override = os.environ.get(ENV_HOME)
    base = Path(override) if override else Path.home() / ".grafeno"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    return home() / "config.toml"


def tasks_dir() -> Path:
    path = home() / "tasks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def task_dir(task_id: str) -> Path:
    return tasks_dir() / task_id


def task_meta_path(task_id: str) -> Path:
    return task_dir(task_id) / "task.toml"


def plan_dir(task_id: str, cycle: int = 1) -> Path:
    """Plan directory for a cycle. Cycle 1 uses the root (backwards
    compatibility); extension cycles use ``plan/ciclo-NN/``."""
    path = task_dir(task_id) / "plan"
    if cycle > 1:
        path = path / f"ciclo-{cycle:02d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def review_dir(task_id: str, cycle: int = 1) -> Path:
    path = task_dir(task_id) / "review"
    if cycle > 1:
        path = path / f"ciclo-{cycle:02d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def final_dir(task_id: str, cycle: int = 1) -> Path:
    path = task_dir(task_id) / "final"
    if cycle > 1:
        path = path / f"ciclo-{cycle:02d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir(task_id: str) -> Path:
    path = task_dir(task_id) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
