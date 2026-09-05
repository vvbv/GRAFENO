"""Read/write operations shared by the REST and WebSocket APIs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .. import models, paths
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
    """Return the full payload of one task."""
    try:
        task = models.load(task_id)
    except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
        raise ApiError(404, "task not found") from exc
    return task_detail(task)


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
