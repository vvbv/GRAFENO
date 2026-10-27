"""WebSocket transport (RFC 6455) for the API server.

Implemented by hand on the stdlib asyncio streams. The handshake shares
the same token authentication as the REST endpoints (validated by
``server.auth.check`` before this module is invoked). Frames from client
to server MUST be masked (RFC); server-to-client frames are sent
unmasked. Text frames carry JSON messages.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from . import actions, branding, clisessions, clock, projectcreate, projectdocs, projectfiles, projectgit, reviews, settings
from .httpcore import Request, Response, WsHandler

if TYPE_CHECKING:
    from .service import ServerService

# WebSocket opcodes (RFC 6455 section 5.2).
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_PATH = "/api/v1/ws"

MAX_WS_PAYLOAD = 1 * 1024 * 1024  # 1 MiB per message; larger closes the socket
# WS attachments (tasks.create / tasks.attach) are bound by the cap above; big files use the raw REST upload.
WS_MEDIA_MAX_BYTES = 16 * 1024 * 1024  # tasks.media answers in one frame; larger files go through REST


def accept_key(key: str) -> str:
    """Return the ``Sec-WebSocket-Accept`` value for a client key."""
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def handshake_response(request: Request) -> Optional[Response]:
    """Build the 101 Switching Protocols response for ``request``.

    Returns ``None`` when the upgrade request is malformed (no Upgrade
    header or no Sec-WebSocket-Key); the router then answers with 400.
    """
    headers = {key.lower(): value for key, value in request.headers.items()}
    upgrade = headers.get("upgrade", "").lower()
    if "websocket" not in upgrade:
        return None
    key = headers.get("sec-websocket-key", "")
    if not key:
        return None
    accept = accept_key(key)
    return Response(
        101,
        headers={"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Accept": accept},
        body=b"",
    )


def write_frame(opcode: int, payload: bytes) -> bytes:
    """Serialize one server-to-client frame (unmasked).

    RFC 6455 §5.1: client frames MUST be masked; server frames MUST NOT be.
    The mask bit of the second byte is therefore always 0 here.
    """
    length = len(payload)
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))  # FIN=1
    if length < 126:
        header.append(length)  # mask=0, length fits in 7 bits
    elif length < (1 << 16):
        header.append(126)
        header.extend(struct.pack(">H", length))
    else:
        header.append(127)
        header.extend(struct.pack(">Q", length))
    return bytes(header) + payload


async def read_message(reader: asyncio.StreamReader) -> tuple[str, bytes] | None:
    """Return (kind, payload) where kind is "text", "ping", "pong" or "close".

    Accumulates fragmented frames until FIN. ``None`` means the
    connection is closed by the peer or the payload limit was exceeded.
    """
    opcode = OP_CONT
    payload = bytearray()
    while True:
        frame = await _read_frame(reader)
        if frame is None:
            return None
        frame_opcode, fin, frame_payload = frame
        if len(payload) + len(frame_payload) > MAX_WS_PAYLOAD:
            return None
        if frame_opcode == OP_PING:
            return "ping", frame_payload
        if frame_opcode == OP_PONG:
            return "pong", frame_payload
        if frame_opcode == OP_CLOSE:
            return "close", frame_payload
        if opcode == OP_CONT:
            opcode = frame_opcode
        payload.extend(frame_payload)
        if fin:
            if opcode == OP_TEXT:
                return "text", bytes(payload)
            if opcode == OP_BINARY:
                return "binary", bytes(payload)
            return None  # unknown opcode


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bool, bytes] | None:
    """Return (opcode, fin, payload) for a single client frame."""
    header = await _read_exact(reader, 2)
    if header is None:
        return None
    byte0, byte1 = header[0], header[1]
    fin = bool(byte0 & 0x80)
    op = byte0 & 0x0F
    masked = bool(byte1 & 0x80)
    length = byte1 & 0x7F
    if length == 126:
        ext = await _read_exact(reader, 2)
        if ext is None:
            return None
        length = int.from_bytes(ext, "big")
    elif length == 127:
        ext = await _read_exact(reader, 8)
        if ext is None:
            return None
        length = int.from_bytes(ext, "big")
    if not masked:
        return None  # client frames MUST be masked (RFC 6455 section 5.1)
    mask = await _read_exact(reader, 4)
    if mask is None:
        return None
    body = await _read_exact(reader, length)
    if body is None:
        return None
    return op, fin, bytes(b ^ mask[i % 4] for i, b in enumerate(body))


async def _read_exact(reader: asyncio.StreamReader, count: int) -> bytes | None:
    if count == 0:
        return b""
    try:
        return await reader.readexactly(count)
    except asyncio.IncompleteReadError:
        return None


@dataclass
class WsConnection:
    """Outbound side of an open WebSocket connection."""

    writer: asyncio.StreamWriter
    closed: bool = False

    async def send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            return
        try:
            self.writer.write(write_frame(opcode, payload))
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            self.closed = True

    async def send_json(self, obj: object) -> None:
        try:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            data = json.dumps({"error": "encoding"}).encode("utf-8")
        await self.send_frame(OP_TEXT, data)

    async def send_close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.writer.write(write_frame(OP_CLOSE, b""))
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return


async def dispatch_upgrade(
    service: "ServerService", request: Request
) -> tuple[Optional[Response], Optional[WsHandler]]:
    """Route a request to the REST or WebSocket handler.

    The REST router is the default for everything except the WebSocket
    path (``/api/v1/ws``), which only accepts GET upgrades.
    """
    from .rest import dispatch_rest

    if request.method == "GET" and request.path == WS_PATH:
        accept = handshake_response(request)
        if accept is None:
            return Response.json(400, {"error": "invalid websocket upgrade"}), None
        return accept, _ws_session(service)
    return await dispatch_rest(service, request)


def _ws_session(service: "ServerService") -> WsHandler:
    """Build the WebSocket handler bound to the running service."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: Request) -> None:
        conn = WsConnection(writer=writer)
        service.add_connection(conn)
        try:
            while True:
                message = await read_message(reader)
                if message is None:
                    break
                kind, payload = message
                if kind == "close":
                    break
                if kind == "ping":
                    await conn.send_frame(OP_PONG, payload)
                    continue
                if kind != "text":
                    continue
                response = await _dispatch_text(service, conn, payload)
                if response is not None:
                    await conn.send_json(response)
        finally:
            await conn.send_close()
            service.remove_connection(conn)

    return handler


async def _dispatch_text(service: "ServerService", conn: "WsConnection", payload: bytes) -> object | None:
    """Parse a text frame and produce the JSON-RPC reply (or None)."""
    from . import actions as actions_module  # local import to avoid cycles

    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"id": None, "error": {"message": "invalid json"}}
    if not isinstance(message, dict):
        return {"id": None, "error": {"message": "invalid json"}}
    rpc_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return {"id": rpc_id, "error": {"message": "params must be an object"}}
    handler = METHODS.get(method or "")
    if handler is None:
        return {"id": rpc_id, "error": {"message": f"unknown method: {method}"}}
    try:
        result = await handler(service, params, conn)
    except actions_module.ApiError as exc:
        error = {"message": exc.message, "status": exc.status}
        if exc.code:
            error["code"] = exc.code
        return {"id": rpc_id, "error": error}
    except Exception as exc:  # noqa: BLE001
        service._log(f"rpc error in {method}: {exc}")
        return {"id": rpc_id, "error": {"message": "internal error"}}
    return {"id": rpc_id, "result": result}


# ---------------------------------------------------------------------- #
# Method table — populated lazily so the import order stays flat.
# ---------------------------------------------------------------------- #
def _required_task_id(params: dict) -> str:
    """Extract a non-empty ``task_id`` string from the call params."""
    task_id = params.get("task_id")
    if not task_id or not isinstance(task_id, str):
        raise actions.ApiError(400, "task_id is required")
    return task_id


def _parse_int(value: object, default: int, *, field: str) -> int:
    """Parse an int param (or fall back to ``default``). Raises ``ApiError``
    when the value is present but not an integer-compatible string."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise actions.ApiError(400, f"{field} must be an integer") from exc


async def _status_method(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    import time as _time

    uptime = int(_time.monotonic() - service.start_monotonic) if service.start_monotonic else 0
    return {
        "version": service.version,
        "uptime_seconds": uptime,
        "pid": service.pid,
        "ws_path": "/api/v1/ws",
        "instance_name": branding.instance_name(),
    }


async def _time_method(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return clock.server_time()


async def _tasks_list(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    def text(field: str) -> str | None:
        value = params.get(field)
        if field == "state" and isinstance(value, list):
            value = ",".join(map(str, value))
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise actions.ApiError(400, f"{field} must be a string")
        return value

    limit = params.get("limit")
    return actions.list_tasks(
        service,
        state=text("state"),
        workdir=text("workdir"),
        project=text("project"),
        sort=text("sort"),
        order=text("order"),
        limit=None if limit in (None, "") else _parse_int(limit, 0, field="limit"),
        offset=_parse_int(params.get("offset"), 0, field="offset"),
    )


async def _tasks_get(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.get_task(service, _required_task_id(params))


async def _projects_list(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.list_projects(service)


async def _projects_create(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return (await projectcreate.create_project(service, params))[1]


async def _projects_docs(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return projectdocs.list_docs(
        service,
        workdir=str(params.get("workdir") or ""),
        project=str(params.get("project") or ""),
    )


async def _projects_doc(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    max_chars = params.get("max_chars")
    return projectdocs.read_doc(
        service,
        workdir=str(params.get("workdir") or ""),
        project=str(params.get("project") or ""),
        name=str(params.get("name") or ""),
        offset=_parse_int(params.get("offset"), 0, field="offset"),
        max_chars=None if max_chars in (None, "") else _parse_int(max_chars, 0, field="max_chars"),
    )


async def _projects_files(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    limit = params.get("limit")
    return projectfiles.list_dir(
        service,
        workdir=str(params.get("workdir") or ""),
        project=str(params.get("project") or ""),
        path=str(params.get("path") or ""),
        hidden=actions._coerce_bool(params.get("hidden"), False),
        limit=None if limit in (None, "") else _parse_int(limit, 0, field="limit"),
        offset=_parse_int(params.get("offset"), 0, field="offset"),
    )


async def _projects_file(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return projectfiles.file_info(
        service, workdir=str(params.get("workdir") or ""),
        project=str(params.get("project") or ""), path=str(params.get("path") or ""),
    )


async def _projects_file_text(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    max_lines = params.get("max_lines")
    return projectfiles.read_text(
        service,
        workdir=str(params.get("workdir") or ""),
        project=str(params.get("project") or ""),
        path=str(params.get("path") or ""),
        line=_parse_int(params.get("line"), 1, field="line"),
        max_lines=None if max_lines in (None, "") else _parse_int(max_lines, 0, field="max_lines"),
    )


async def _projects_file_raw(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    import base64

    target, data = projectfiles.read_raw(
        service, workdir=str(params.get("workdir") or ""),
        project=str(params.get("project") or ""), path=str(params.get("path") or ""),
        max_bytes=WS_MEDIA_MAX_BYTES,
    )
    return {
        "path": target.rel, "name": target.name, "mime": target.mime, "size": target.size,
        "etag": target.etag, "data": base64.b64encode(data).decode("ascii"),
    }


async def _tasks_logs(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    task_id = _required_task_id(params)
    limit = _parse_int(params.get("limit"), 200, field="limit")
    return actions.get_logs(service, task_id, limit=limit)


async def _tasks_artifacts(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    task_id = _required_task_id(params)
    kind = params.get("kind")
    if not kind or not isinstance(kind, str):
        raise actions.ApiError(400, "kind is required")
    cycle = _parse_int(params.get("cycle"), 1, field="cycle")
    return actions.get_artifacts(service, task_id, kind=kind, cycle=cycle)


async def _tasks_create(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    result = await actions.create_task(service, params)
    if isinstance(result, tuple):
        return result[1]
    return result


async def _tasks_start(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.start_task(service, _required_task_id(params))


async def _tasks_resume(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.resume_task(service, _required_task_id(params))


async def _tasks_restart(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await actions.restart_task(service, _required_task_id(params))


async def _tasks_reset(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await actions.reset_task(service, _required_task_id(params))


async def _tasks_run(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.run_phase(service, _required_task_id(params), str(params.get("phase") or ""))


async def _tasks_continue(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.continue_task(service, _required_task_id(params))


async def _tasks_approve_plan(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.approve_plan(service, _required_task_id(params))


async def _tasks_edit(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.edit_task(service, _required_task_id(params), params)


async def _tasks_roles(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.update_roles(service, _required_task_id(params), params)


async def _tasks_artifact(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.get_artifact_file(
        service, _required_task_id(params),
        kind=str(params.get("kind") or ""), path=str(params.get("path") or ""),
    )


async def _tasks_media(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    import base64

    name = str(params.get("name") or "")
    data, mime = actions.get_media(service, _required_task_id(params), name, max_bytes=WS_MEDIA_MAX_BYTES)
    return {"name": name, "mime": mime, "data": base64.b64encode(data).decode("ascii")}


async def _tasks_attachments(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.list_task_attachments(service, _required_task_id(params))


async def _tasks_attach(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    task_id = _required_task_id(params)
    attachments = actions._decode_attachments(params)
    _, payload = actions.upload_attachments(service, task_id, attachments)
    return payload


async def _tasks_detach(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.delete_task_attachment(
        service, _required_task_id(params), str(params.get("file") or "")
    )


def _required_session_id(params: dict) -> str:
    """Extract a non-empty ``session_id`` string from the call params."""
    session_id = params.get("session_id")
    if not session_id or not isinstance(session_id, str):
        raise actions.ApiError(400, "session_id is required")
    return session_id


async def _sessions_list(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return clisessions.list_sessions(service, workdir=str(params.get("workdir") or ""))


async def _sessions_get(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return clisessions.get_session(service, _required_session_id(params))


async def _sessions_transcript(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return clisessions.get_transcript(
        service, _required_session_id(params),
        after=_parse_int(params.get("after"), 0, field="after"),
        limit=_parse_int(params.get("limit"), 0, field="limit"),
    )


async def _sessions_create(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return clisessions.create_session(service, params)[1]


async def _sessions_send(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return clisessions.send_prompt(
        service, _required_session_id(params), str(params.get("prompt") or "")
    )


async def _sessions_cancel(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return clisessions.cancel_session(service, _required_session_id(params))


async def _sessions_edit(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return clisessions.edit_session(service, _required_session_id(params), params)


async def _sessions_close(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return clisessions.close_session(service, _required_session_id(params))


async def _sessions_reopen(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return clisessions.reopen_session(service, _required_session_id(params))


async def _sessions_delete(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await clisessions.delete_session(service, _required_session_id(params))


async def _options_get(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return settings.form_options(service)


async def _models_list(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await settings.models_catalog(service, refresh=bool(params.get("refresh")))


async def _fs_dirs(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return settings.list_dirs(service, str(params.get("path") or ""))


async def _issues_list(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await settings.list_issues(service, str(params.get("workdir") or ""))


async def _reports_get(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return settings.usage_report(
        service,
        start=str(params.get("from") or ""),
        end=str(params.get("to") or ""),
        period=str(params.get("period") or ""),
    )


async def _settings_get(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return settings.get_settings(service)


async def _settings_update(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return settings.update_settings(service, params)


async def _tasks_pause(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.pause_task(service, _required_task_id(params))


async def _tasks_discard(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await actions.discard_task(service, _required_task_id(params))


async def _tasks_mark_done(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.mark_done(service, _required_task_id(params))


async def _tasks_extend(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    request = params.get("request")
    if not request or not isinstance(request, str):
        raise actions.ApiError(400, "request is required")
    attachments = actions._decode_attachments(params)
    return actions.extend_task(
        service, _required_task_id(params), request=request, attachments=attachments
    )


async def _subscribe(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    topics = params.get("topics") or []
    if not isinstance(topics, list) or not all(isinstance(item, str) for item in topics):
        raise actions.ApiError(400, "topics must be a list of strings")
    service.set_topics(conn, set(topics))
    return {"topics": sorted(service.topics_of(conn))}


async def _unsubscribe(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    service.set_topics(conn, set())
    return {"topics": []}


def _required_review_id(params: dict) -> str:
    """Extract a non-empty ``review_id`` string from the call params."""
    review_id = params.get("review_id")
    if not review_id or not isinstance(review_id, str):
        raise actions.ApiError(400, "review_id is required")
    return review_id


def _text_param(params: dict, field: str) -> str:
    value = params.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise actions.ApiError(400, f"{field} must be a string")
    return value


async def _projects_repos(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await reviews.list_repos(
        service, workdir=_text_param(params, "workdir"), project=_text_param(params, "project")
    )


async def _projects_changes(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await projectgit.list_changes(
        service, workdir=_text_param(params, "workdir"), project=_text_param(params, "project"),
        refresh=actions._coerce_bool(params.get("refresh"), False),
    )


async def _projects_branches(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await projectgit.list_branches(service, params)


async def _projects_push(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await projectgit.push(service, params)


async def _projects_pull(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await projectgit.pull(service, params)


async def _projects_checkout(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await projectgit.checkout(service, params)


async def _projects_abort(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await projectgit.abort(service, params)


async def _reviews_list(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return reviews.list_reviews(
        service, workdir=_text_param(params, "workdir"), state=_text_param(params, "state")
    )


async def _reviews_start(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    status, body = await reviews.start_review(service, params)
    return body


async def _reviews_get(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await reviews.get_review(service, _required_review_id(params))


async def _reviews_delete(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return reviews.delete_review(service, _required_review_id(params))


async def _reviews_close(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await reviews.close_review(service, _required_review_id(params))


async def _reviews_reopen(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await reviews.reopen_review(service, _required_review_id(params))


async def _reviews_files(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await reviews.list_files(
        service, _required_review_id(params), repo=_text_param(params, "repo"),
        decision=_text_param(params, "decision"),
    )


async def _reviews_diff(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await reviews.get_diff(service, _required_review_id(params), params)


async def _reviews_navigate(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await reviews.navigate(service, _required_review_id(params), params)


async def _reviews_decide(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await reviews.decide(service, _required_review_id(params), params)


async def _reviews_discuss(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await reviews.discuss(service, _required_review_id(params), params)


async def _reviews_request_changes(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await reviews.request_changes(service, _required_review_id(params), params)


async def _reviews_transcript(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return reviews.review_transcript(
        service, _required_review_id(params),
        after=_parse_int(params.get("after"), 0, field="after"),
        limit=_parse_int(params.get("limit"), 0, field="limit"),
    )


async def _reviews_cancel(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return reviews.cancel_review_turn(service, _required_review_id(params))


async def _reviews_commit_message(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await reviews.commit_message(service, _required_review_id(params), params)


async def _reviews_commit(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return await reviews.commit(service, _required_review_id(params), params)


async def _reviews_commits(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return reviews.list_commits(service, _required_review_id(params))


METHODS: dict[str, Callable] = {
    "status": _status_method,
    "time": _time_method,
    "tasks.list": _tasks_list,
    "tasks.get": _tasks_get,
    "projects.list": _projects_list,
    "projects.create": _projects_create,
    "projects.docs": _projects_docs,
    "projects.doc": _projects_doc,
    "projects.files": _projects_files,
    "projects.file": _projects_file,
    "projects.file_text": _projects_file_text,
    "projects.file_raw": _projects_file_raw,
    "tasks.logs": _tasks_logs,
    "tasks.artifacts": _tasks_artifacts,
    "tasks.create": _tasks_create,
    "tasks.start": _tasks_start,
    "tasks.resume": _tasks_resume,
    "tasks.restart": _tasks_restart,
    "tasks.pause": _tasks_pause,
    "tasks.discard": _tasks_discard,
    "tasks.mark_done": _tasks_mark_done,
    "tasks.extend": _tasks_extend,
    "tasks.reset": _tasks_reset,
    "tasks.run": _tasks_run,
    "tasks.continue": _tasks_continue,
    "tasks.approve_plan": _tasks_approve_plan,
    "tasks.edit": _tasks_edit,
    "tasks.roles": _tasks_roles,
    "tasks.artifact": _tasks_artifact,
    "tasks.media": _tasks_media,
    "tasks.attachments": _tasks_attachments,
    "tasks.attach": _tasks_attach,
    "tasks.detach": _tasks_detach,
    "sessions.list": _sessions_list,
    "sessions.get": _sessions_get,
    "sessions.transcript": _sessions_transcript,
    "sessions.create": _sessions_create,
    "sessions.send": _sessions_send,
    "sessions.cancel": _sessions_cancel,
    "sessions.edit": _sessions_edit,
    "sessions.close": _sessions_close,
    "sessions.reopen": _sessions_reopen,
    "sessions.delete": _sessions_delete,
    "projects.repos": _projects_repos,
    "projects.changes": _projects_changes,
    "projects.branches": _projects_branches,
    "projects.push": _projects_push,
    "projects.pull": _projects_pull,
    "projects.checkout": _projects_checkout,
    "projects.abort": _projects_abort,
    "reviews.list": _reviews_list,
    "reviews.start": _reviews_start,
    "reviews.get": _reviews_get,
    "reviews.delete": _reviews_delete,
    "reviews.close": _reviews_close,
    "reviews.reopen": _reviews_reopen,
    "reviews.files": _reviews_files,
    "reviews.diff": _reviews_diff,
    "reviews.navigate": _reviews_navigate,
    "reviews.decide": _reviews_decide,
    "reviews.discuss": _reviews_discuss,
    "reviews.request_changes": _reviews_request_changes,
    "reviews.transcript": _reviews_transcript,
    "reviews.cancel": _reviews_cancel,
    "reviews.commit_message": _reviews_commit_message,
    "reviews.commit": _reviews_commit,
    "reviews.commits": _reviews_commits,
    "options.get": _options_get,
    "models.list": _models_list,
    "fs.dirs": _fs_dirs,
    "issues.list": _issues_list,
    "reports.get": _reports_get,
    "settings.get": _settings_get,
    "settings.update": _settings_update,
    "subscribe": _subscribe,
    "unsubscribe": _unsubscribe,
}
