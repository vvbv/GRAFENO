"""Minimal HTTP/1.1 server built on asyncio streams (stdlib only).

Hand-rolled to avoid pulling a web framework into the dependency tree
(precedent: the Telegram bot also rides on stdlib urllib). The parser is
deliberately restrictive: GET / POST / DELETE / OPTIONS only, fixed
limits on the header and body sizes, no chunked transfer encoding, HTTP
keep-alive supported for HTTP/1.1.

The router returns a tuple ``(response, ws_handler)`` so the same socket
can be promoted to a WebSocket connection when the right path is hit
(see :mod:`server.ws`).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlsplit

MAX_HEAD = 32 * 1024      # hard cap on a single request head
MAX_BODY = 1 * 1024 * 1024  # hard cap on a single request body
HEAD_READ_TIMEOUT = 15.0  # seconds to receive the request head
REQUEST_TIMEOUT = 30.0    # seconds for any single read on the request

# Reasons indexed by HTTP status code (a tiny subset is enough for the API).
REASONS: dict[int, str] = {
    101: "Switching Protocols",
    200: "OK",
    201: "Created",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
}

ALLOWED_METHODS = ("GET", "POST", "DELETE", "OPTIONS")

Handler = Callable[["Request"], Awaitable["Response"]]
WsHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter, "Request"], Awaitable[None]]


@dataclass
class Request:
    """A parsed HTTP request."""

    method: str
    path: str
    query: dict[str, str]
    version: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Response:
    """An HTTP response to be serialized with :meth:`to_bytes`."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @classmethod
    def json(cls, status: int, data: object) -> "Response":
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return cls(status=status, headers={"Content-Type": "application/json"}, body=payload)

    def to_bytes(self, keep_alive: bool) -> bytes:
        """Serialize the response. Caller-supplied headers win over the
        defaults (``Content-Length`` always overrides; ``Connection`` is
        only filled when the caller did not specify one, so the 101
        handshake can keep its ``Connection: Upgrade`` value instead of
        being overwritten with ``close``)."""
        reason = REASONS.get(self.status, "Unknown")
        caller_headers = {key.lower(): value for key, value in self.headers.items()}
        merged: dict[str, str] = dict(self.headers)
        merged["Content-Length"] = str(len(self.body))
        if "connection" not in caller_headers:
            merged["Connection"] = "keep-alive" if keep_alive else "close"
        lines = [f"HTTP/1.1 {self.status} {reason}"]
        lines.extend(f"{key}: {value}" for key, value in merged.items())
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + self.body


class BadRequest(Exception):
    """The request was malformed (mapped to HTTP 400)."""


class HttpServer:
    """Async HTTP/1.1 server wrapping :func:`asyncio.start_server`."""

    def __init__(
        self,
        on_request: Callable[
            ["Request"],
            Awaitable[tuple[Optional[Response], Optional[WsHandler]]],
        ],
    ) -> None:
        self._on_request = on_request
        self._sockets: list[asyncio.Server] = []

    async def serve(self, host: str, port: int) -> tuple[asyncio.AbstractServer, int]:
        """Bind to ``host:port`` (port 0 = ephemeral) and start serving.

        Returns the underlying server and the resolved port.
        """
        server = await asyncio.start_server(self._handle_connection, host=host, port=port)
        self._sockets.append(server)
        sockets = server.sockets or ()
        resolved_port = sockets[0].getsockname()[1] if sockets else port
        return server, resolved_port

    def close(self) -> None:
        """Close every bound server. Safe to call multiple times."""
        for server in self._sockets:
            server.close()

    async def wait_closed(self) -> None:
        """Wait for every bound server to finish closing."""
        for server in self._sockets:
            await server.wait_closed()

    # ------------------------------------------------------------------ #
    # Connection loop
    # ------------------------------------------------------------------ #
    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                request = await _read_request(reader)
            except asyncio.IncompleteReadError:
                return  # client closed without sending a request
            except asyncio.TimeoutError:
                return  # nothing arrived in time
            except BadRequest as exc:
                await _safe_write(writer, Response(400, body=json.dumps({"error": str(exc)}).encode("utf-8")).to_bytes(keep_alive=False))
                return
            if request is None:
                return
            try:
                response, ws_handler = await self._on_request(request)
            except Exception as exc:  # noqa: BLE001 - last line of defense
                response = Response(500, body=json.dumps({"error": "internal error", "detail": str(exc)}).encode("utf-8"))
                ws_handler = None
            if response is None:
                # Router decided to handle the rest of the socket itself.
                return
            keep_alive = (
                request.version == "HTTP/1.1"
                and request.headers.get("connection", "").lower() != "close"
                and ws_handler is None
            )
            await _safe_write(writer, response.to_bytes(keep_alive=keep_alive))
            if ws_handler is not None:
                try:
                    await ws_handler(reader, writer, request)
                except Exception:  # noqa: BLE001
                    pass
                return
            if not keep_alive:
                return


async def _safe_write(writer: asyncio.StreamWriter, data: bytes) -> None:
    try:
        writer.write(data)
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        return


# ---------------------------------------------------------------------- #
# Request parsing
# ---------------------------------------------------------------------- #
async def _read_request(reader: asyncio.StreamReader) -> Request | None:
    try:
        raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_READ_TIMEOUT)
    except asyncio.LimitOverrunError as exc:
        raise BadRequest(f"head too large (>{MAX_HEAD} bytes)") from exc
    except asyncio.IncompleteReadError:
        return None
    if len(raw_head) > MAX_HEAD:
        raise BadRequest(f"head too large (>{MAX_HEAD} bytes)")
    if not raw_head:
        return None
    method, target, version = _parse_request_line(raw_head)
    headers = _parse_headers(raw_head)
    body = await _read_body(reader, headers)
    path, query = _split_target(target)
    return Request(method=method, path=path, query=query, version=version, headers=headers, body=body)


def _parse_request_line(raw_head: bytes) -> tuple[str, str, str]:
    head_text = raw_head.decode("iso-8859-1", errors="replace")
    line = head_text.split("\r\n", 1)[0]
    parts = line.split(" ")
    if len(parts) != 3:
        raise BadRequest("malformed request line")
    method, target, version = parts
    # Methods accepted by the parser: HEAD is rejected with 405 by the
    # router (no body), the rest are routed to a handler or 405/404.
    if method not in ALLOWED_METHODS and method != "HEAD":
        raise BadRequest(f"unsupported method: {method}")
    if not version.startswith("HTTP/"):
        raise BadRequest("malformed version")
    return method, target, version


def _parse_headers(raw_head: bytes) -> dict[str, str]:
    text = raw_head.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


async def _read_body(reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
    raw_length = headers.get("content-length")
    if raw_length is None:
        transfer = headers.get("transfer-encoding", "").lower()
        if transfer and transfer != "identity":
            raise BadRequest("chunked unsupported")
        return b""
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise BadRequest("invalid content-length") from exc
    if length < 0:
        raise BadRequest("negative content-length")
    if length > MAX_BODY:
        raise BadRequest(f"body too large (>{MAX_BODY} bytes)")
    if length == 0:
        return b""
    try:
        return await asyncio.wait_for(reader.readexactly(length), REQUEST_TIMEOUT)
    except asyncio.IncompleteReadError as exc:
        raise BadRequest("short body") from exc


def _split_target(target: str) -> tuple[str, dict[str, str]]:
    """Return (path, query) from a request-line target."""
    if "?" in target:
        path, _, raw_query = target.partition("?")
    else:
        path, raw_query = target, ""
    if not path.startswith("/"):
        raise BadRequest("invalid path")
    try:
        parsed = urlsplit(target)
    except ValueError as exc:
        raise BadRequest("invalid target") from exc
    query_pairs = parse_qs(parsed.query, keep_blank_values=True)
    query = {key: values[0] for key, values in query_pairs.items() if values}
    return path, query
