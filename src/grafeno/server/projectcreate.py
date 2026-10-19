"""Project creation in the working directories for API clients (REST + WS)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import projectsetup, remotesession
from .. import workspaces as workspaces_module
from .actions import ApiError, _project_item, _workspace_roots

if TYPE_CHECKING:
    from .service import ServerService

_STATUS = {
    "invalid_name": 400,
    "invalid_url": 400,
    "invalid_mode": 400,
    "invalid_workspace": 400,
    "no_workspace": 409,
    "exists": 409,
    "git_missing": 503,
    "clone_failed": 422,
    "init_failed": 500,
    "io_error": 500,
}


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f"{key} must be a string")
    return value


async def create_project(service: "ServerService", payload: dict) -> tuple[int, dict]:
    """Create a project folder (git init, git clone or plain) in a working directory."""
    if remotesession.active():
        raise ApiError(409, "project creation is not available in remote session mode")
    try:
        created = await projectsetup.create_project(
            _text(payload, "name"),
            _workspace_roots(),
            workspace=_text(payload, "workspace"),
            url=_text(payload, "url"),
            mode=_text(payload, "mode"),
        )
    except projectsetup.ProjectError as exc:
        raise ApiError(_STATUS.get(exc.code, 400), exc.message) from exc
    project = workspaces_module.Project(
        name=created.name, path=created.path, workspace=created.workspace
    )
    log = getattr(service, "_log", None)
    if log is not None:
        log(f"project created: {created.path} ({created.mode})")
    return 201, {
        "project": _project_item(str(created.path), 0, project, 0),
        "mode": created.mode,
        "git": created.git,
        "url": created.url,
    }
