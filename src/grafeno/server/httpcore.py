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
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from .. import media

MAX_HEAD = 32 * 1024      # hard cap on a single request head
MAX_BODY = 8 * 1024 * 1024  # hard cap on a single request body (regular requests)
MAX_UPLOAD_BODY = media.base64_length(media.MAX_ATTACHMENT_BYTES) + 1024 * 1024  # JSON+base64: one max file
MAX_RAW_UPLOAD_BODY = media.MAX_ATTACHMENT_BYTES  # raw file body of POST .../attachments
UPLOAD_IDLE_TIMEOUT = 60.0  # seconds without receiving upload bytes
READ_CHUNK = 1024 * 1024    # bytes per read/write when streaming bodies and files
_UPLOAD_PATH = re.compile(r"/api/v1/tasks(?:/[^/]+/(?:extend|attachments))?")
_RAW_UPLOAD_PATH = re.compile(r"/api/v1/tasks/[^/]+/attachments")
HEAD_READ_TIMEOUT = 15.0  # seconds to receive the request head
REQUEST_TIMEOUT = 30.0    # seconds for any single read on the request

# Reasons indexed by HTTP status code (a tiny subset is enough for the API).
REASONS: dict[int, str] = {
    100: "Continue",
    101: "Switching Protocols",
    200: "OK",
    201: "Created",
    204: "No Content",
    206: "Partial Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    416: "Range Not Satisfiable",
    422: "Unprocessable Content",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
    507: "Insufficient Storage",
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
    body_path: Optional[Path] = None  # spooled upload body (raw attachment)


@dataclass
class Response:
    """An HTTP response to be serialized with :meth:`to_bytes`."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    file: Optional[Path] = None  # streamed from disk after the head (body must be empty)
    file_offset: int = 0                 # first byte of ``file`` to send (Range responses)
    file_length: Optional[int] = None    # bytes of ``file`` to send; None = up to its end
    _file_length: int = field(default=0, init=False, repr=False)

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
        if self.file is not None:
            if self.file_length is not None:
                self._file_length = self.file_length
            else:
                self._file_length = max(self.file.stat().st_size - self.file_offset, 0)
            merged["Content-Length"] = str(self._file_length)
        else:
            merged["Content-Length"] = str(len(self.body))
        if "connection" not in caller_headers:
            merged["Connection"] = "keep-alive" if keep_alive else "close"
        lines = [f"HTTP/1.1 {self.status} {reason}"]
        lines.extend(f"{key}: {value}" for key, value in merged.items())
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + self.body


class BadRequest(Exception):
    """The request was malformed (mapped to HTTP 400)."""


class _SpoolWriteError(Exception):
    """Writing the spooled upload to disk failed (mapped to HTTP 507)."""


class EarlyResponse(Exception):
    """Answer sent before (or instead of) reading the body; the connection closes."""

    def __init__(self, response: "Response") -> None:
        super().__init__(response.status)
        self.response = response


class HttpServer:
    """Async HTTP/1.1 server wrapping :func:`asyncio.start_server`."""

    def __init__(
        self,
        on_request: Callable[
            ["Request"],
            Awaitable[tuple[Optional[Response], Optional[WsHandler]]],
        ],
        authorize: Callable[["Request"], Optional[Response]] | None = None,
        spool_dir: Callable[[], Path] | None = None,
    ) -> None:
        self._on_request = on_request
        self._authorize = authorize
        self._spool_dir = spool_dir
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
                request = await _read_request(reader, writer, self._authorize, self._spool_dir)
            except asyncio.IncompleteReadError:
                return  # client closed without sending a request
            except asyncio.TimeoutError:
                return  # nothing arrived in time
            except EarlyResponse as exc:
                await _safe_write(writer, exc.response.to_bytes(keep_alive=False))
                return
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
            finally:
                if request.body_path is not None:  # moved into media/ by the action when it succeeded
                    request.body_path.unlink(missing_ok=True)
            if response is None:
                # Router decided to handle the rest of the socket itself.
                return
            keep_alive = (
                request.version == "HTTP/1.1"
                and request.headers.get("connection", "").lower() != "close"
                and ws_handler is None
            )
            await _safe_write(writer, response.to_bytes(keep_alive=keep_alive))
            if response.file is not None:
                if not await _stream_file(writer, response.file, response._file_length, response.file_offset):
                    return
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


async def _stream_file(
    writer: asyncio.StreamWriter, path: Path, length: int, offset: int = 0
) -> bool:
    """Send exactly ``length`` bytes of ``path`` starting at ``offset`` in READ_CHUNK blocks (False if it shrank or failed)."""
    try:
        with open(path, "rb") as handle:
            if offset:
                handle.seek(offset)
            remaining = length
            while remaining:
                chunk = handle.read(min(READ_CHUNK, remaining))
                if not chunk:
                    return False
                writer.write(chunk)
                await writer.drain()
                remaining -= len(chunk)
    except (OSError, ConnectionResetError, BrokenPipeError):
        return False
    return True


# ---------------------------------------------------------------------- #
# Request parsing
# ---------------------------------------------------------------------- #
async def _read_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None = None,
    authorize: Callable[["Request"], Optional["Response"]] | None = None,
    spool_dir: Callable[[], Path] | None = None,
) -> Request | None:
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
    path, query = _split_target(target)
    policy = body_policy(method, path, headers)
    if policy.upload and authorize is not None:
        # Upload routes are authorized before their (large) body is read.
        denied = authorize(Request(method, path, query, version, headers, b""))
        if denied is not None:
            raise EarlyResponse(denied)
    body, body_path = await _read_body(reader, headers, policy, writer, spool_dir)
    return Request(
        method=method, path=path, query=query, version=version,
        headers=headers, body=body, body_path=body_path,
    )


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


@dataclass(frozen=True)
class BodyPolicy:
    """How to receive a request body."""

    limit: int            # max bytes (larger -> 413)
    timeout: float        # seconds per read
    upload: bool = False  # file-carrying route: authorized before the body is read
    spool: bool = False   # stream into a temp file (Request.body_path) instead of memory


def body_policy(method: str, path: str, headers: dict[str, str]) -> BodyPolicy:
    """Body policy of a request (only the upload routes get the large caps).

    Raw (non-JSON) bodies of ``POST .../attachments`` are spooled to disk; the
    JSON+base64 upload routes are read in memory. Both are authorized before
    the body is read, which is why the large caps stay limited to them.
    """
    if method == "POST":
        content_type = headers.get("content-type", "").lower()
        if _RAW_UPLOAD_PATH.fullmatch(path) and not content_type.startswith("application/json"):
            return BodyPolicy(MAX_RAW_UPLOAD_BODY, UPLOAD_IDLE_TIMEOUT, upload=True, spool=True)
        if _UPLOAD_PATH.fullmatch(path):
            return BodyPolicy(MAX_UPLOAD_BODY, UPLOAD_IDLE_TIMEOUT, upload=True)
    return BodyPolicy(MAX_BODY, REQUEST_TIMEOUT)


async def _read_chunks(
    reader: asyncio.StreamReader, length: int, timeout: float, sink: Callable[[bytes], object]
) -> None:
    """Feed ``length`` bytes to ``sink`` in READ_CHUNK blocks (``timeout`` is per block)."""
    remaining = length
    while remaining:
        chunk = await asyncio.wait_for(reader.read(min(READ_CHUNK, remaining)), timeout)
        if not chunk:
            raise BadRequest("short body")
        sink(chunk)
        remaining -= len(chunk)


async def _read_body(
    reader: asyncio.StreamReader,
    headers: dict[str, str],
    policy: BodyPolicy,
    writer: asyncio.StreamWriter | None = None,
    spool_dir: Callable[[], Path] | None = None,
) -> tuple[bytes, Path | None]:
    raw_length = headers.get("content-length")
    if raw_length is None:
        transfer = headers.get("transfer-encoding", "").lower()
        if transfer and transfer != "identity":
            raise BadRequest("chunked unsupported")
        return b"", None
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise BadRequest("invalid content-length") from exc
    if length < 0:
        raise BadRequest("negative content-length")
    if length > policy.limit:
        raise EarlyResponse(Response.json(413, {"error": f"body too large (>{policy.limit} bytes)"}))
    if length == 0:
        return b"", None
    directory: Path | None = None
    if policy.spool:
        directory = spool_dir() if spool_dir else Path(tempfile.gettempdir())
        if not media.has_free_space(directory, length):
            raise EarlyResponse(Response.json(507, {"error": "insufficient storage"}))
    if policy.upload and writer is not None and headers.get("expect", "").lower() == "100-continue":
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        await writer.drain()
    if not policy.upload:
        try:
            return await asyncio.wait_for(reader.readexactly(length), policy.timeout), None
        except asyncio.IncompleteReadError as exc:
            raise BadRequest("short body") from exc
    if directory is None:  # JSON+base64 upload: the buffer grows with the data actually received
        buf = bytearray()  # never preallocated from the announced length (a bare header must cost nothing)
        await _read_chunks(reader, length, policy.timeout, buf.extend)
        return buf, None
    handle = tempfile.NamedTemporaryFile(dir=directory, prefix="upload-", suffix=".part", delete=False)
    spooled = Path(handle.name)

    def write(chunk: bytes) -> None:
        try:
            handle.write(chunk)
        except OSError as exc:  # only disk write failures mean "no space"; timeouts and resets are OSErrors too
            raise _SpoolWriteError() from exc

    try:
        with handle:
            await _read_chunks(reader, length, policy.timeout, write)
    except _SpoolWriteError as exc:
        spooled.unlink(missing_ok=True)
        raise EarlyResponse(Response.json(507, {"error": "insufficient storage"})) from exc
    except BaseException:
        spooled.unlink(missing_ok=True)
        raise
    return b"", spooled


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
