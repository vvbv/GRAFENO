"""End-to-end tests of the WebSocket endpoint (RFC 6455 frames by hand)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import struct
from typing import Optional

from grafeno import models as models_module
from grafeno.config import ApiConfig, Config
from grafeno.models import Task, TaskState
from grafeno.server.service import ServerService


def _run(coro):
    return asyncio.run(coro)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


async def _start_service() -> tuple[ServerService, asyncio.Task]:
    cfg = ApiConfig(enabled=True, host="127.0.0.1", port=0, tokens="t1")
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
    if service.http is not None:
        service.http.close()
    if not task.done():
        task.cancel()


def _build_upgrade(token: Optional[str] = "t1") -> bytes:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [
        "GET /api/v1/ws HTTP/1.1",
        "Host: 127.0.0.1",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if token:
        lines.append(f"Authorization: Bearer {token}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def _mask(payload: bytes) -> bytes:
    key = os.urandom(4)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return key + masked


def _client_text_frame(payload: bytes) -> bytes:
    """Build a client text frame (FIN + opcode 0x1, masked)."""
    length = len(payload)
    mask_bit = 0x80
    if length < 126:
        header = bytes([0x81, mask_bit | length])
    elif length < (1 << 16):
        header = bytes([0x81, mask_bit | 126]) + struct.pack(">H", length)
    else:
        header = bytes([0x81, mask_bit | 127]) + struct.pack(">Q", length)
    return header + _mask(payload)


async def _read_server_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Read one server-to-client frame (unmasked). Returns (opcode, payload)."""
    header = await asyncio.wait_for(reader.readexactly(2), timeout=5.0)
    byte0, byte1 = header[0], header[1]
    opcode = byte0 & 0x0F
    length = byte1 & 0x7F
    if length == 126:
        length = int.from_bytes(await asyncio.wait_for(reader.readexactly(2), timeout=5.0), "big")
    elif length == 127:
        length = int.from_bytes(await asyncio.wait_for(reader.readexactly(8), timeout=5.0), "big")
    payload = b""
    if length:
        payload = await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
    return opcode, payload


async def _ws_handshake(port: int, token: Optional[str] = "t1") -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_build_upgrade(token=token))
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
    status_line = head.decode("iso-8859-1").split("\r\n", 1)[0]
    status = int(status_line.split(" ", 2)[1])
    assert status == 101, f"expected 101 Switching Protocols, got {status}: {head!r}"
    return reader, writer


async def _rpc(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, payload: dict) -> dict:
    """Send one JSON-RPC frame and return the parsed reply (skips pongs)."""
    frame = _client_text_frame(json.dumps(payload).encode("utf-8"))
    writer.write(frame)
    await writer.drain()
    while True:
        opcode, body = await _read_server_frame(reader)
        if opcode == 0xA:  # pong
            continue
        assert opcode == 0x1, f"unexpected opcode: {opcode}"
        return json.loads(body.decode("utf-8"))


def test_handshake_without_token_returns_401() -> None:
    async def scenario():
        service, srv_task = await _start_service()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                writer.write(_build_upgrade(token=None))
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
                status = int(head.decode("iso-8859-1").split(" ", 2)[1])
                assert status == 401
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_handshake_with_token_returns_101() -> None:
    async def scenario():
        service, srv_task = await _start_service()
        try:
            reader, writer = await _ws_handshake(service.port)
            try:
                # Sanity: we get a JSON-RPC reply for the status method.
                reply = await _rpc(reader, writer, {"id": 1, "method": "status", "params": {}})
                assert reply["id"] == 1
                assert reply["result"]["version"]
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_tasks_list_returns_empty() -> None:
    async def scenario():
        service, srv_task = await _start_service()
        try:
            reader, writer = await _ws_handshake(service.port)
            try:
                reply = await _rpc(reader, writer, {"id": 1, "method": "tasks.list", "params": {}})
                assert "result" in reply, reply
                assert reply["result"] == {"tasks": []}
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv_task)
            if "internal error" in str(reply):
                from grafeno import paths
                print("LOG:", paths.api_log_path().read_text())

    _run(scenario())


def test_tasks_list_with_results(tmp_path) -> None:
    async def scenario():
        task = Task.create("Alpha", "d", str(tmp_path), Config())
        models_module.save(task)
        service, srv_task = await _start_service()
        try:
            reader, writer = await _ws_handshake(service.port)
            try:
                reply = await _rpc(reader, writer, {"id": "x", "method": "tasks.list", "params": {}})
                assert reply["id"] == "x"
                ids = [entry["id"] for entry in reply["result"]["tasks"]]
                assert ids == [task.id]
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_invalid_json_returns_error() -> None:
    async def scenario():
        service, srv_task = await _start_service()
        try:
            reader, writer = await _ws_handshake(service.port)
            try:
                writer.write(_client_text_frame(b"not-json"))
                await writer.drain()
                # Skip pongs/empty, expect a single text reply with an error.
                opcode, body = await _read_server_frame(reader)
                assert opcode == 0x1
                payload = json.loads(body.decode("utf-8"))
                assert payload["error"]["message"] == "invalid json"
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_ping_returns_pong_with_same_payload() -> None:
    async def scenario():
        service, srv_task = await _start_service()
        try:
            reader, writer = await _ws_handshake(service.port)
            try:
                # Build a client ping frame (FIN + opcode 0x9, masked).
                payload = b"ping-data"
                length = len(payload)
                header = bytes([0x89, 0x80 | length])
                writer.write(header + _mask(payload))
                await writer.drain()
                opcode, body = await _read_server_frame(reader)
                assert opcode == 0xA  # pong
                assert body == payload
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_subscribe_and_task_changed_event(tmp_path) -> None:
    async def scenario():
        service, srv_task = await _start_service()
        try:
            reader, writer = await _ws_handshake(service.port)
            try:
                reply = await _rpc(
                    reader, writer,
                    {"id": 1, "method": "subscribe", "params": {"topics": ["tasks"]}},
                )
                assert reply["result"]["topics"] == ["tasks"]

                # Create a task from another coroutine.
                await asyncio.sleep(0)
                task = Task.create("New", "desc", str(tmp_path), Config())
                models_module.save(task)
                # Wait for the events poll (POLL_SECONDS=2.0) to broadcast.
                # Drain up to 6 s for the event.
                deadline = asyncio.get_event_loop().time() + 6.0
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        opcode, body = await asyncio.wait_for(_read_server_frame(reader), timeout=2.0)
                    except asyncio.TimeoutError:
                        continue
                    if opcode == 0x1:
                        payload = json.loads(body.decode("utf-8"))
                        if payload.get("event") == "task.changed":
                            assert payload["task"]["id"] == task.id
                            return
                raise AssertionError("task.changed event not received within timeout")
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_unknown_method_returns_error() -> None:
    async def scenario():
        service, srv_task = await _start_service()
        try:
            reader, writer = await _ws_handshake(service.port)
            try:
                reply = await _rpc(
                    reader, writer,
                    {"id": 7, "method": "nonexistent.method", "params": {}},
                )
                assert reply["id"] == 7
                assert "unknown method" in reply["error"]["message"]
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv_task)

    _run(scenario())
