"""Rutas de GRAFENO en el home del usuario (~/.grafeno).

El directorio base puede sobreescribirse con la variable de entorno
``GRAFENO_HOME`` (útil para tests).
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "GRAFENO_HOME"


def home() -> Path:
    """Directorio base de GRAFENO (por defecto ``~/.grafeno``)."""
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


def plan_dir(task_id: str) -> Path:
    path = task_dir(task_id) / "plan"
    path.mkdir(parents=True, exist_ok=True)
    return path


def review_dir(task_id: str) -> Path:
    path = task_dir(task_id) / "review"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir(task_id: str) -> Path:
    path = task_dir(task_id) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
