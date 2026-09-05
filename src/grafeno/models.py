"""Data model of a GRAFENO task and its state machine."""

from __future__ import annotations

import re
import shutil
import tomllib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from . import _toml, live_log, paths
from .config import Config, RoleConfig
from .i18n import t
from .references import Reference

if TYPE_CHECKING:
    from .drivers.base import TokenUsage


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
    DISCARDED = "discarded"


def state_label(state: "TaskState") -> str:
    """Localized label for a task state."""
    return t(f"state.{state.value}")


def task_state_label(task: "Task") -> str:
    """State label for display, with a "Waiting" suffix during quota waits."""
    label = state_label(task.state)
    if task.usage_waiting:
        label = f"{label} {t('state.waiting')}"
    return label


# Phases shown in the detail progress bar.
PHASES = ("plan", "implement", "review", "final", "done")

TOKEN_KEY_SEP = "|"
DEFAULT_MODEL_LABEL = "default"  # key when the role does not set a model
LEGACY_PHASE = "legacy"          # phase for usage records stored with the old format
# Order of the phases with tokens (used to display the breakdown).
TOKEN_PHASES = ("plan", "implement", "review", "fix", "final")

# Valid task repetition modes (empty = not repetitive).
REPEAT_MODES = ("", "interval", "infinite")
# Plan reuse policy between repetitions.
PLAN_REUSE_MODES = ("reuse", "replan", "reevaluate")


def _token_key(phase: str, cli: str, model: str, kind: str) -> str:
    """Compose the flat key ``"{phase}|{cli}|{model}|{input|output}"``."""
    return f"{phase}{TOKEN_KEY_SEP}{cli}{TOKEN_KEY_SEP}{model}{TOKEN_KEY_SEP}{kind}"


def _parse_token_key(key: str) -> tuple[str, str, str, str]:
    """Decompose a token key into (phase, cli, model, kind).

    The legacy format ``"{model}|{kind}"`` is interpreted as phase
    ``legacy`` and empty cli. ``kind`` is split with ``rpartition`` so that
    models whose name contains ``|`` do not break parsing.
    """
    prefix, _, kind = key.rpartition(TOKEN_KEY_SEP)
    parts = prefix.split(TOKEN_KEY_SEP)
    if len(parts) >= 3:
        phase = parts[0]
        cli = parts[1]
        model = TOKEN_KEY_SEP.join(parts[2:])
        return (phase or LEGACY_PHASE), cli, model, kind or "input"
    return LEGACY_PHASE, "", prefix, kind or "input"


def cli_model_label(cli: str, model: str) -> str:
    """Agent label: ``"cli/model"``; just the model if there is no cli."""
    return f"{cli}/{model}" if cli else model


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
    remote: str = ""  # canonical remote spec "user@host:/path"; empty = local
    remote_os: str = ""  # detected destination OS (best effort); empty = unknown
    state: TaskState = TaskState.DRAFT
    planner: RoleConfig = field(default_factory=RoleConfig)
    implementer: RoleConfig = field(default_factory=RoleConfig)
    reviewer: RoleConfig = field(default_factory=RoleConfig)
    final: RoleConfig = field(default_factory=RoleConfig)
    automode: bool = False
    max_iterations: int = 5
    test_command: str = ""
    create_branch: bool = True
    confirm_plan: bool = False  # in automode, ask for confirmation after the plan
    final_prompt: str = ""  # extra instructions for the final steps
    hook_command: str = ""  # empty = no task-specific hook
    hook_stages: str = ""   # comma-separated stages; empty = none
    hook_mode: str = "override"  # "override" (replaces the global) | "both"
    branch: str = ""
    base_commit: str = ""  # project HEAD when the implementation started (diff base for changes.md)
    iteration: int = 0
    cycle: int = 1  # current cycle (>=2 = "ask for more" extensions)
    extensions: dict[str, str] = field(default_factory=dict)  # cycle -> request
    sessions: dict[str, str] = field(default_factory=dict)  # role -> session id
    durations: dict[str, int] = field(default_factory=dict)  # phase -> accumulated seconds
    tokens: dict[str, int] = field(default_factory=dict)  # "{phase}|{cli}|{model}|{input|output}" -> accumulated tokens
    # References: list of additional resources (paths/URLs) attached as
    # inspiration/context for the pipeline agents. Up to three levels are
    # combined at prompt-time via ``references.resolve``.
    use_global_references: bool = True   # include global references in prompts
    use_project_references: bool = True  # include project references in prompts
    references: list[Reference] = field(default_factory=list)  # task-level refs
    # Time scheduling and repetition (see scheduler.py).
    scheduled_at: str = ""        # local ISO "YYYY-MM-DDTHH:MM"; empty = unscheduled
    parent_id: str = ""           # id of the parent task (chained); empty = root
    repeat_mode: str = ""         # "" | "interval" | "infinite"
    repeat_interval_minutes: int = 60  # only if repeat_mode == "interval"
    plan_reuse: str = "reuse"     # "reuse" | "replan" | "reevaluate"
    repeat_count: int = 0         # repetitions already executed (0 = first execution)
    last_completed_at: str = ""   # local ISO of the last time it reached DONE
    origin: str = ""              # "" = normal; "trigger" = spawned by a trigger task
    failed_phase: str = ""        # pipeline phase that failed (plan/implement/review/fix/final); "" = unknown
    usage_waiting: bool = field(default=False, repr=False)  # transient: waiting for CLI quota
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
        final_prompt: str | None = None,
        hook_command: str | None = None,
        hook_stages: str | None = None,
        hook_mode: str | None = None,
        scheduled_at: str | None = None,
        parent_id: str | None = None,
        repeat_mode: str | None = None,
        repeat_interval_minutes: int | None = None,
        plan_reuse: str | None = None,
        use_global_references: bool | None = None,
        use_project_references: bool | None = None,
        references: list[Reference] | None = None,
        remote: str | None = None,
    ) -> "Task":
        now = datetime.now().isoformat(timespec="seconds")
        return cls(
            id=new_task_id(name),
            name=name,
            description=description,
            workdir=workdir,
            remote="" if remote is None else remote,
            planner=RoleConfig(config.planner.cli, config.planner.model, config.planner.effort),
            implementer=RoleConfig(config.implementer.cli, config.implementer.model, config.implementer.effort),
            reviewer=RoleConfig(config.reviewer.cli, config.reviewer.model, config.reviewer.effort),
            final=RoleConfig(config.final.cli, config.final.model, config.final.effort),
            automode=config.automode.enabled if automode is None else automode,
            max_iterations=config.automode.max_iterations,
            test_command=config.automode.test_command if test_command is None else test_command,
            create_branch=config.automode.create_branch if create_branch is None else create_branch,
            confirm_plan=config.automode.confirm_plan if confirm_plan is None else confirm_plan,
            final_prompt=config.final_prompt if final_prompt is None else final_prompt,
            hook_command="" if hook_command is None else hook_command,
            hook_stages="" if hook_stages is None else hook_stages,
            hook_mode="override" if hook_mode is None else hook_mode,
            scheduled_at="" if scheduled_at is None else scheduled_at,
            parent_id="" if parent_id is None else parent_id,
            repeat_mode="" if repeat_mode is None else repeat_mode,
            repeat_interval_minutes=60 if repeat_interval_minutes is None else repeat_interval_minutes,
            plan_reuse="reuse" if plan_reuse is None else plan_reuse,
            use_global_references=True if use_global_references is None else use_global_references,
            use_project_references=True if use_project_references is None else use_project_references,
            references=[] if references is None else references,
            created_at=now,
            updated_at=now,
        )

    def role(self, name: str) -> RoleConfig:
        return getattr(self, name)

    @property
    def is_remote(self) -> bool:
        """True when the task points to a remote (SSH) project."""
        return bool(self.remote.strip())

    @property
    def current_extension(self) -> str:
        """Extension request for the current cycle (empty in cycle 1)."""
        return self.extensions.get(str(self.cycle), "")

    def start_new_cycle(self, request: str) -> None:
        """Record an extension and reset the state machine for the cycle."""
        self.cycle += 1
        self.extensions[str(self.cycle)] = request
        self.iteration = 0
        self.state = TaskState.DRAFT
        self.failed_phase = ""

    def record_tokens(self, cli: str, model: str, phase: str, usage: "TokenUsage") -> None:
        """Accumulate the token usage of a run under its phase and CLI+model."""
        key = model or DEFAULT_MODEL_LABEL
        self.tokens[_token_key(phase, cli, key, "input")] = (
            self.tokens.get(_token_key(phase, cli, key, "input"), 0) + usage.input
        )
        self.tokens[_token_key(phase, cli, key, "output")] = (
            self.tokens.get(_token_key(phase, cli, key, "output"), 0) + usage.output
        )

    def token_totals(self) -> tuple[int, int]:
        """Totals (input, output) of the task summing every model."""
        suffix_in = f"{TOKEN_KEY_SEP}input"
        suffix_out = f"{TOKEN_KEY_SEP}output"
        total_in = sum(v for k, v in self.tokens.items() if k.endswith(suffix_in))
        total_out = sum(v for k, v in self.tokens.items() if k.endswith(suffix_out))
        return total_in, total_out

    def tokens_by_phase(self) -> dict[str, tuple[int, int]]:
        """Breakdown phase -> (input, output)."""
        result: dict[str, list[int]] = {}
        for key, value in self.tokens.items():
            phase, _, _, kind = _parse_token_key(key)
            entry = result.setdefault(phase, [0, 0])
            if kind == "input":
                entry[0] += value
            elif kind == "output":
                entry[1] += value
        return {phase: (pair[0], pair[1]) for phase, pair in result.items()}

    def tokens_by_cli_model(self) -> dict[str, tuple[int, int]]:
        """Breakdown ``cli/model`` label -> (input, output)."""
        result: dict[str, list[int]] = {}
        for key, value in self.tokens.items():
            _, cli, model, kind = _parse_token_key(key)
            label = cli_model_label(cli, model)
            entry = result.setdefault(label, [0, 0])
            if kind == "input":
                entry[0] += value
            elif kind == "output":
                entry[1] += value
        return {label: (pair[0], pair[1]) for label, pair in result.items()}

    def total_duration_seconds(self) -> int:
        """Total accumulated duration of the task across every phase."""
        return sum(self.durations.values())

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "task": {
                "id": self.id,
                "name": self.name,
                "description": self.description,
                "workdir": self.workdir,
                "remote": self.remote,
                "remote_os": self.remote_os,
                "state": self.state.value,
                "automode": self.automode,
                "max_iterations": self.max_iterations,
                "test_command": self.test_command,
                "create_branch": self.create_branch,
                "confirm_plan": self.confirm_plan,
                "final_prompt": self.final_prompt,
                "hook_command": self.hook_command,
                "hook_stages": self.hook_stages,
                "hook_mode": self.hook_mode,
                "branch": self.branch,
                "base_commit": self.base_commit,
                "iteration": self.iteration,
                "cycle": self.cycle,
                "scheduled_at": self.scheduled_at,
                "parent_id": self.parent_id,
                "repeat_mode": self.repeat_mode,
                "repeat_interval_minutes": self.repeat_interval_minutes,
                "plan_reuse": self.plan_reuse,
                "repeat_count": self.repeat_count,
                "last_completed_at": self.last_completed_at,
                "origin": self.origin,
                "failed_phase": self.failed_phase,
                "use_global_references": self.use_global_references,
                "use_project_references": self.use_project_references,
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
            "tokens": dict(self.tokens),
            "references": [ref.to_dict() for ref in self.references],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        raw = data.get("task", {})
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name", "")),
            description=str(raw.get("description", "")),
            workdir=str(raw.get("workdir", ".")),
            remote=str(raw.get("remote", "")),
            remote_os=str(raw.get("remote_os", "")),
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
            final_prompt=str(raw.get("final_prompt", "")),
            hook_command=str(raw.get("hook_command", "")),
            hook_stages=str(raw.get("hook_stages", "")),
            hook_mode=str(raw.get("hook_mode", "override")),
            branch=str(raw.get("branch", "")),
            base_commit=str(raw.get("base_commit", "")),
            iteration=int(raw.get("iteration", 0)),
            cycle=int(raw.get("cycle", 1)),
            scheduled_at=str(raw.get("scheduled_at", "")),
            parent_id=str(raw.get("parent_id", "")),
            repeat_mode=str(raw.get("repeat_mode", "")),
            repeat_interval_minutes=int(raw.get("repeat_interval_minutes", 60)),
            plan_reuse=str(raw.get("plan_reuse", "reuse")),
            repeat_count=int(raw.get("repeat_count", 0)),
            last_completed_at=str(raw.get("last_completed_at", "")),
            origin=str(raw.get("origin", "")),
            failed_phase=str(raw.get("failed_phase", "")),
            sessions={str(k): str(v) for k, v in data.get("sessions", {}).items()},
            durations={str(k): int(v) for k, v in data.get("durations", {}).items()},
            extensions={str(k): str(v) for k, v in data.get("extensions", {}).items()},
            tokens={str(k): int(v) for k, v in data.get("tokens", {}).items()},
            use_global_references=bool(raw.get("use_global_references", True)),
            use_project_references=bool(raw.get("use_project_references", True)),
            references=[
                Reference.from_dict(item)
                for item in data.get("references", [])
                if isinstance(item, dict)
            ],
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


def reset_to_draft(task: Task) -> None:
    """Reset the task to DRAFT so it can be launched from scratch.

    Clears the state machine (state, iteration, cycle, sessions and
    extensions), unschedules unattended startup, clears the recorded base
    commit and deletes the pipeline artifacts (``plan/``, ``review/``,
    ``final/``) so that the next run re-plans with the current name and
    description. Keeps tokens, durations, hooks and the already-created git
    branch.
    """
    task.state = TaskState.DRAFT
    task.iteration = 0
    task.cycle = 1
    task.sessions = {}
    task.extensions = {}
    task.scheduled_at = ""
    task.base_commit = ""
    task.failed_phase = ""
    for directory in (
        paths.plan_dir(task.id),
        paths.review_dir(task.id),
        paths.final_dir(task.id),
    ):
        shutil.rmtree(directory, ignore_errors=True)
    live_log.clear(task.id)
    save(task)


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
                continue  # corrupt task: ignored in the listing
    return tasks


def tasks_signature() -> tuple[tuple[str, int, int], ...]:
    """Cheap change detector for the tasks store.

    Sorted tuple of ``(task_id, mtime_ns, size)`` for every ``task.toml``
    under the tasks dir. Any save/create/delete changes the signature.
    The list screen polls it to refresh only when something changed.
    """
    base = paths.tasks_dir()
    if not base.is_dir():
        return ()
    entries: list[tuple[str, int, int]] = []
    for entry in base.iterdir():
        meta = entry / "task.toml"
        try:
            if entry.is_dir() and meta.exists():
                stat = meta.stat()
                entries.append((entry.name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue  # transient stat error: skip that entry
    entries.sort()
    return tuple(entries)
