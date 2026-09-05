"""Tests of the REST write endpoints (create / start / pause / discard / ...)."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any, Optional

from grafeno.config import ApiConfig
from grafeno.models import Task, TaskState, list_all
from grafeno.server.service import ServerService
from grafeno.tui.runtime import TaskRuntime


def _run(coro):
    return asyncio.run(coro)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


class FakeApp:
    """Minimal App stub with runtimes + runtime_for + notify + run_worker."""

    def __init__(self) -> None:
        self.runtimes: dict[str, TaskRuntime] = {}
        self.notified: list[str] = []
        self.workers: list[asyncio.Task] = []

    def runtime_for(self, task: Task) -> TaskRuntime:
        runtime = self.runtimes.get(task.id)
        if runtime is None:
            runtime = TaskRuntime(task, orchestrator_factory=_fake_orchestrator)
            self.runtimes[task.id] = runtime
        return runtime

    def notify(self, message: str, **_: Any) -> None:
        self.notified.append(message)

    def run_worker(self, coro, **_: Any):  # noqa: ANN001 - mirrors Textual signature
        """Run the coroutine in the background; emulate Textual's run_worker."""
        task = asyncio.ensure_future(coro)
        self.workers.append(task)
        return task


def _fake_orchestrator(task: Task, **callbacks):
    """Orchestrator stub: records runner invocations on the task."""
    from grafeno.pipeline.orchestrator import Orchestrator

    class _Stub:
        def __init__(self, task: Task) -> None:
            self.task = task
            self.calls: list[str] = []

        async def run_automode(self) -> None:
            self.calls.append("run_automode")
            self.task.state = TaskState.DONE

        async def run_automode_plan(self) -> None:
            self.calls.append("run_automode_plan")
            self.task.state = TaskState.PLANNED

        async def run_automode_resume(self) -> None:
            self.calls.append("run_automode_resume")
            self.task.state = TaskState.DONE

        async def run_automode_continue(self) -> None:
            self.calls.append("run_automode_continue")

        async def run_reevaluate_plan(self) -> None:
            self.calls.append("run_reevaluate_plan")

    return _Stub(task)


async def _start_service(app: Optional[FakeApp]) -> tuple[ServerService, asyncio.Task]:
    cfg = ApiConfig(enabled=True, host="127.0.0.1", port=0, tokens="t1")
    service = ServerService(cfg, app=app)
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


async def _request(
    service: ServerService,
    method: str,
    path: str,
    body: bytes = b"",
) -> tuple[int, dict]:
    headers: dict[str, str] = {"Authorization": "Bearer t1"}
    if body:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
    raw = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        + "\r\n"
    ).encode("ascii") + body
    reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
    try:
        writer.write(raw)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        status = int(head.decode("iso-8859-1").split(" ", 2)[1])
        length = 0
        for line in head.decode("iso-8859-1").split("\r\n")[1:]:
            if not line:
                continue
            key, _, value = line.partition(":")
            if key.strip().lower() == "content-length":
                length = int(value.strip())
        body_bytes = b"" if length == 0 else await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
        return status, json.loads(body_bytes) if body_bytes else {}
    finally:
        writer.close()
        await writer.wait_closed()


def _make_task(tmp_path, name: str = "Base", **overrides) -> Task:
    from grafeno.config import Config

    cfg = Config()
    return Task.create(name, "desc", str(tmp_path), cfg, **overrides)


def test_create_task_201(tmp_path) -> None:
    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, payload = await _request(
                service, "POST", "/api/v1/tasks",
                json.dumps({"name": "API task", "workdir": str(tmp_path)}).encode(),
            )
            assert status == 201
            assert payload["task"]["name"] == "API task"
            assert payload["task"]["state"] == "draft"
            assert list_all()[0].id == payload["task"]["id"]
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_create_task_invalid_parent(tmp_path) -> None:
    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, payload = await _request(
                service, "POST", "/api/v1/tasks",
                json.dumps({"name": "Child", "workdir": str(tmp_path), "parent_id": "missing"}).encode(),
            )
            assert status == 400
            assert payload["error"] == "et.error.parent_missing"
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_create_task_with_valid_parent(tmp_path) -> None:
    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            parent = _make_task(tmp_path, name="Parent")
            from grafeno import models as models_module
            models_module.save(parent)
            status, payload = await _request(
                service, "POST", "/api/v1/tasks",
                json.dumps({
                    "name": "Child", "workdir": str(tmp_path), "parent_id": parent.id,
                }).encode(),
            )
            assert status == 201
            assert payload["task"]["parent_id"] == parent.id
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_start_task_invokes_automode(tmp_path) -> None:
    async def scenario():
        from grafeno import models as models_module
        from grafeno import paths

        app = FakeApp()
        task = _make_task(tmp_path)
        models_module.save(task)
        service, srv_task = await _start_service(app=app)
        try:
            status, payload = await _request(service, "POST", f"/api/v1/tasks/{task.id}/start")
            if status != 200:
                log_path = paths.api_log_path()
                if log_path.exists():
                    print("API LOG:", log_path.read_text())
            assert status == 200, payload
            assert payload == {"ok": True}
            await asyncio.sleep(0)
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_resume_only_failed(tmp_path) -> None:
    async def scenario():
        from grafeno import models as models_module

        app = FakeApp()
        task = _make_task(tmp_path)
        task.state = TaskState.DRAFT
        models_module.save(task)
        service, srv_task = await _start_service(app=app)
        try:
            status, payload = await _request(service, "POST", f"/api/v1/tasks/{task.id}/resume")
            assert status == 409
            assert "not in FAILED" in payload["error"]
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_discard_without_app(tmp_path) -> None:
    async def scenario():
        from grafeno import models as models_module

        task = _make_task(tmp_path)
        models_module.save(task)
        service, srv_task = await _start_service(app=None)
        try:
            status, payload = await _request(service, "POST", f"/api/v1/tasks/{task.id}/discard")
            assert status == 200
            assert payload == {"ok": True, "state": "discarded"}
            reloaded = models_module.load(task.id)
            assert reloaded.state is TaskState.DISCARDED
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_extend_records_request(tmp_path) -> None:
    async def scenario():
        from grafeno import models as models_module

        app = FakeApp()
        task = _make_task(tmp_path)
        models_module.save(task)
        service, srv_task = await _start_service(app=app)
        try:
            status, payload = await _request(
                service, "POST", f"/api/v1/tasks/{task.id}/extend",
                json.dumps({"request": "Add more tests"}).encode(),
            )
            assert status == 200
            reloaded = models_module.load(task.id)
            assert reloaded.cycle == 2
            assert reloaded.extensions.get("2") == "Add more tests"
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_invalid_json_returns_400(tmp_path) -> None:
    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, payload = await _request(
                service, "POST", "/api/v1/tasks", b"not-json",
            )
            assert status == 400
            assert payload["error"] == "invalid json"
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_create_task_parses_string_automode_off(tmp_path) -> None:
    """String 'false' / '0' must disable automode (bool('false') would be True)."""
    from grafeno import models as models_module

    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, payload = await _request(
                service, "POST", "/api/v1/tasks",
                json.dumps({
                    "name": "Manual",
                    "workdir": str(tmp_path),
                    "automode": "false",
                }).encode(),
            )
            assert status == 201
            created = models_module.load(payload["task"]["id"])
            assert created.automode is False
        finally:
            _stop(service, srv_task)

    _run(scenario())
