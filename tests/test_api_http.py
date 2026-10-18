"""Smoke tests of the HTTP server: hand-rolled client + the status endpoint."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Optional

from grafeno import __version__
from grafeno.config import ApiConfig
from grafeno.server.service import ServerService


def _run(coro):
    return asyncio.run(coro)


def _free_port() -> int:
    """Pick an unused TCP port (closed by the time the test starts)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


async def _read_response(reader: asyncio.StreamReader, n: int) -> bytes:
    return await asyncio.wait_for(reader.readexactly(n), timeout=5.0)


async def _exchange(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    raw_request: bytes,
) -> tuple[int, dict[str, str], bytes]:
    """Send one HTTP request and parse the response (status, headers, body)."""
    writer.write(raw_request)
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
    lines = head.decode("iso-8859-1").split("\r\n")
    status_line = lines[0]
    try:
        status = int(status_line.split(" ", 2)[1])
    except (IndexError, ValueError):
        raise AssertionError(f"malformed status line: {status_line!r}")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0") or "0")
    if length == 0:
        body = b""
    else:
        body = await _read_response(reader, length)
    return status, headers, body


async def _start_service(tokens: str = "t1", port: Optional[int] = None) -> tuple[ServerService, asyncio.Task]:
    cfg = ApiConfig(enabled=True, host="127.0.0.1", port=port or 0, tokens=tokens)
    service = ServerService(cfg, app=None)
    task = asyncio.create_task(service.run())
    for _ in range(200):
        if service.server is not None:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("server did not bind in time")
    await asyncio.sleep(0)
    return service, task


def _stop(service: ServerService, task: asyncio.Task) -> None:
    """Shut down the service: close the listening socket.

    The service task is cancelled; any pending cleanup is dropped because
    the test event loop is about to exit anyway. We close the underlying
    server first so the run() coroutine can return on its own (otherwise
    serve_forever() would block forever).
    """
    if service.http is not None:
        service.http.close()
    if not task.done():
        task.cancel()


def _build_request(method: str, path: str, headers: Optional[dict[str, str]] = None, body: bytes = b"") -> bytes:
    headers = headers or {}
    lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1"]
    if body:
        lines.append(f"Content-Length: {len(body)}")
    for key, value in headers.items():
        lines.append(f"{key}: {value}")
    head = "\r\n".join(lines) + "\r\n\r\n"
    return head.encode("ascii") + body


def test_status_without_token_returns_401() -> None:
    async def scenario():
        service, task = await _start_service()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                status, _, body = await _exchange(reader, writer, _build_request("GET", "/api/v1/status"))
                assert status == 401
                assert json.loads(body)["error"] == "unauthorized"
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, task)

    _run(scenario())


def test_status_with_bearer_token() -> None:
    async def scenario():
        service, task = await _start_service(tokens="t1")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                status, _, body = await _exchange(
                    reader, writer,
                    _build_request("GET", "/api/v1/status", {"Authorization": "Bearer t1"}),
                )
                assert status == 200
                payload = json.loads(body)
                assert payload["version"] == __version__
                assert payload["ws_path"] == "/api/v1/ws"
                assert payload["pid"] > 0
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, task)

    _run(scenario())


def test_status_with_query_token() -> None:
    async def scenario():
        service, task = await _start_service(tokens="t1")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                status, _, body = await _exchange(
                    reader, writer,
                    _build_request("GET", "/api/v1/status?token=t1"),
                )
                assert status == 200
                assert json.loads(body)["version"] == __version__
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, task)

    _run(scenario())


def test_unknown_path_returns_404() -> None:
    async def scenario():
        service, task = await _start_service()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                status, _, body = await _exchange(
                    reader, writer,
                    _build_request("GET", "/api/v1/missing", {"Authorization": "Bearer t1"}),
                )
                assert status == 404
                assert json.loads(body)["error"] == "not found"
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, task)

    _run(scenario())


def test_head_returns_405() -> None:
    async def scenario():
        service, task = await _start_service()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                status, _, _ = await _exchange(
                    reader, writer,
                    _build_request("HEAD", "/api/v1/status", {"Authorization": "Bearer t1"}),
                )
                assert status == 405
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, task)

    _run(scenario())


def test_keep_alive_two_requests() -> None:
    async def scenario():
        service, task = await _start_service()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                first = _build_request("GET", "/api/v1/status", {"Authorization": "Bearer t1"})
                status, headers, body = await _exchange(reader, writer, first)
                assert status == 200
                assert headers.get("connection") == "keep-alive"
                second = _build_request("GET", "/api/v1/status", {"Authorization": "Bearer t1"})
                status, _, body = await _exchange(reader, writer, second)
                assert status == 200
                assert json.loads(body)["version"] == __version__
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, task)

    _run(scenario())


def test_no_tokens_allows_all() -> None:
    async def scenario():
        service, task = await _start_service(tokens="")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                status, _, body = await _exchange(
                    reader, writer, _build_request("GET", "/api/v1/status")
                )
                assert status == 200
                assert json.loads(body)["version"] == __version__
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, task)

    _run(scenario())


def test_no_tokens_ws_independent_note() -> None:
    """Empty token set opens REST too: status reachable without any header."""
    async def scenario():
        service, task = await _start_service(tokens="")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                status, _, body = await _exchange(reader, writer, _build_request("GET", "/api/v1/status"))
                assert status == 200
                assert json.loads(body)["ws_path"] == "/api/v1/ws"
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, task)

    _run(scenario())


def test_body_policy_by_route() -> None:
    from grafeno import media
    from grafeno.server import httpcore

    json_headers = {"content-type": "application/json"}
    for path in ("/api/v1/tasks", "/api/v1/tasks/x/extend", "/api/v1/tasks/x/attachments"):
        policy = httpcore.body_policy("POST", path, json_headers)
        assert policy.limit == httpcore.MAX_UPLOAD_BODY
        assert policy.upload and not policy.spool
    raw = httpcore.body_policy(
        "POST", "/api/v1/tasks/x/attachments", {"content-type": "application/octet-stream"}
    )
    assert raw.limit == httpcore.MAX_RAW_UPLOAD_BODY and raw.spool and raw.upload
    for method, path in (("POST", "/api/v1/settings"), ("GET", "/api/v1/tasks")):
        policy = httpcore.body_policy(method, path, json_headers)
        assert policy.limit == httpcore.MAX_BODY and not policy.upload
    assert httpcore.MAX_UPLOAD_BODY >= media.base64_length(media.MAX_ATTACHMENT_BYTES)
    assert httpcore.MAX_RAW_UPLOAD_BODY == media.MAX_ATTACHMENT_BYTES


async def _head_only(service: ServerService, path: str, headers: dict[str, str]) -> int:
    """Send only a request head and return the status of the early answer."""
    reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
    try:
        lines = [f"POST {path} HTTP/1.1", "Host: 127.0.0.1"]
        lines.extend(f"{key}: {value}" for key, value in headers.items())
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        return int(head.decode("iso-8859-1").split(" ", 2)[1])
    finally:
        writer.close()
        await writer.wait_closed()


def test_upload_unauthorized_rejected_before_body() -> None:
    async def scenario():
        service, task = await _start_service()
        try:
            status = await _head_only(service, "/api/v1/tasks/x/attachments?name=a.zip", {
                "Content-Type": "application/octet-stream",
                "Content-Length": "600000000",
                "Authorization": "Bearer wrong",
            })
            assert status == 401
        finally:
            _stop(service, task)

    _run(scenario())


def test_upload_body_too_large_is_413() -> None:
    from grafeno.server import httpcore

    async def scenario():
        service, task = await _start_service()
        try:
            status = await _head_only(service, "/api/v1/tasks/x/attachments?name=a.zip", {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(httpcore.MAX_RAW_UPLOAD_BODY + 1),
                "Authorization": "Bearer t1",
            })
            assert status == 413
        finally:
            _stop(service, task)

    _run(scenario())


def test_regular_body_too_large_is_413() -> None:
    from grafeno.server import httpcore

    async def scenario():
        service, task = await _start_service()
        try:
            status = await _head_only(service, "/api/v1/settings", {
                "Content-Type": "application/json",
                "Content-Length": str(httpcore.MAX_BODY + 1),
                "Authorization": "Bearer t1",
            })
            assert status == 413
        finally:
            _stop(service, task)

    _run(scenario())


def test_expect_100_continue() -> None:
    from grafeno.config import Config
    from grafeno.models import Task
    from grafeno import models as models_module

    async def scenario():
        task = Task.create("Base", "desc", "/tmp", Config())
        models_module.save(task)
        service, srv = await _start_service()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                body = b"hello"
                head = (
                    f"POST /api/v1/tasks/{task.id}/attachments?name=a.zip HTTP/1.1\r\n"
                    "Host: 127.0.0.1\r\nAuthorization: Bearer t1\r\n"
                    "Content-Type: application/octet-stream\r\n"
                    f"Content-Length: {len(body)}\r\nExpect: 100-continue\r\n\r\n"
                )
                writer.write(head.encode("ascii"))
                await writer.drain()
                interim = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
                assert interim == b"HTTP/1.1 100 Continue\r\n\r\n"
                status, _, _ = await _exchange(reader, writer, body)
                assert status == 201
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv)

    _run(scenario())


def test_upload_idle_timeout_is_not_507(monkeypatch) -> None:
    from grafeno import paths
    from grafeno.server import httpcore

    monkeypatch.setattr(httpcore, "UPLOAD_IDLE_TIMEOUT", 0.2)

    async def scenario():
        service, task = await _start_service()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                writer.write((
                    "POST /api/v1/tasks/x/attachments?name=a.zip HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    "Authorization: Bearer t1\r\nContent-Type: application/octet-stream\r\n"
                    "Content-Length: 10\r\n\r\nabc"
                ).encode("ascii"))
                await writer.drain()
                data = await asyncio.wait_for(reader.read(), timeout=5.0)  # connection closes
                assert b"507" not in data
            finally:
                writer.close()
                await writer.wait_closed()
            assert not list(paths.uploads_dir().glob("*.part"))
        finally:
            _stop(service, task)

    _run(scenario())


def test_json_upload_buffer_grows_with_data() -> None:
    from grafeno.server import httpcore

    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(b"abcdef")
        reader.feed_eof()
        policy = httpcore.BodyPolicy(64 * 1024 * 1024, 1.0, upload=True)
        try:
            await httpcore._read_body(reader, {"content-length": str(64 * 1024 * 1024)}, policy)
        except httpcore.BadRequest:
            pass  # short body: nothing preallocated for the announced 64 MiB
        reader = asyncio.StreamReader()
        reader.feed_data(b"hello world")
        reader.feed_eof()
        body, path = await httpcore._read_body(reader, {"content-length": "11"}, policy)
        assert bytes(body) == b"hello world" and path is None

    _run(scenario())
