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

from . import actions
from .httpcore import Request, Response, WsHandler

if TYPE_CHECKING:
    from .service import ServerService

REST_HANDLER = Callable[["ServerService", Request, dict[str, str]], Awaitable[dict]]


@dataclass
class _Route:
    method: str
    pattern: re.Pattern
    handler: REST_HANDLER
    has_task_id: bool


_ROUTES: list[_Route] = []


def route(method: str, path: str) -> Callable[[REST_HANDLER], REST_HANDLER]:
    """Decorator that registers a REST handler at ``method path``.

    Path may include ``{task_id}`` which becomes a captured group; the
    captured value lands in the ``params`` dict passed to the handler.
    """

    def decorator(handler: REST_HANDLER) -> REST_HANDLER:
        regex = re.compile(re.escape(path).replace(r"\{task_id\}", r"(?P<task_id>[^/]+)") + "$")
        _ROUTES.append(_Route(method=method, pattern=regex, handler=handler, has_task_id="{task_id}" in path))
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
# Helpers
# ---------------------------------------------------------------------- #
def _match(request: Request) -> Optional[REST_HANDLER]:
    for route in _ROUTES:
        if route.method != request.method:
            continue
        match = route.pattern.fullmatch(request.path)
        if match is None:
            continue
        return route.handler
    return None


async def dispatch_rest(
    service: "ServerService", request: Request
) -> tuple[Optional[Response], Optional[WsHandler]]:
    """Return the JSON response (or ``None`` for unknown methods/paths)."""
    if request.method == "HEAD":
        return Response(405, headers={"Allow": ", ".join(sorted({r.method for r in _ROUTES}))}), None
    if request.method == "OPTIONS":
        return Response(200, headers={"Allow": ", ".join(sorted({r.method for r in _ROUTES}))}), None
    handler = _match(request)
    if handler is None:
        if any(r.pattern.fullmatch(request.path) for r in _ROUTES):
            return Response.json(405, {"error": "method not allowed"}), None
        return Response.json(404, {"error": "not found"}), None
    try:
        payload = await handler(service, request, {})
    except actions.ApiError as exc:
        return Response.json(exc.status, {"error": exc.message}), None
    except Exception as exc:  # noqa: BLE001
        service._log(f"unhandled error: {exc}")
        return Response.json(500, {"error": "internal error"}), None
    return Response.json(200, payload), None


def extract_params(route: _Route, request: Request) -> dict[str, str]:
    match = route.pattern.fullmatch(request.path)
    if match is None:
        return {}
    return match.groupdict()


def route_for(request: Request) -> Optional[_Route]:
    for route in _ROUTES:
        if route.method != request.method:
            continue
        if route.pattern.fullmatch(request.path) is not None:
            return route
    return None


def route_path_for(route: _Route, request: Request) -> dict[str, str]:
    match = route.pattern.fullmatch(request.path)
    return match.groupdict() if match is not None else {}


def find_route(method: str, path: str) -> Optional[tuple[_Route, dict[str, str]]]:
    for route in _ROUTES:
        if route.method != method:
            continue
        match = route.pattern.fullmatch(path)
        if match is not None:
            return route, match.groupdict()
    return None


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
