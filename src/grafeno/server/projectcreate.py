"""Project creation in the working directories for API clients (REST + WS)."""

from __future__ import annotations

import time
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
    "timeout": 504,
}


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f"{key} must be a string")
    return value


def _loggable(value: str, limit: int = 200) -> str:
    """Client text as a quoted, truncated repr: no forged log lines, bounded size."""
    return repr(value[:limit] + ("..." if len(value) > limit else ""))


async def create_project(service: "ServerService", payload: dict) -> tuple[int, dict]:
    """Create a project folder (git init, git clone or plain) in a working directory."""
    if remotesession.active():
        raise ApiError(409, "project creation is not available in remote session mode")
    log = getattr(service, "_log", None) or (lambda _message: None)
    name = _text(payload, "name")
    workspace = _text(payload, "workspace")
    url = _text(payload, "url")
    mode = _text(payload, "mode")
    log(
        f"project create requested: name={_loggable(name)} mode={_loggable(mode) if mode else 'auto'} "
        f"workspace={_loggable(workspace) if workspace else '-'} "
        f"url={_loggable(projectsetup.redact_url(url)) if url else '-'}"
    )
    started = time.monotonic()
    try:
        created = await projectsetup.create_project(
            name, _workspace_roots(), workspace=workspace, url=url, mode=mode
        )
    except projectsetup.ProjectError as exc:
        log(f"project create failed: {exc.code}: {exc.message} ({time.monotonic() - started:.1f} s)")
        raise ApiError(_STATUS.get(exc.code, 400), exc.message, code=exc.code) from exc
    project = workspaces_module.Project(
        name=created.name, path=created.path, workspace=created.workspace
    )
    log(f"project created: {created.path} ({created.mode}, {time.monotonic() - started:.1f} s)")
    return 201, {
        "project": _project_item(str(created.path), 0, project, 0),
        "mode": created.mode,
        "git": created.git,
        "url": created.url,
    }
