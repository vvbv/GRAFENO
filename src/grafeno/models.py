"""Modelo de datos de una tarea GRAFENO y su máquina de estados."""

from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from . import _toml, paths
from .config import Config, RoleConfig
from .i18n import t


class TaskState(str, Enum):
    DRAFT = "draft"
    PLANNING = "planning"
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    IMPLEMENTED = "implemented"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    FINALIZING = "finalizing"
    DONE = "done"
    FAILED = "failed"
    PAUSED = "paused"


def state_label(state: "TaskState") -> str:
    """Etiqueta localizada de un estado de tarea."""
    return t(f"state.{state.value}")


# Fases visibles en la barra de progreso del detalle.
PHASES = ("plan", "implement", "review", "final", "done")


def slugify(text: str, *, max_length: int = 40) -> str:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return (slug[:max_length].strip("-") or "tarea")


def new_task_id(name: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{slugify(name)}"


@dataclass
class Task:
    id: str
    name: str
    description: str = ""
    workdir: str = "."
    state: TaskState = TaskState.DRAFT
    planner: RoleConfig = field(default_factory=RoleConfig)
    implementer: RoleConfig = field(default_factory=RoleConfig)
    reviewer: RoleConfig = field(default_factory=RoleConfig)
    final: RoleConfig = field(default_factory=RoleConfig)
    automode: bool = False
    max_iterations: int = 5
    test_command: str = ""
    create_branch: bool = True
    confirm_plan: bool = False  # en automode, pedir confirmación tras el plan
    branch: str = ""
    iteration: int = 0
    cycle: int = 1  # ciclo actual (≥2 = ampliaciones "pedir más")
    extensions: dict[str, str] = field(default_factory=dict)  # ciclo -> petición
    sessions: dict[str, str] = field(default_factory=dict)  # rol -> session id
    durations: dict[str, int] = field(default_factory=dict)  # fase -> segundos acumulados
    created_at: str = ""
    updated_at: str = ""

    # ------------------------------------------------------------------ #
    @classmethod
    def create(
        cls,
        name: str,
        description: str,
        workdir: str,
        config: Config,
        *,
        automode: bool | None = None,
        test_command: str | None = None,
        create_branch: bool | None = None,
        confirm_plan: bool | None = None,
    ) -> "Task":
        now = datetime.now().isoformat(timespec="seconds")
        return cls(
            id=new_task_id(name),
            name=name,
            description=description,
            workdir=workdir,
            planner=RoleConfig(config.planner.cli, config.planner.model),
            implementer=RoleConfig(config.implementer.cli, config.implementer.model),
            reviewer=RoleConfig(config.reviewer.cli, config.reviewer.model),
            final=RoleConfig(config.final.cli, config.final.model),
            automode=config.automode.enabled if automode is None else automode,
            max_iterations=config.automode.max_iterations,
            test_command=config.automode.test_command if test_command is None else test_command,
            create_branch=config.automode.create_branch if create_branch is None else create_branch,
            confirm_plan=config.automode.confirm_plan if confirm_plan is None else confirm_plan,
            created_at=now,
            updated_at=now,
        )

    def role(self, name: str) -> RoleConfig:
        return getattr(self, name)

    @property
    def current_extension(self) -> str:
        """Petición de ampliación del ciclo actual (vacía en el ciclo 1)."""
        return self.extensions.get(str(self.cycle), "")

    def start_new_cycle(self, request: str) -> None:
        """Registra una ampliación y reinicia la máquina de estados del ciclo."""
        self.cycle += 1
        self.extensions[str(self.cycle)] = request
        self.iteration = 0
        self.state = TaskState.DRAFT

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "task": {
                "id": self.id,
                "name": self.name,
                "description": self.description,
                "workdir": self.workdir,
                "state": self.state.value,
                "automode": self.automode,
                "max_iterations": self.max_iterations,
                "test_command": self.test_command,
                "create_branch": self.create_branch,
                "confirm_plan": self.confirm_plan,
                "branch": self.branch,
                "iteration": self.iteration,
                "cycle": self.cycle,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            },
            "planner": self.planner.to_dict(),
            "implementer": self.implementer.to_dict(),
            "reviewer": self.reviewer.to_dict(),
            "final": self.final.to_dict(),
            "sessions": dict(self.sessions),
            "durations": dict(self.durations),
            "extensions": dict(self.extensions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        raw = data.get("task", {})
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name", "")),
            description=str(raw.get("description", "")),
            workdir=str(raw.get("workdir", ".")),
            state=TaskState(raw.get("state", TaskState.DRAFT.value)),
            planner=RoleConfig.from_dict(data.get("planner", {}), default_cli="opencode"),
            implementer=RoleConfig.from_dict(data.get("implementer", {}), default_cli="kimi"),
            reviewer=RoleConfig.from_dict(data.get("reviewer", {}), default_cli="opencode"),
            final=RoleConfig.from_dict(data.get("final", {}), default_cli="opencode"),
            automode=bool(raw.get("automode", False)),
            max_iterations=int(raw.get("max_iterations", 5)),
            test_command=str(raw.get("test_command", "")),
            create_branch=bool(raw.get("create_branch", True)),
            confirm_plan=bool(raw.get("confirm_plan", False)),
            branch=str(raw.get("branch", "")),
            iteration=int(raw.get("iteration", 0)),
            cycle=int(raw.get("cycle", 1)),
            sessions={str(k): str(v) for k, v in data.get("sessions", {}).items()},
            durations={str(k): int(v) for k, v in data.get("durations", {}).items()},
            extensions={str(k): str(v) for k, v in data.get("extensions", {}).items()},
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
        )


# ---------------------------------------------------------------------- #
def save(task: Task) -> None:
    task.updated_at = datetime.now().isoformat(timespec="seconds")
    base = paths.task_dir(task.id)
    base.mkdir(parents=True, exist_ok=True)
    paths.plan_dir(task.id)
    paths.review_dir(task.id)
    paths.final_dir(task.id)
    paths.logs_dir(task.id)
    paths.task_meta_path(task.id).write_text(_toml.dumps(task.to_dict()), encoding="utf-8")


def load(task_id: str) -> Task:
    with paths.task_meta_path(task_id).open("rb") as handle:
        return Task.from_dict(tomllib.load(handle))


def list_all() -> list[Task]:
    tasks: list[Task] = []
    for entry in sorted(paths.tasks_dir().iterdir(), reverse=True):
        meta = entry / "task.toml"
        if entry.is_dir() and meta.exists():
            try:
                tasks.append(load(entry.name))
            except Exception:
                continue  # tarea corrupta: se ignora en el listado
    return tasks
