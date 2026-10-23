"""REST router for the API server.

Routes are precompiled into ``re.Pattern`` objects at import time so the
runtime dispatch is a flat ``fullmatch`` per request, O(1) per pattern.
Generic ``{name}`` placeholders (``{task_id}``, ``{session_id}``) are
translated to a named ``[^/]+`` group whose value lands in ``params``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from . import actions, clisessions, clock, projectcreate, projectdocs, projectgit, reviews, settings
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
_PLACEHOLDER = re.compile(r"\\\{(\w+)\\\}")  # an escaped "{name}" inside re.escape(path)


def route(method: str, path: str) -> Callable[[REST_HANDLER], REST_HANDLER]:
    """Decorator that registers a REST handler at ``method path``.

    Path may include ``{name}`` placeholders which become named groups; the
    captured values land in the ``params`` dict passed to the handler.
    """

    def decorator(handler: REST_HANDLER) -> REST_HANDLER:
        regex = re.compile(_PLACEHOLDER.sub(r"(?P<\1>[^/]+)", re.escape(path)) + "$")
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


@route("GET", "/api/v1/time")
async def _time(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return clock.server_time()


# ---------------------------------------------------------------------- #
# Read endpoints
# ---------------------------------------------------------------------- #
@route("GET", "/api/v1/tasks")
async def _tasks_list(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    query = request.query
    return actions.list_tasks(
        service,
        state=query.get("state") or None,
        workdir=query.get("workdir") or None,
        project=query.get("project") or None,
        sort=query.get("sort") or None,
        order=query.get("order") or None,
        limit=_query_int(request, "limit", None),
        offset=_query_int(request, "offset", 0),
    )


@route("GET", "/api/v1/tasks/{task_id}")
async def _task_detail(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.get_task(service, params["task_id"])


@route("GET", "/api/v1/projects")
async def _projects_list(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.list_projects(service)


@route("POST", "/api/v1/projects")
async def _projects_create(service: "ServerService", request: Request, params: dict[str, str]) -> Response:
    status, body = await projectcreate.create_project(service, parse_json_body(request))
    return Response.json(status, body)


@route("GET", "/api/v1/projects/docs")
async def _project_docs(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    query = request.query
    return projectdocs.list_docs(
        service, workdir=query.get("workdir") or "", project=query.get("project") or ""
    )


@route("GET", "/api/v1/projects/doc")
async def _project_doc(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    query = request.query
    return projectdocs.read_doc(
        service,
        workdir=query.get("workdir") or "",
        project=query.get("project") or "",
        name=query.get("name") or "",
        offset=_query_int(request, "offset", 0),
        max_chars=_query_int(request, "max_chars", None),
    )


@route("GET", "/api/v1/projects/repos")
async def _project_repos(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    query = request.query
    return await reviews.list_repos(
        service, workdir=query.get("workdir") or "", project=query.get("project") or ""
    )


@route("GET", "/api/v1/projects/changes")
async def _project_changes(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    query = request.query
    return await projectgit.list_changes(
        service, workdir=query.get("workdir") or "", project=query.get("project") or "",
        refresh=actions._coerce_bool(query.get("refresh"), False),
    )


@route("GET", "/api/v1/projects/branches")
async def _project_branches(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await projectgit.list_branches(service, dict(request.query))


@route("POST", "/api/v1/projects/push")
async def _project_push(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await projectgit.push(service, parse_json_body(request))


@route("POST", "/api/v1/projects/pull")
async def _project_pull(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await projectgit.pull(service, parse_json_body(request))


@route("POST", "/api/v1/projects/checkout")
async def _project_checkout(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await projectgit.checkout(service, parse_json_body(request))


@route("POST", "/api/v1/projects/abort")
async def _project_abort(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await projectgit.abort(service, parse_json_body(request))


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
    path, mime = actions.media_file(service, params["task_id"], request.query.get("name") or "")
    return Response(  # streamed from disk: constant memory even for 500 MB files
        200,
        headers={"Content-Type": mime, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        file=path,
    )


@route("GET", "/api/v1/tasks/{task_id}/attachments")
async def _task_attachments(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.list_task_attachments(service, params["task_id"])


@route("POST", "/api/v1/tasks/{task_id}/attachments")
async def _task_attachments_upload(service: "ServerService", request: Request, params: dict[str, str]):
    """JSON body {"attachments": [{name, data}]} or a raw file body with ?name=."""
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("application/json"):
        attachments = actions._decode_attachments(parse_json_body(request))
    else:
        name = (request.query.get("name") or "").strip()
        if not name:
            raise actions.ApiError(400, "name is required")
        raw_cycle = (request.query.get("cycle") or "").strip()
        cycle: int | None = None
        if raw_cycle:
            try:
                cycle = int(raw_cycle)
            except ValueError as exc:
                raise actions.ApiError(400, "cycle must be an integer") from exc
        if request.body_path is not None:  # spooled by httpcore: moved, never loaded
            return actions.upload_attachment_file(service, params["task_id"], name, request.body_path, cycle)
        if not request.body:
            raise actions.ApiError(400, "empty body")
        attachments = [(name, bytes(request.body))]  # defensive fallback: small in-memory body
    return actions.upload_attachments(service, params["task_id"], attachments)


@route("DELETE", "/api/v1/tasks/{task_id}/attachments")
async def _task_attachments_delete(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return actions.delete_task_attachment(service, params["task_id"], request.query.get("file") or "")


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
# Direct CLI sessions
# ---------------------------------------------------------------------- #
@route("GET", "/api/v1/sessions")
async def _sessions_list(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return clisessions.list_sessions(service, workdir=request.query.get("workdir") or "")


@route("POST", "/api/v1/sessions")
async def _sessions_create(service: "ServerService", request: Request, params: dict[str, str]) -> Response:
    status, body = clisessions.create_session(service, parse_json_body(request))
    return Response.json(status, body)


@route("GET", "/api/v1/sessions/{session_id}")
async def _session_get(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return clisessions.get_session(service, params["session_id"])


@route("DELETE", "/api/v1/sessions/{session_id}")
async def _session_delete(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await clisessions.delete_session(service, params["session_id"])


@route("GET", "/api/v1/sessions/{session_id}/transcript")
async def _session_transcript(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    after = _query_int(request, "after", 0)
    limit = _query_int(request, "limit", 0)
    return clisessions.get_transcript(service, params["session_id"], after=after, limit=limit)


@route("POST", "/api/v1/sessions/{session_id}/send")
async def _session_send(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    payload = parse_json_body(request)
    return clisessions.send_prompt(service, params["session_id"], str(payload.get("prompt") or ""))


@route("POST", "/api/v1/sessions/{session_id}/cancel")
async def _session_cancel(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return clisessions.cancel_session(service, params["session_id"])


@route("POST", "/api/v1/sessions/{session_id}/edit")
async def _session_edit(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return clisessions.edit_session(service, params["session_id"], parse_json_body(request))


@route("POST", "/api/v1/sessions/{session_id}/close")
async def _session_close(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return clisessions.close_session(service, params["session_id"])


@route("POST", "/api/v1/sessions/{session_id}/reopen")
async def _session_reopen(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return clisessions.reopen_session(service, params["session_id"])


# ---------------------------------------------------------------------- #
# Assisted reviews
# ---------------------------------------------------------------------- #
@route("GET", "/api/v1/reviews")
async def _reviews_list(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    query = request.query
    return reviews.list_reviews(
        service, workdir=query.get("workdir") or "", state=query.get("state") or ""
    )


@route("POST", "/api/v1/reviews")
async def _reviews_start(service: "ServerService", request: Request, params: dict[str, str]) -> Response:
    status, body = await reviews.start_review(service, parse_json_body(request))
    return Response.json(status, body)


@route("GET", "/api/v1/reviews/{review_id}")
async def _review_get(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await reviews.get_review(service, params["review_id"])


@route("DELETE", "/api/v1/reviews/{review_id}")
async def _review_delete(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return reviews.delete_review(service, params["review_id"])


@route("POST", "/api/v1/reviews/{review_id}/close")
async def _review_close(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await reviews.close_review(service, params["review_id"])


@route("POST", "/api/v1/reviews/{review_id}/reopen")
async def _review_reopen(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await reviews.reopen_review(service, params["review_id"])


@route("GET", "/api/v1/reviews/{review_id}/files")
async def _review_files(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    query = request.query
    return await reviews.list_files(
        service, params["review_id"], repo=query.get("repo") or "",
        decision=query.get("decision") or "",
    )


@route("GET", "/api/v1/reviews/{review_id}/diff")
async def _review_diff(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await reviews.get_diff(service, params["review_id"], dict(request.query))


@route("POST", "/api/v1/reviews/{review_id}/navigate")
async def _review_navigate(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await reviews.navigate(service, params["review_id"], parse_json_body(request))


@route("POST", "/api/v1/reviews/{review_id}/decision")
async def _review_decision(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await reviews.decide(service, params["review_id"], parse_json_body(request))


@route("POST", "/api/v1/reviews/{review_id}/discuss")
async def _review_discuss(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await reviews.discuss(service, params["review_id"], parse_json_body(request))


@route("POST", "/api/v1/reviews/{review_id}/request-changes")
async def _review_request_changes(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await reviews.request_changes(service, params["review_id"], parse_json_body(request))


@route("GET", "/api/v1/reviews/{review_id}/transcript")
async def _review_transcript(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    after = _query_int(request, "after", 0)
    limit = _query_int(request, "limit", 0)
    return reviews.review_transcript(service, params["review_id"], after=after, limit=limit)


@route("POST", "/api/v1/reviews/{review_id}/cancel")
async def _review_cancel(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return reviews.cancel_review_turn(service, params["review_id"])


@route("POST", "/api/v1/reviews/{review_id}/commit-message")
async def _review_commit_message(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await reviews.commit_message(service, params["review_id"], parse_json_body(request))


@route("POST", "/api/v1/reviews/{review_id}/commit")
async def _review_commit(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return await reviews.commit(service, params["review_id"], parse_json_body(request))


@route("GET", "/api/v1/reviews/{review_id}/commits")
async def _review_commits(service: "ServerService", request: Request, params: dict[str, str]) -> dict:
    return reviews.list_commits(service, params["review_id"])


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _query_int(request: Request, name: str, default: int | None) -> int | None:
    raw = request.query.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise actions.ApiError(400, f"{name} must be an integer") from exc


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
        body = {"error": exc.message}
        if exc.code:
            body["code"] = exc.code
        return Response.json(exc.status, body), None
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
