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

from . import actions
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
        return {"id": rpc_id, "error": {"message": exc.message, "status": exc.status}}
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
    }


async def _tasks_list(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.list_tasks(service, state=params.get("state") or None)


async def _tasks_get(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.get_task(service, _required_task_id(params))


async def _projects_list(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.list_projects(service)


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
    result = actions.create_task(service, params)
    if isinstance(result, tuple):
        return result[1]
    return result


async def _tasks_start(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.start_task(service, _required_task_id(params))


async def _tasks_resume(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.resume_task(service, _required_task_id(params))


async def _tasks_restart(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.restart_task(service, _required_task_id(params))


async def _tasks_pause(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.pause_task(service, _required_task_id(params))


async def _tasks_discard(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.discard_task(service, _required_task_id(params))


async def _tasks_mark_done(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    return actions.mark_done(service, _required_task_id(params))


async def _tasks_extend(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    request = params.get("request")
    if not request or not isinstance(request, str):
        raise actions.ApiError(400, "request is required")
    return actions.extend_task(service, _required_task_id(params), request=request)


async def _subscribe(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    topics = params.get("topics") or []
    if not isinstance(topics, list) or not all(isinstance(item, str) for item in topics):
        raise actions.ApiError(400, "topics must be a list of strings")
    service.set_topics(conn, set(topics))
    return {"topics": sorted(service.topics_of(conn))}


async def _unsubscribe(service: "ServerService", params: dict, conn: "WsConnection") -> dict:
    service.set_topics(conn, set())
    return {"topics": []}


METHODS: dict[str, Callable] = {
    "status": _status_method,
    "tasks.list": _tasks_list,
    "tasks.get": _tasks_get,
    "projects.list": _projects_list,
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
    "subscribe": _subscribe,
    "unsubscribe": _unsubscribe,
}
