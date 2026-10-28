"""Web editor links for the panel: resolve the project folder, enforce the settings, build the URL."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .. import config as config_module
from .. import remotesession
from .. import startup as startup_module
from . import projectdocs
from .actions import ApiError, _load_or_404

if TYPE_CHECKING:
    from .service import ServerService

FEATURE = "web editor links"


def _editor_running(service: "ServerService", action) -> bool | None:
    """True/False when GRAFENO launched the editor at startup; None when it did not."""
    app = getattr(service, "app", None)
    runner = getattr(app, "startup_runner", None) if app is not None else None
    for item in getattr(runner, "processes", None) or []:
        if item.action.name == action.name and item.action.command == action.command:
            return item.process.poll() is None
    return None  # not launched by GRAFENO (disabled, --nostartup, external editor)


def open_editor(service: "ServerService", params: dict) -> dict:
    """URL that opens the project of ``workdir``/``project``/``task_id`` in the configured web editor."""
    action = startup_module.editor_action(config_module.load().startup)
    if action is None:
        raise ApiError(404, "no web editor configured (add a startup command with an editor URL)")
    task_id = str(params.get("task_id") or "").strip()
    if task_id:
        task = _load_or_404(task_id)
        if remotesession.active():
            raise ApiError(409, f"{FEATURE} are not available in remote session mode")
        if task.is_remote:
            raise ApiError(409, "remote projects are not supported")
        folder = Path(task.workdir).expanduser().resolve()
        if not folder.is_dir():
            raise ApiError(404, "project folder not found")
    else:
        _, folder = projectdocs.resolve_project(
            service, workdir=str(params.get("workdir") or ""),
            project=str(params.get("project") or ""), feature=FEATURE,
        )
    settings_updated = False
    settings_error = ""
    if action.settings_file.strip():  # never blocks the answer
        try:
            settings_updated = startup_module.apply_settings(
                action.settings_file, startup_module.effective_settings(action))
        except (OSError, ValueError) as exc:
            settings_error = str(exc)
    try:
        url = startup_module.editor_url(action, str(folder), str(params.get("host") or ""))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return {
        "url": url,
        "name": action.name,
        "folder": str(folder),
        "running": _editor_running(service, action),
        "settings_updated": settings_updated,
        "settings_error": settings_error,
    }
