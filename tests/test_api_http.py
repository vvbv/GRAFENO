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


def test_no_tokens_denies_all() -> None:
    async def scenario():
        service, task = await _start_service(tokens="")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                status, _, body = await _exchange(
                    reader, writer,
                    _build_request("GET", "/api/v1/status", {"Authorization": "Bearer anything"}),
                )
                assert status == 401
                assert json.loads(body)["error"] == "unauthorized"
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, task)

    _run(scenario())
