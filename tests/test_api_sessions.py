"""Tests of the direct CLI session REST/WS endpoints (fake driver)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from grafeno.clisession_runtime import CliSessionManager
from grafeno.config import ApiConfig
from grafeno.drivers.base import CLIDriver, EventKind, RunEvent, RunResult, TokenUsage
from grafeno.server.service import ServerService
from test_api_rest_write import _request, _stop
from test_api_ws import _rpc, _ws_handshake


class FakeSessionDriver(CLIDriver):
    name = "claude"
    display_name = "Fake Claude"
    executable = "fake"

    def __init__(self, delay: float = 0.0):
        self.delay = delay

    def is_available(self) -> bool:
        return True

    async def run(self, request, on_event=None, on_activity=None, on_session=None):
        if on_session:
            on_session("native-1")
        if on_event:
            on_event(RunEvent(EventKind.TEXT, f"echo: {request.prompt}"))
        if self.delay:
            await asyncio.sleep(self.delay)
        return RunResult(ok=True, session_id="native-1", tokens=TokenUsage(3, 2))


class FakeApp:
    def __init__(self, delay: float = 0.0) -> None:
        self.cli_sessions = CliSessionManager(driver_factory=lambda cli: FakeSessionDriver(delay))

    def notify(self, message: str, **_: Any) -> None:
        pass


async def _start(app: Optional[FakeApp]) -> tuple[ServerService, asyncio.Task]:
    service = ServerService(ApiConfig(enabled=True, host="127.0.0.1", port=0, tokens="t1"), app=app)
    task = asyncio.create_task(service.run())
    for _ in range(200):
        if service.server is not None:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("server did not bind in time")
    return service, task


def _body(**payload: Any) -> bytes:
    return json.dumps(payload).encode()


async def _create(service, tmp_path, **extra) -> dict:
    status, payload = await _request(
        service, "POST", "/api/v1/sessions",
        _body(workdir=str(tmp_path), cli="claude", **extra),
    )
    assert status == 201, payload
    return payload["session"]


async def _idle(service, session_id: str) -> None:
    for _ in range(200):
        _, payload = await _request(service, "GET", f"/api/v1/sessions/{session_id}")
        if not payload["session"]["running"]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("session still running")


def _run(coro):
    return asyncio.run(coro)


def test_create_with_prompt_and_incremental_transcript(tmp_path) -> None:
    async def scenario():
        service, srv = await _start(FakeApp())
        try:
            session = await _create(service, tmp_path, prompt="hello")
            await _idle(service, session["id"])
            status, data = await _request(service, "GET", f"/api/v1/sessions/{session['id']}/transcript")
            assert status == 200
            kinds = [e["kind"] for e in data["entries"]]
            assert "user" in kinds and "text" in kinds
            assert data["total"] == len(data["entries"])
            assert data["session"]["turns"] == 1
            _, tail = await _request(
                service, "GET", f"/api/v1/sessions/{session['id']}/transcript?after={data['total']}"
            )
            assert tail["entries"] == []
        finally:
            _stop(service, srv)

    _run(scenario())


def test_send_while_running_is_conflict(tmp_path) -> None:
    async def scenario():
        service, srv = await _start(FakeApp(delay=5))
        try:
            session = await _create(service, tmp_path, prompt="one")
            status, _ = await _request(
                service, "POST", f"/api/v1/sessions/{session['id']}/send", _body(prompt="two")
            )
            assert status == 409
            status, _ = await _request(service, "POST", f"/api/v1/sessions/{session['id']}/cancel")
            assert status == 200
            await _idle(service, session["id"])
            status, _ = await _request(service, "POST", f"/api/v1/sessions/{session['id']}/cancel")
            assert status == 409
        finally:
            _stop(service, srv)

    _run(scenario())


def test_close_reopen_edit_delete(tmp_path) -> None:
    async def scenario():
        service, srv = await _start(FakeApp())
        try:
            session = await _create(service, tmp_path)
            base = f"/api/v1/sessions/{session['id']}"
            assert (await _request(service, "POST", base + "/close"))[0] == 200
            assert (await _request(service, "POST", base + "/send", _body(prompt="x")))[0] == 409
            assert (await _request(service, "POST", base + "/reopen"))[0] == 200
            assert (await _request(service, "POST", base + "/send", _body(prompt="x")))[0] == 200
            await _idle(service, session["id"])
            assert (await _request(service, "POST", base + "/edit", _body(name="")))[0] == 400
            status, payload = await _request(service, "POST", base + "/edit", _body(name="Renamed"))
            assert status == 200 and payload["session"]["name"] == "Renamed"
            assert (await _request(service, "DELETE", base))[0] == 200
            assert (await _request(service, "GET", base))[0] == 404
        finally:
            _stop(service, srv)

    _run(scenario())


def test_list_filters_by_workdir_and_validates_input(tmp_path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()

    async def scenario():
        service, srv = await _start(FakeApp())
        try:
            await _create(service, one)
            await _create(service, two)
            _, everything = await _request(service, "GET", "/api/v1/sessions")
            assert len(everything["sessions"]) == 2
            assert everything["available"] is True and "claude" in everything["clis"]
            _, only = await _request(service, "GET", f"/api/v1/sessions?workdir={one}")
            assert [s["project_name"] for s in only["sessions"]] == ["one"]
            status, _ = await _request(
                service, "POST", "/api/v1/sessions", _body(workdir=str(tmp_path / "nope"))
            )
            assert status == 400
            assert (await _request(service, "GET", "/api/v1/sessions/..%2Fx"))[0] == 404
            assert (await _request(service, "GET", "/api/v1/sessions/20260101-000000-abcdef"))[0] == 404
            status, _ = await _request(service, "GET", "/api/v1/sessions/x/transcript?after=abc")
            assert status == 400
        finally:
            _stop(service, srv)

    _run(scenario())


def test_without_app_reads_work_and_writes_are_unavailable(tmp_path) -> None:
    async def scenario():
        service, srv = await _start(None)
        try:
            assert (await _request(service, "GET", "/api/v1/sessions"))[0] == 200
            status, _ = await _request(
                service, "POST", "/api/v1/sessions", _body(workdir=str(tmp_path))
            )
            assert status == 503
        finally:
            _stop(service, srv)

    _run(scenario())


def test_ws_sessions_list_and_changed_event(tmp_path) -> None:
    async def scenario():
        service, srv = await _start(FakeApp())
        try:
            reader, writer = await _ws_handshake(service.port)
            try:
                reply = await _rpc(reader, writer, {"id": 1, "method": "sessions.list", "params": {}})
                assert reply["result"]["sessions"] == []
                reply = await _rpc(
                    reader, writer,
                    {"id": 2, "method": "subscribe", "params": {"topics": ["sessions"]}},
                )
                assert reply["result"]["topics"] == ["sessions"]
                reply = await _rpc(
                    reader, writer,
                    {"id": 3, "method": "sessions.create", "params": {"workdir": str(tmp_path)}},
                )
                session_id = reply["result"]["session"]["id"]
                seen = set()
                for _ in range(10):
                    from test_api_ws import _read_server_frame

                    opcode, body = await asyncio.wait_for(_read_server_frame(reader), timeout=3.0)
                    payload = json.loads(body.decode("utf-8"))
                    if payload.get("event") == "session.changed":
                        assert payload["session"]["id"] == session_id
                        seen.add("changed")
                        break
                assert "changed" in seen
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv)

    _run(scenario())


def test_create_from_task(tmp_path) -> None:
    from grafeno import models
    from grafeno.config import Config

    project = tmp_path / "proj"
    project.mkdir()
    task = models.Task.create("My task", "d", str(project), Config())
    models.save(task)
    remote = models.Task.create("Far", "d", str(project), Config())
    remote.remote = "u@h:/p"
    models.save(remote)

    async def scenario():
        service, srv = await _start(FakeApp())
        try:
            status, data = await _request(service, "POST", "/api/v1/sessions", _body(task_id=task.id))
            assert status == 201, data
            session = data["session"]
            assert session["workdir"] == str(project.resolve())
            assert session["task_id"] == task.id
            assert session["name"] == "Task: My task"
            assert session["initial_context"] is True
            assert (await _request(service, "POST", "/api/v1/sessions", _body(task_id="nope")))[0] == 404
            assert (await _request(service, "POST", "/api/v1/sessions", _body(task_id=remote.id)))[0] == 409
            plain = await _create(service, tmp_path)
            assert plain["task_id"] == "" and plain["initial_context"] is False
        finally:
            _stop(service, srv)

    _run(scenario())
