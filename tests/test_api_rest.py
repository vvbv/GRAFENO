"""Tests of the REST read endpoints."""

from __future__ import annotations

import asyncio
import json
import socket

from grafeno import paths
from grafeno.config import ApiConfig
from grafeno.config import Config as CfgType
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


async def _request(service: ServerService, method: str, path: str, body: bytes = b"") -> tuple[int, dict]:
    headers = {"Authorization": "Bearer t1"}
    if body:
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


def _make_task(tmp_path, name: str = "Demo", **overrides) -> Task:
    cfg = CfgType()
    task = Task.create(name, "desc", str(tmp_path), cfg, **overrides)
    return task


def test_list_tasks_empty() -> None:
    from grafeno import models

    async def scenario():
        service, task = await _start_service()
        try:
            status, payload = await _request(service, "GET", "/api/v1/tasks")
            assert status == 200
            assert payload == {"tasks": []}
            assert models.list_all() == []
        finally:
            _stop(service, task)

    _run(scenario())


def test_list_tasks_with_two_tasks(tmp_path) -> None:
    from grafeno import models

    t1 = _make_task(tmp_path / "p1")
    t2 = _make_task(tmp_path / "p2")
    models.save(t1)
    models.save(t2)

    async def scenario():
        service, task = await _start_service()
        try:
            status, payload = await _request(service, "GET", "/api/v1/tasks")
            assert status == 200
            ids = {entry["id"] for entry in payload["tasks"]}
            assert ids == {t1.id, t2.id}
            # The summary exposes the slim fields.
            for entry in payload["tasks"]:
                assert "state_label" in entry
                assert "automode" in entry
        finally:
            _stop(service, task)

    _run(scenario())


def test_list_tasks_filter_by_state(tmp_path) -> None:
    from grafeno import models

    t1 = _make_task(tmp_path, name="Alpha")
    t2 = _make_task(tmp_path, name="Beta")
    t2.state = TaskState.DONE
    models.save(t1)
    models.save(t2)

    async def scenario():
        service, task = await _start_service()
        try:
            status, payload = await _request(service, "GET", "/api/v1/tasks?state=draft")
            assert status == 200
            ids = [entry["id"] for entry in payload["tasks"]]
            assert ids == [t1.id]
            status, payload = await _request(service, "GET", "/api/v1/tasks?state=done")
            ids = [entry["id"] for entry in payload["tasks"]]
            assert ids == [t2.id]
        finally:
            _stop(service, task)

    _run(scenario())


def test_get_task_detail_and_404(tmp_path) -> None:
    from grafeno import models

    task = _make_task(tmp_path)
    models.save(task)

    async def scenario():
        service, srv_task = await _start_service()
        try:
            status, payload = await _request(service, "GET", f"/api/v1/tasks/{task.id}")
            assert status == 200
            # task.to_dict() nests the task fields under "task".
            assert payload["task"]["id"] == task.id
            assert payload["state_label"]
            assert payload["token_totals"] == {"input": 0, "output": 0}
            # 404 for an unknown id.
            status, payload = await _request(service, "GET", "/api/v1/tasks/nope")
            assert status == 404
            assert payload["error"] == "task not found"
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_list_projects(tmp_path) -> None:
    from grafeno import models

    p1 = tmp_path / "p1"
    p2 = tmp_path / "p2"
    t1 = _make_task(p1, name="P1A")
    t2 = _make_task(p1, name="P1B")
    t3 = _make_task(p2, name="P2A")
    models.save(t1)
    models.save(t2)
    models.save(t3)

    async def scenario():
        service, srv_task = await _start_service()
        try:
            status, payload = await _request(service, "GET", "/api/v1/projects")
            assert status == 200
            counts = {item["workdir"]: item["count"] for item in payload["projects"]}
            assert counts[str(p1)] == 2
            assert counts[str(p2)] == 1
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_logs_empty(tmp_path) -> None:
    from grafeno import models

    task = _make_task(tmp_path)
    models.save(task)

    async def scenario():
        service, srv_task = await _start_service()
        try:
            status, payload = await _request(
                service, "GET", f"/api/v1/tasks/{task.id}/logs?limit=50"
            )
            assert status == 200
            assert payload == {"logs": []}
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_logs_404(tmp_path) -> None:
    async def scenario():
        service, srv_task = await _start_service()
        try:
            status, payload = await _request(service, "GET", "/api/v1/tasks/nope/logs")
            assert status == 404
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_artifacts_empty(tmp_path) -> None:
    from grafeno import models

    task = _make_task(tmp_path)
    models.save(task)

    async def scenario():
        service, srv_task = await _start_service()
        try:
            for kind in ("plan", "review", "final"):
                status, payload = await _request(
                    service, "GET", f"/api/v1/tasks/{task.id}/artifacts?kind={kind}&cycle=1"
                )
                assert status == 200
                assert payload["kind"] == kind
                assert payload["files"] == []
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_artifacts_with_content(tmp_path) -> None:
    from grafeno import models

    task = _make_task(tmp_path)
    models.save(task)
    plan_path = paths.plan_dir(task.id, 1) / "01-idea.md"
    plan_path.write_text("# idea\nmore", encoding="utf-8")

    async def scenario():
        service, srv_task = await _start_service()
        try:
            status, payload = await _request(
                service, "GET", f"/api/v1/tasks/{task.id}/artifacts?kind=plan"
            )
            assert status == 200
            assert payload["kind"] == "plan"
            assert len(payload["files"]) == 1
            assert payload["files"][0]["name"] == "01-idea.md"
            assert "more" in payload["files"][0]["content"]
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_artifacts_invalid_kind(tmp_path) -> None:
    from grafeno import models

    task = _make_task(tmp_path)
    models.save(task)

    async def scenario():
        service, srv_task = await _start_service()
        try:
            status, payload = await _request(
                service, "GET", f"/api/v1/tasks/{task.id}/artifacts?kind=bogus"
            )
            assert status == 400
            assert payload["error"] == "unknown kind: bogus"
        finally:
            _stop(service, srv_task)

    _run(scenario())
