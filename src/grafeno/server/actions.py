"""Read/write operations shared by the REST and WebSocket APIs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .. import models, paths, scheduler
from ..models import Task, TaskState, task_state_label

if TYPE_CHECKING:
    from .service import ServerService


class ApiError(Exception):
    """Domain error raised by action handlers; mapped to an HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def task_summary(task: Task) -> dict:
    """Slim task payload for list endpoints and WS events."""
    return {
        "id": task.id,
        "name": task.name,
        "workdir": task.workdir,
        "remote": task.remote,
        "state": task.state.value,
        "state_label": task_state_label(task),
        "automode": task.automode,
        "origin": task.origin,
        "scheduled_at": task.scheduled_at,
        "parent_id": task.parent_id,
    }


def task_detail(task: Task) -> dict:
    """Full payload: to_dict() + derived data (label, token totals)."""
    data = task.to_dict()
    data["state_label"] = task_state_label(task)
    total_in, total_out = task.token_totals()
    data["token_totals"] = {"input": total_in, "output": total_out}
    data["total_duration_seconds"] = task.total_duration_seconds()
    return data


# ---------------------------------------------------------------------- #
# Read operations
# ---------------------------------------------------------------------- #
def list_tasks(service: "ServerService", state: str | None = None) -> dict:
    """List tasks, optionally filtered by state."""
    tasks = models.list_all()
    if state:
        try:
            wanted = TaskState(state)
        except ValueError as exc:
            raise ApiError(400, f"unknown state: {state}") from exc
        tasks = [task for task in tasks if task.state is wanted]
    return {"tasks": [task_summary(task) for task in tasks]}


def get_task(service: "ServerService", task_id: str) -> dict:
    """Return the full payload of one task.

    The plan's contract is ``{"task": task_detail(task)}``: the response
    is wrapped under a ``task`` key (symmetric with ``list_tasks`` which
    wraps each item in ``{"tasks": [...]}``). ``task_detail`` itself
    already contains a nested ``task`` key (it returns ``Task.to_dict()``
    plus a few derived fields), so the result intentionally has a
    ``task.task.id`` location for the identifier; this matches the plan.
    """
    try:
        task = models.load(task_id)
    except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
        raise ApiError(404, "task not found") from exc
    return {"task": task_detail(task)}


def list_projects(service: "ServerService") -> dict:
    """Distinct task workdirs with their task counts (best effort)."""
    counts: dict[str, int] = {}
    for task in models.list_all():
        if not task.workdir:
            continue
        counts[task.workdir] = counts.get(task.workdir, 0) + 1
    try:
        from .. import workspaces as workspaces_module

        discovered = [
            str(path)
            for path in workspaces_module.discover(workspaces_module.resolve([]))
        ]
    except Exception:  # noqa: BLE001 - workspaces discovery is best effort
        discovered = []
    seen: set[str] = set()
    items: list[dict] = []
    for workdir, count in sorted(counts.items()):
        seen.add(workdir)
        items.append({"workdir": workdir, "count": count})
    for workdir in sorted(set(discovered) - seen):
        items.append({"workdir": workdir, "count": 0})
    return {"projects": items}


def get_logs(service: "ServerService", task_id: str, limit: int = 200) -> dict:
    """Tail of the persisted live log of a task (plain text per line)."""
    if limit < 1 or limit > 1000:
        raise ApiError(400, "limit must be between 1 and 1000")
    # Ensure the task exists: 404 before any content.
    try:
        models.load(task_id)
    except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
        raise ApiError(404, "task not found") from exc
    from .. import live_log

    entries = live_log.load(task_id, limit)
    lines = [entry.plain for entry in entries]
    return {"logs": lines}


_KINDS = {
    "plan": paths.plan_dir,
    "review": paths.review_dir,
    "final": paths.final_dir,
}


def get_artifacts(service: "ServerService", task_id: str, kind: str, cycle: int = 1) -> dict:
    """Return every .md file of a plan/review/final phase of a task."""
    if kind not in _KINDS:
        raise ApiError(400, f"unknown kind: {kind}")
    if cycle < 1:
        raise ApiError(400, "cycle must be >= 1")
    try:
        models.load(task_id)
    except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
        raise ApiError(404, "task not found") from exc
    directory: Path = _KINDS[kind](task_id, cycle)
    files: list[dict] = []
    if directory.is_dir():
        for entry in sorted(directory.glob("*.md")):
            try:
                content = entry.read_text(encoding="utf-8")
            except OSError:
                continue
            files.append({"name": entry.name, "content": content})
    return {"kind": kind, "cycle": cycle, "files": files}


# ---------------------------------------------------------------------- #
# Write operations
# ---------------------------------------------------------------------- #
def _runtime_start(service: "ServerService", task: Task, runner_factory, label: str) -> None:
    """Start a runner on the task's runtime, raising ApiError on failures."""
    app = service.app
    if app is None:
        raise ApiError(503, "app unavailable")
    runtime = service.app.runtime_for(task)
    if not runtime.start(app, runner_factory, label):
        raise ApiError(409, "task already running")


def _coerce_bool(value: object, default: bool) -> bool:
    """Coerce a JSON value to a boolean.

    Truthy strings (``"true"``, ``"1"``, ``"yes"``) and actual booleans
    become True; the rest become False. Without this, ``bool("false")``
    would silently keep automode on.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def create_task(service: "ServerService", payload: dict) -> dict:
    """Create a new task from a JSON payload and return its summary."""
    from .. import config as config_module

    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be an object")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError(400, "name is required")
    workdir = str(payload.get("workdir") or "").strip()
    if not workdir:
        raise ApiError(400, "workdir is required")
    description = str(payload.get("description") or "")
    cfg = config_module.load()
    parent_id = str(payload.get("parent_id") or "").strip()
    automode = _coerce_bool(payload.get("automode"), True)
    if parent_id:
        # Validate the proposed position using the same rule the TUI uses.
        task = models.Task.create(
            name=name,
            description=description,
            workdir=workdir,
            config=cfg,
            automode=automode,
            parent_id=parent_id or None,
        )
        by_id = {item.id: item for item in models.list_all()}
        error = scheduler.rechain_error(task, parent_id, by_id)
        if error:
            raise ApiError(400, error)
    else:
        task = models.Task.create(
            name=name,
            description=description,
            workdir=workdir,
            config=cfg,
            automode=automode,
        )
    scheduled_at = payload.get("scheduled_at")
    if scheduled_at:
        task.scheduled_at = str(scheduled_at)
    if payload.get("origin"):
        task.origin = str(payload["origin"])
    else:
        task.origin = "api"
    models.save(task)
    return 201, {"task": task_summary(task)}


def _load_or_404(task_id: str) -> Task:
    try:
        return models.load(task_id)
    except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
        raise ApiError(404, "task not found") from exc


def start_task(service: "ServerService", task_id: str) -> dict:
    """Start the full automode pipeline of a task."""
    task = _load_or_404(task_id)
    _runtime_start(service, task, lambda orch: orch.run_automode(), "API start")
    return {"ok": True}


def resume_task(service: "ServerService", task_id: str) -> dict:
    """Resume a FAILED task reusing the artifacts on disk."""
    task = _load_or_404(task_id)
    if task.state is not TaskState.FAILED:
        raise ApiError(409, "task is not in FAILED state")
    _runtime_start(service, task, lambda orch: orch.run_automode_resume(), "API resume")
    return {"ok": True}


def restart_task(service: "ServerService", task_id: str) -> dict:
    """Reset the task to DRAFT and start automode again."""
    task = _load_or_404(task_id)
    models.reset_to_draft(task)
    _runtime_start(service, task, lambda orch: orch.run_automode(), "API restart")
    return {"ok": True}


def extend_task(service: "ServerService", task_id: str, request: str) -> dict:
    """Start a new cycle on an existing task with a fresh request."""
    task = _load_or_404(task_id)
    request = (request or "").strip()
    if not request:
        raise ApiError(400, "request is required")
    task.start_new_cycle(request)
    models.save(task)
    _runtime_start(service, task, lambda orch: orch.run_automode_plan(), "API extend")
    return {"ok": True}


def pause_task(service: "ServerService", task_id: str) -> dict:
    """Pause a running task (cancels the worker)."""
    task = _load_or_404(task_id)
    app = service.app
    if app is None:
        raise ApiError(503, "app unavailable")
    runtime = app.runtimes.get(task.id)
    if runtime is None or not runtime.running:
        raise ApiError(409, "not running")
    runtime.cancel()
    return {"ok": True, "state": "paused"}


def discard_task(service: "ServerService", task_id: str) -> dict:
    """Mark the task as DISCARDED (cancel any running pipeline first)."""
    task = _load_or_404(task_id)
    app = service.app
    if app is not None:
        runtime = app.runtimes.get(task.id)
        if runtime is not None and runtime.running:
            runtime.cancel()
    task.state = TaskState.DISCARDED
    models.save(task)
    return {"ok": True, "state": "discarded"}


def mark_done(service: "ServerService", task_id: str) -> dict:
    """Force-complete the task without running the rest of the pipeline."""
    task = _load_or_404(task_id)
    task.state = TaskState.DONE
    models.save(task)
    return {"ok": True, "state": "done"}
