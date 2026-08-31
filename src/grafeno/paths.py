"""GRAFENO paths under the user's home (~/.grafeno).

The base directory can be overridden with the ``GRAFENO_HOME`` environment
variable (useful for tests).
"""

from __future__ import annotations

import hashlib
import os
import re
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


def references_path() -> Path:
    return home() / "references.toml"


def triggers_path() -> Path:
    return home() / "triggers.toml"


def telegram_state_path() -> Path:
    """Bot state file: last update offset + task_id -> chat_id mapping."""
    return home() / "telegram-state.toml"


def telegram_log_path() -> Path:
    """Bot activity log (received updates, decisions, errors)."""
    return home() / "telegram.log"


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


def media_dir(task_id: str) -> Path:
    """Directory with the images pasted into the task (description/requests)."""
    path = task_dir(task_id) / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mounts_dir() -> Path:
    """Local mount points for remote (sshfs) projects."""
    path = home() / "mounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def consoles_dir() -> Path:
    path = home() / "consoles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def consoles_path(workdir: Path | str) -> Path:
    """Per-project consoles file: ``~/.grafeno/consoles/<slug>-<hash8>.toml``.

    The slug comes from the directory name and the hash from the workdir
    string as given (same precedent as ``remote.mount_dir``): stable for the
    same project and collision-free across different ones.
    """
    text = str(workdir)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", Path(text).name).strip("-").lower() or "project"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return consoles_dir() / f"{slug}-{digest}.toml"
