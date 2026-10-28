"""Direct CLI session operations shared by the REST and WebSocket APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import clisessions, remotesession
from ..clisession_runtime import CliSessionManager, SessionBusy, SessionClosed
from ..clisessions import SESSION_CLIS, CliSession
from ..i18n import t
from .actions import ApiError
from .actions import _load_or_404 as load_task_or_404

if TYPE_CHECKING:
    from .service import ServerService

MAX_TRANSCRIPT_LIMIT = 2000


def session_summary(session: CliSession, running: bool) -> dict:
    """JSON summary of a session, as used by every response."""
    return {
        "id": session.id,
        "name": session.name,
        "workdir": session.workdir,
        "project_name": session.project_name,
        "cli": session.cli,
        "model": session.model,
        "effort": session.effort,
        "cli_session_id": session.cli_session_id,
        "state": session.state,
        "running": running,
        "turns": session.turns,
        "last_prompt": session.last_prompt,
        "last_error": session.last_error,
        "tokens": {"input": session.tokens_in, "output": session.tokens_out},
        "seconds": session.seconds,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "task_id": session.task_id,
        "initial_context": clisessions.has_initial_context(session.id),
    }


def _manager(service: "ServerService") -> CliSessionManager | None:
    return getattr(getattr(service, "app", None), "cli_sessions", None)


def _require_manager(service: "ServerService") -> CliSessionManager:
    manager = _manager(service)
    if manager is None:
        raise ApiError(503, "app unavailable")
    return manager


def _load_or_404(service: "ServerService", session_id: str) -> CliSession:
    manager = _manager(service)
    try:
        return manager.get(session_id) if manager else clisessions.load(session_id)
    except (FileNotFoundError, OSError, ValueError) as exc:  # ValueError: corrupt session.toml
        raise ApiError(404, "session not found") from exc


def _running(service: "ServerService", session_id: str) -> bool:
    manager = _manager(service)
    return bool(manager and manager.is_running(session_id))


def _summary(service: "ServerService", session: CliSession) -> dict:
    return session_summary(session, _running(service, session.id))


def list_sessions(service: "ServerService", workdir: str = "") -> dict:
    sessions = clisessions.list_all(workdir)
    return {
        "sessions": [_summary(service, s) for s in sessions],
        "clis": list(SESSION_CLIS),
        "available": not remotesession.active(),
    }


def get_session(service: "ServerService", session_id: str) -> dict:
    return {"session": _summary(service, _load_or_404(service, session_id))}


def get_transcript(
    service: "ServerService", session_id: str, after: int = 0, limit: int = 0
) -> dict:
    if after < 0:
        raise ApiError(400, "after must be >= 0")
    if not 0 <= limit <= MAX_TRANSCRIPT_LIMIT:
        raise ApiError(400, f"limit must be between 0 and {MAX_TRANSCRIPT_LIMIT}")
    session = _load_or_404(service, session_id)
    entries, total = clisessions.load_transcript(session_id, after=after, limit=limit)
    return {
        "entries": entries,
        "total": total,
        "running": _running(service, session_id),
        "session": _summary(service, session),
    }


def _text(payload: dict, key: str) -> str:
    return str(payload.get(key) or "").strip()


def create_session(service: "ServerService", payload: dict) -> tuple[int, dict]:
    manager = _require_manager(service)
    prompt = _text(payload, "prompt")
    task_id = _text(payload, "task_id")
    task = None
    if task_id:
        task = load_task_or_404(task_id)  # 404 task not found
        if task.is_remote:
            raise ApiError(409, "remote projects are not supported")
    workdir = _text(payload, "workdir") or (task.workdir if task else "")
    name = _text(payload, "name") or (t("cs.task_session_name", name=task.name) if task else "")
    try:
        session = manager.create(
            workdir,
            _text(payload, "cli") or "claude",
            model=_text(payload, "model"),
            effort=_text(payload, "effort"),
            name=name,
            task_id=task.id if task else "",
        )
        if task is not None:
            try:
                clisessions.save_initial_context(session.id, clisessions.task_context(task))
            except OSError as exc:
                raise ApiError(500, "could not store the initial context") from exc
        if prompt:
            manager.send(session.id, prompt)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return 201, {"session": session_summary(manager.get(session.id), manager.is_running(session.id))}


def send_prompt(service: "ServerService", session_id: str, prompt: str) -> dict:
    manager = _require_manager(service)
    _load_or_404(service, session_id)
    try:
        session = manager.send(session_id, prompt)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    except SessionBusy as exc:
        raise ApiError(409, t("cs.busy")) from exc
    except SessionClosed as exc:
        raise ApiError(409, t("cs.closed_notice")) from exc
    return {"session": _summary(service, session)}


def cancel_session(service: "ServerService", session_id: str) -> dict:
    manager = _require_manager(service)
    session = _load_or_404(service, session_id)
    if not manager.cancel(session_id):
        raise ApiError(409, "session is not running")
    return {"session": _summary(service, session)}


def edit_session(service: "ServerService", session_id: str, payload: dict) -> dict:
    manager = _require_manager(service)
    _load_or_404(service, session_id)
    try:
        session = manager.rename(session_id, _text(payload, "name"))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return {"session": _summary(service, session)}


def close_session(service: "ServerService", session_id: str) -> dict:
    manager = _require_manager(service)
    _load_or_404(service, session_id)
    try:
        session = manager.close(session_id)
    except SessionBusy as exc:
        raise ApiError(409, t("cs.busy")) from exc
    return {"session": _summary(service, session)}


def reopen_session(service: "ServerService", session_id: str) -> dict:
    manager = _require_manager(service)
    _load_or_404(service, session_id)
    return {"session": _summary(service, manager.reopen(session_id))}


async def delete_session(service: "ServerService", session_id: str) -> dict:
    manager = _require_manager(service)
    _load_or_404(service, session_id)
    await manager.delete(session_id)
    return {"ok": True}
