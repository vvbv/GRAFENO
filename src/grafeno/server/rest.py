"""REST router for the API server.

Routes are precompiled into ``re.Pattern`` objects at import time so the
runtime dispatch is a flat ``fullmatch`` per request, O(1) per pattern.
``{task_id}`` placeholders are translated to ``[^/]+`` and the captured
group becomes ``request.task_id``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from . import actions, settings
from .httpcore import Request, Response, WsHandler

if TYPE_CHECKING:
    from .service import ServerService

REST_HANDLER = Callable[["ServerService", Request, dict[str, str]], Awaitable[dict | Response]]


@dataclass
class _Route:
    method: str
    pattern: re.Pattern
    handler: REST_HANDLER


_ROUTES: list[_Route] = []


def route(method: str, path: str) -> Callable[[REST_HANDLER], REST_HANDLER]:
    """Decorator that registers a REST handler at ``method path``.

    Path may include ``{task_id}`` which becomes a captured group; the
    captured value lands in the ``params`` dict passed to the handler.
    """

    def decorator(handler: REST_HANDLER) -> REST_HANDLER:
        regex = re.compile(
            re.escape(path).replace(r"\{task_id\}", r"(?P<task_id>[^/]+)") + "$"
        )
        _ROUTES.append(_Route(method=method, pattern=regex, handler=handler))
        return handler

    return decorator


# ---------------------------------------------------------------------- #
# Status
# ---------------------------------------------------------------------- #
@route("GET", "/api/v1/status")
async def _status(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    import time as _time

    uptime = int(_time.monotonic() - service.start_monotonic) if service.start_monotonic else 0
    return {
        "version": service.version,
        "uptime_seconds": uptime,
        "pid": service.pid,
        "ws_path": "/api/v1/ws",
    }


# ---------------------------------------------------------------------- #
# Read endpoints
# ---------------------------------------------------------------------- #
@route("GET", "/api/v1/tasks")
async def _tasks_list(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    state = request.query.get("state") or None
    return actions.list_tasks(service, state=state)


@route("GET", "/api/v1/tasks/{task_id}")
async def _task_detail(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.get_task(service, params["task_id"])


@route("GET", "/api/v1/projects")
async def _projects_list(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.list_projects(service)


@route("GET", "/api/v1/tasks/{task_id}/logs")
async def _task_logs(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    raw_limit = request.query.get("limit", "200")
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise actions.ApiError(400, "limit must be an integer") from exc
    return actions.get_logs(service, params["task_id"], limit=limit)


@route("GET", "/api/v1/tasks/{task_id}/artifacts")
async def _task_artifacts(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    kind = request.query.get("kind") or ""
    raw_cycle = request.query.get("cycle", "1")
    try:
        cycle = int(raw_cycle)
    except ValueError as exc:
        raise actions.ApiError(400, "cycle must be an integer") from exc
    return actions.get_artifacts(service, params["task_id"], kind=kind, cycle=cycle)


@route("GET", "/api/v1/tasks/{task_id}/artifact")
async def _task_artifact_file(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    kind = request.query.get("kind") or ""
    path = request.query.get("path") or ""
    return actions.get_artifact_file(service, params["task_id"], kind=kind, path=path)


@route("GET", "/api/v1/tasks/{task_id}/media")
async def _task_media(service: "ServerService", request: Request, params: dict[str, str]) -> Response:
    data, mime = actions.get_media(service, params["task_id"], request.query.get("name") or "")
    return Response(200, headers={"Content-Type": mime, "Cache-Control": "no-store"}, body=data)


@route("GET", "/api/v1/options")
async def _options(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return settings.form_options(service)


@route("GET", "/api/v1/models")
async def _models(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    refresh = request.query.get("refresh", "") in ("1", "true", "yes")
    return await settings.models_catalog(service, refresh=refresh)


@route("GET", "/api/v1/fs/dirs")
async def _fs_dirs(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return settings.list_dirs(service, request.query.get("path") or "")


@route("GET", "/api/v1/issues")
async def _issues(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await settings.list_issues(service, request.query.get("workdir") or "")


@route("GET", "/api/v1/reports")
async def _reports(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return settings.usage_report(
        service,
        start=request.query.get("from") or "",
        end=request.query.get("to") or "",
        period=request.query.get("period") or "",
    )


@route("GET", "/api/v1/settings")
async def _settings_get(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return settings.get_settings(service)


@route("POST", "/api/v1/settings")
async def _settings_update(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return settings.update_settings(service, parse_json_body(request))


# ---------------------------------------------------------------------- #
# Write endpoints
# ---------------------------------------------------------------------- #
@route("POST", "/api/v1/tasks")
async def _task_create(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    payload = parse_json_body(request)
    return await actions.create_task(service, payload)


@route("POST", "/api/v1/audio/speech")
async def _audio_speech(service: "ServerService", request: Request, params: dict[str, str]) -> Response:
    payload = parse_json_body(request)
    text = str(payload.get("text") or "")
    fmt = str(payload.get("format") or "wav").strip().lower()
    audio, mime = await actions.synthesize_speech(service, text, fmt)
    return Response(200, headers={"Content-Type": mime}, body=audio)


@route("POST", "/api/v1/tasks/{task_id}/start")
async def _task_start(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.start_task(service, params["task_id"])


@route("POST", "/api/v1/tasks/{task_id}/resume")
async def _task_resume(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.resume_task(service, params["task_id"])


@route("POST", "/api/v1/tasks/{task_id}/restart")
async def _task_restart(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await actions.restart_task(service, params["task_id"])


@route("POST", "/api/v1/tasks/{task_id}/reset")
async def _task_reset(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await actions.reset_task(service, params["task_id"])


@route("POST", "/api/v1/tasks/{task_id}/run")
async def _task_run(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    payload = parse_json_body(request)
    return actions.run_phase(service, params["task_id"], str(payload.get("phase") or ""))


@route("POST", "/api/v1/tasks/{task_id}/continue")
async def _task_continue(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.continue_task(service, params["task_id"])


@route("POST", "/api/v1/tasks/{task_id}/approve-plan")
async def _task_approve_plan(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.approve_plan(service, params["task_id"])


@route("POST", "/api/v1/tasks/{task_id}/edit")
async def _task_edit(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.edit_task(service, params["task_id"], parse_json_body(request))


@route("POST", "/api/v1/tasks/{task_id}/roles")
async def _task_roles(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.update_roles(service, params["task_id"], parse_json_body(request))


@route("POST", "/api/v1/tasks/{task_id}/pause")
async def _task_pause(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.pause_task(service, params["task_id"])


@route("POST", "/api/v1/tasks/{task_id}/discard")
async def _task_discard(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await actions.discard_task(service, params["task_id"])


@route("POST", "/api/v1/tasks/{task_id}/mark-done")
async def _task_mark_done(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.mark_done(service, params["task_id"])


@route("POST", "/api/v1/tasks/{task_id}/extend")
async def _task_extend(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    payload = parse_json_body(request)
    request_text = str(payload.get("request") or "")
    attachments = actions._decode_attachments(payload)
    return actions.extend_task(service, params["task_id"], request=request_text, attachments=attachments)


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _match(request: Request) -> tuple[Optional[REST_HANDLER], dict[str, str]]:
    for route in _ROUTES:
        if route.method != request.method:
            continue
        match = route.pattern.fullmatch(request.path)
        if match is None:
            continue
        return route.handler, match.groupdict()
    return None, {}


async def dispatch_rest(
    service: "ServerService", request: Request
) -> tuple[Optional[Response], Optional[WsHandler]]:
    """Return the JSON response (or ``None`` for unknown methods/paths)."""
    if request.method == "HEAD":
        return Response(405, headers={"Allow": ", ".join(sorted({r.method for r in _ROUTES}))}), None
    if request.method == "OPTIONS":
        return Response(200, headers={"Allow": ", ".join(sorted({r.method for r in _ROUTES}))}), None
    handler, params = _match(request)
    if handler is None:
        if any(r.pattern.fullmatch(request.path) for r in _ROUTES):
            return Response.json(405, {"error": "method not allowed"}), None
        return Response.json(404, {"error": "not found"}), None
    try:
        result = await handler(service, request, params)
    except actions.ApiError as exc:
        return Response.json(exc.status, {"error": exc.message}), None
    except Exception as exc:  # noqa: BLE001
        service._log(f"unhandled error: {exc}")
        return Response.json(500, {"error": "internal error"}), None
    if isinstance(result, Response):
        return result, None
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
        status, payload = result
    else:
        status, payload = 200, result
    return Response.json(status, payload), None


def parse_json_body(request: Request) -> dict:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise actions.ApiError(400, "invalid json") from exc
    if not isinstance(data, dict):
        raise actions.ApiError(400, "invalid json")
    return data
