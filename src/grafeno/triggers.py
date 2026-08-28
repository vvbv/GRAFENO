"""Trigger tasks: task templates fired at pipeline phase boundaries.

Two levels: global (``~/.grafeno/triggers.toml``) and project
(``<workdir>/.grafeno.toml``, ``[[triggers]]`` section). Each trigger
declares the stages it listens to (a specific one or all of them) and
whether it fires ``before`` or ``after`` the stage. When it fires, a new
independent GRAFENO task is created (automode, scheduled for now), so the
scheduler tick of the App starts it unattended: triggers never block nor
interfere with the task that fired them.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _toml, paths
from .config import PROJECT_CONFIG_FILE
from .pipeline.hooks import HOOK_STAGES

if TYPE_CHECKING:
    from .models import Task

TRIGGER_STAGES = HOOK_STAGES  # plan, implement, review, fix, final, tests
TIMINGS = ("before", "after")
ALL_PHASES = "all"  # value of ``phases`` meaning "every stage"
ORIGIN_TRIGGER = "trigger"  # Task.origin of tasks spawned by a trigger


@dataclass
class Trigger:
    """Template of a task fired at a phase boundary."""

    name: str
    description: str = ""
    phases: str = ALL_PHASES  # "all" or comma-separated TRIGGER_STAGES
    timing: str = "after"     # "before" | "after"
    workdir: str = ""         # empty = same workdir as the firing task

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "phases": self.phases,
            "timing": self.timing,
            "workdir": self.workdir,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Trigger":
        timing = str(data.get("timing", "after"))
        return cls(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            phases=str(data.get("phases", ALL_PHASES)) or ALL_PHASES,
            timing=timing if timing in TIMINGS else "after",
            workdir=str(data.get("workdir", "")),
        )


def parse_phases(value: str) -> list[str]:
    """Normalize a phases value: ``all`` or a comma-separated stage list."""
    if value.strip() == ALL_PHASES:
        return list(TRIGGER_STAGES)
    chosen = {part.strip() for part in value.split(",") if part.strip()}
    return [stage for stage in TRIGGER_STAGES if stage in chosen]


def matches(trigger: Trigger, stage: str, timing: str) -> bool:
    """True if the trigger listens to this stage and timing."""
    return trigger.timing == timing and stage in parse_phases(trigger.phases)


def load_global() -> list[Trigger]:
    """Global triggers from ``~/.grafeno/triggers.toml`` (missing = [])."""
    path = paths.triggers_path()
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return _parse_list(data)


def save_global(triggers: list[Trigger]) -> None:
    """Write the global triggers file."""
    payload = {"triggers": [trigger.to_dict() for trigger in triggers]}
    paths.triggers_path().write_text(_toml.dumps(payload), encoding="utf-8")


def load_project(workdir: Path) -> list[Trigger]:
    """Project triggers from ``<workdir>/.grafeno.toml`` (missing = [])."""
    path = workdir / PROJECT_CONFIG_FILE
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return _parse_list(data)


def _parse_list(data: dict[str, Any]) -> list[Trigger]:
    """Extract the ``triggers`` array-of-tables from parsed TOML data."""
    raw = data.get("triggers", [])
    if not isinstance(raw, list):
        return []
    return [Trigger.from_dict(item) for item in raw if isinstance(item, dict)]


def resolve(workdir: Path) -> list[Trigger]:
    """Effective triggers for a workdir: global first, then project."""
    return load_global() + load_project(workdir)


def spawn(trigger: Trigger, task: "Task") -> "Task":
    """Create the independent task described by the trigger.

    The new task runs in automode and is scheduled for "now", so the App
    scheduler tick starts it unattended. ``origin`` marks it so it does not
    fire further triggers itself (no trigger recursion).
    """
    from . import config as config_module
    from . import models

    spawned = models.Task.create(
        trigger.name,
        trigger.description,
        trigger.workdir.strip() or task.workdir,
        config_module.load(),
        automode=True,
        confirm_plan=False,
        scheduled_at=datetime.now().isoformat(timespec="minutes"),
    )
    spawned.origin = ORIGIN_TRIGGER
    models.save(spawned)
    return spawned


def fire(task: "Task", stage: str, timing: str, on_info=lambda message: None) -> int:
    """Spawn the tasks of every trigger matching ``stage``+``timing``.

    Best effort and total: a task with ``origin == "trigger"`` never fires
    triggers, and any internal error is only reported via ``on_info``.
    Returns the number of spawned tasks.
    """
    from . import remote
    from .i18n import t

    if task.origin == ORIGIN_TRIGGER:
        return 0
    spawned = 0
    project_workdir = remote.effective_workdir(task.remote, task.workdir)
    for trigger in resolve(project_workdir):
        if not trigger.name.strip() or not matches(trigger, stage, timing):
            continue
        try:
            new_task = spawn(trigger, task)
        except Exception as exc:  # noqa: BLE001 - triggers never break the pipeline
            on_info(t("trig.error", name=trigger.name, error=exc))
            continue
        spawned += 1
        on_info(
            t(
                "trig.fired",
                name=trigger.name,
                stage=t(f"phase.{stage}"),
                timing=t(f"trig.timing.{timing}"),
                task=new_task.name,
            )
        )
    return spawned
