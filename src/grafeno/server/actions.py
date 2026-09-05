"""Read/write operations shared by the REST and WebSocket APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import models, paths
from ..models import Task, task_state_label

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
