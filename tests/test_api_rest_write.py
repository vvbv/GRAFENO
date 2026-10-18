"""Tests of the REST write endpoints (create / start / pause / discard / ...)."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from typing import Any, Optional

from grafeno.config import ApiConfig
from grafeno.i18n import t
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
    content_type: str = "application/json",
) -> tuple[int, dict]:
    headers: dict[str, str] = {"Authorization": "Bearer t1"}
    if body:
        headers["Content-Type"] = content_type
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
            assert payload["error"] == t("et.error.parent_missing")
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
            assert payload["task"]["scheduled_at"]  # auto-scheduled: starts when the parent finishes
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_create_task_with_completed_parent(tmp_path) -> None:
    """Chaining after a DONE task is allowed and auto-schedules the child."""
    async def scenario():
        from grafeno import models as models_module
        from grafeno.models import TaskState

        service, srv_task = await _start_service(app=FakeApp())
        try:
            parent = _make_task(tmp_path, name="Parent done")
            parent.state = TaskState.DONE
            models_module.save(parent)
            status, payload = await _request(
                service, "POST", "/api/v1/tasks",
                json.dumps({
                    "name": "Child", "workdir": str(tmp_path), "parent_id": parent.id,
                }).encode(),
            )
            assert status == 201
            assert payload["task"]["parent_id"] == parent.id
            assert payload["task"]["scheduled_at"]
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_create_task_chained_keeps_explicit_schedule(tmp_path) -> None:
    """An explicit scheduled_at is never overwritten by the chain default."""
    async def scenario():
        from grafeno import models as models_module

        service, srv_task = await _start_service(app=FakeApp())
        try:
            parent = _make_task(tmp_path, name="Parent")
            models_module.save(parent)
            status, payload = await _request(
                service, "POST", "/api/v1/tasks",
                json.dumps({
                    "name": "Child", "workdir": str(tmp_path), "parent_id": parent.id,
                    "scheduled_at": "2099-01-01 09:30",
                }).encode(),
            )
            assert status == 201
            assert payload["task"]["scheduled_at"] == "2099-01-01T09:30"
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_create_task_with_attachments(tmp_path) -> None:
    """POST /tasks with base64 attachments persists media and references it."""
    async def scenario():
        from grafeno import models as models_module
        from grafeno import paths

        service, srv_task = await _start_service(app=FakeApp())
        try:
            body = {
                "name": "Con adjunto",
                "workdir": str(tmp_path),
                "attachments": [
                    {"name": "wireframe.png", "data": base64.b64encode(b"PNGBYTES").decode()},
                    {"name": "notes.mp4", "data": base64.b64encode(b"MP4BYTES").decode()},
                ],
            }
            status, payload = await _request(
                service, "POST", "/api/v1/tasks", json.dumps(body).encode()
            )
            assert status == 201, payload
            task = models_module.load(payload["task"]["id"])
            directory = paths.task_dir(task.id) / "media"
            assert (directory / "media-01.png").exists()
            assert (directory / "media-01.mp4").exists()
            assert "media/media-01.png" in task.description
            assert str(directory / "media-01.mp4") in task.description
            assert "Attachments received via the API:" in task.description
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_create_task_attachments_invalid(tmp_path) -> None:
    """Malformed attachments yield 400 and no task is created."""
    cases = [
        ({"not": "a list"}, "attachments must be a list"),
        ([{"name": "x.png"}], "attachment 0 must be an object with 'data'"),
        ([{"name": "x.png", "data": "not base64!!"}], "attachment 0: invalid base64 data"),
        (
            [
                {"name": f"x{n}.png", "data": base64.b64encode(b"P").decode()}
                for n in range(11)
            ],
            "too many attachments (max 10)",
        ),
    ]

    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            for attachments, message in cases:
                body = {
                    "name": "Invalid",
                    "workdir": str(tmp_path),
                    "attachments": attachments,
                }
                status, payload = await _request(
                    service, "POST", "/api/v1/tasks", json.dumps(body).encode()
                )
                assert status == 400, (attachments, payload)
                assert payload["error"] == message
                assert list_all() == []
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


def test_create_task_with_profile_applies_roles(tmp_path) -> None:
    """POST /tasks with a profile name writes its roles into the new task."""
    from grafeno import models as models_module
    from grafeno import profiles as profiles_module
    from grafeno.config import RoleConfig
    from grafeno.profiles import Profile

    profile = Profile(name="calidad")
    profile.implementer = RoleConfig(cli="kimi", model="kimi-code/k3", effort="max")
    profiles_module.save_global([profile])

    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, payload = await _request(
                service, "POST", "/api/v1/tasks",
                json.dumps({
                    "name": "Con perfil",
                    "workdir": str(tmp_path),
                    "profile": "calidad",
                }).encode(),
            )
            assert status == 201, payload
            assert payload["task"]["profile"] == "calidad"
            created = models_module.load(payload["task"]["id"])
            assert created.profile == "calidad"
            assert created.implementer.cli == "kimi"
            assert created.implementer.model == "kimi-code/k3"
            assert created.implementer.effort == "max"
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_create_task_unknown_profile_returns_400(tmp_path) -> None:
    async def scenario():
        from grafeno import models as models_module

        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, payload = await _request(
                service, "POST", "/api/v1/tasks",
                json.dumps({
                    "name": "Sin perfil",
                    "workdir": str(tmp_path),
                    "profile": "no-existe",
                }).encode(),
            )
            assert status == 400
            assert "unknown profile" in payload["error"]
            assert models_module.list_all() == []
        finally:
            _stop(service, srv_task)

    _run(scenario())


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_upload_attachments_json_keeps_office_formats(tmp_path) -> None:
    async def scenario():
        from grafeno import models as models_module
        from grafeno import paths

        task = _make_task(tmp_path)
        models_module.save(task)
        service, srv_task = await _start_service(app=FakeApp())
        try:
            body = {"attachments": [
                {"name": "presupuesto.xlsx", "data": _b64(b"XLSX")},
                {"name": "informe.docx", "data": _b64(b"DOCX")},
            ]}
            status, payload = await _request(
                service, "POST", f"/api/v1/tasks/{task.id}/attachments", json.dumps(body).encode()
            )
            assert status == 201, payload
            kinds = {a["name"]: a["kind"] for a in payload["attachments"]}
            assert kinds == {"presupuesto.xlsx": "spreadsheet", "informe.docx": "document"}
            from pathlib import Path

            assert all(Path(a["path"]).exists() for a in payload["attachments"])
            directory = paths.task_dir(task.id) / "media"
            assert (directory / "media-01.xlsx").exists()
            assert (directory / "media-01.docx").exists()
            reloaded = models_module.load(task.id)
            assert reloaded.state is TaskState.DRAFT
            assert reloaded.description == task.description
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_upload_attachment_raw_body(tmp_path) -> None:
    async def scenario():
        from grafeno import models as models_module

        task = _make_task(tmp_path)
        models_module.save(task)
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, payload = await _request(
                service, "POST", f"/api/v1/tasks/{task.id}/attachments?name=demo.mp4",
                b"MP4BYTES", content_type="application/octet-stream",
            )
            assert status == 201, payload
            assert payload["attachments"][0]["kind"] == "video"
            status, _ = await _request(
                service, "POST", f"/api/v1/tasks/{task.id}/attachments",
                b"MP4BYTES", content_type="application/octet-stream",
            )
            assert status == 400
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_list_and_delete_attachments(tmp_path, monkeypatch) -> None:
    from grafeno.server import actions

    async def scenario():
        from grafeno import media
        from grafeno import models as models_module

        task = _make_task(tmp_path)
        models_module.save(task)
        media.save_attachment(task.id, "presupuesto.xlsx", b"X")
        service, srv_task = await _start_service(app=FakeApp())
        base = f"/api/v1/tasks/{task.id}/attachments"
        try:
            status, payload = await _request(service, "GET", base)
            assert status == 200
            assert [a["name"] for a in payload["attachments"]] == ["presupuesto.xlsx"]
            status, _ = await _request(service, "DELETE", base + "?file=media-01.xlsx")
            assert status == 200
            assert media.list_attachments(task.id) == []
            status, _ = await _request(service, "DELETE", base + "?file=media-01.xlsx")
            assert status == 404
            status, _ = await _request(service, "DELETE", base + "?file=.task.toml")
            assert status == 400
            status, _ = await _request(service, "DELETE", base + "?file=../task.toml")
            assert status == 400
            media.save_attachment(task.id, "again.xlsx", b"X")
            monkeypatch.setattr(actions, "_is_running", lambda *_: True)
            status, _ = await _request(service, "DELETE", base + "?file=media-01.xlsx")
            assert status == 409
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_upload_attachment_too_large(tmp_path, monkeypatch) -> None:
    async def scenario():
        from grafeno import media
        from grafeno import models as models_module

        monkeypatch.setattr(media, "MAX_ATTACHMENT_BYTES", 4)
        task = _make_task(tmp_path)
        models_module.save(task)
        service, srv_task = await _start_service(app=FakeApp())
        try:
            body = {"attachments": [{"name": "a.xlsx", "data": _b64(b"12345")}]}
            status, _ = await _request(
                service, "POST", f"/api/v1/tasks/{task.id}/attachments", json.dumps(body).encode()
            )
            assert status == 413
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_extend_attachments_record_next_cycle(tmp_path) -> None:
    async def scenario():
        from grafeno import media
        from grafeno import models as models_module

        task = _make_task(tmp_path)
        models_module.save(task)
        service, srv_task = await _start_service(app=FakeApp())
        try:
            body = {"request": "More", "attachments": [{"name": "a.xlsx", "data": _b64(b"X")}]}
            status, _ = await _request(
                service, "POST", f"/api/v1/tasks/{task.id}/extend", json.dumps(body).encode()
            )
            assert status == 200
            entry = media.list_attachments(task.id)[0]
            assert entry.cycle == models_module.load(task.id).cycle
            assert entry.origin == "api"
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_task_detail_includes_attachments(tmp_path) -> None:
    async def scenario():
        from grafeno import media
        from grafeno import models as models_module

        task = _make_task(tmp_path)
        models_module.save(task)
        media.save_attachment(task.id, "presupuesto.xlsx", b"X")
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, payload = await _request(service, "GET", f"/api/v1/tasks/{task.id}")
            assert status == 200
            detail = payload.get("task", payload)
            assert detail["attachments"][0]["name"] == "presupuesto.xlsx"
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_upload_attachment_raw_streams_to_media(tmp_path) -> None:
    async def scenario():
        import os

        from grafeno import models as models_module
        from grafeno import paths

        task = _make_task(tmp_path)
        models_module.save(task)
        service, srv_task = await _start_service(app=FakeApp())
        try:
            blob = os.urandom(3 * 1024 * 1024)
            status, payload = await _request(
                service, "POST", f"/api/v1/tasks/{task.id}/attachments?name=datos.zip",
                blob, content_type="application/octet-stream",
            )
            assert status == 201, payload
            assert payload["attachments"][0]["name"] == "datos.zip"
            assert (paths.task_dir(task.id) / "media" / "media-01.zip").read_bytes() == blob
            assert not list(paths.uploads_dir().glob("*.part"))
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_upload_attachment_raw_respects_cycle(tmp_path) -> None:
    async def scenario():
        from grafeno import media
        from grafeno import models as models_module

        task = _make_task(tmp_path)
        models_module.save(task)
        service, srv_task = await _start_service(app=FakeApp())
        try:
            url = f"/api/v1/tasks/{task.id}/attachments?name=a.zip"
            kwargs = {"content_type": "application/octet-stream"}
            status, _ = await _request(service, "POST", url + "&cycle=2", b"abc", **kwargs)
            assert status == 201
            assert media.list_attachments(task.id)[0].cycle == 2
            status, _ = await _request(service, "POST", url + "&cycle=5", b"abc", **kwargs)
            assert status == 400
            status, _ = await _request(service, "POST", url + "&cycle=x", b"abc", **kwargs)
            assert status == 400
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_upload_attachment_raw_too_large(tmp_path, monkeypatch) -> None:
    async def scenario():
        from grafeno import models as models_module
        from grafeno import paths
        from grafeno.server import httpcore

        monkeypatch.setattr(httpcore, "MAX_RAW_UPLOAD_BODY", 4)
        task = _make_task(tmp_path)
        models_module.save(task)
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, _ = await _request(
                service, "POST", f"/api/v1/tasks/{task.id}/attachments?name=a.zip",
                b"12345", content_type="application/octet-stream",
            )
            assert status == 413
            assert not list((paths.task_dir(task.id) / "media").glob("media-*"))
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_upload_attachment_raw_disk_full(tmp_path, monkeypatch) -> None:
    async def scenario():
        from grafeno import media
        from grafeno import models as models_module
        from grafeno import paths

        monkeypatch.setattr(media, "has_free_space", lambda *_: False)
        task = _make_task(tmp_path)
        models_module.save(task)
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, _ = await _request(
                service, "POST", f"/api/v1/tasks/{task.id}/attachments?name=a.zip",
                b"12345", content_type="application/octet-stream",
            )
            assert status == 507
            assert not list(paths.uploads_dir().glob("*.part"))
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_upload_json_base64_too_long_is_413_without_decoding(tmp_path, monkeypatch) -> None:
    async def scenario():
        from grafeno import media
        from grafeno import models as models_module
        from grafeno.server import actions

        monkeypatch.setattr(media, "MAX_ATTACHMENT_BYTES", 4)

        def boom(*_a, **_k):
            raise AssertionError("must not decode")

        monkeypatch.setattr(actions.base64, "b64decode", boom)
        task = _make_task(tmp_path)
        models_module.save(task)
        service, srv_task = await _start_service(app=FakeApp())
        try:
            body = {"attachments": [{"name": "a.zip", "data": "A" * 100}]}
            status, _ = await _request(
                service, "POST", f"/api/v1/tasks/{task.id}/attachments", json.dumps(body).encode()
            )
            assert status == 413
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_get_media_streams_large_file(tmp_path) -> None:
    async def scenario():
        import os

        from grafeno import media
        from grafeno import models as models_module

        task = _make_task(tmp_path)
        models_module.save(task)
        blob = os.urandom(3 * 1024 * 1024)
        saved = media.save_attachment(task.id, "x.bin", blob)
        service, srv_task = await _start_service(app=FakeApp())
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                writer.write((
                    f"GET /api/v1/tasks/{task.id}/media?name={saved.name} HTTP/1.1\r\n"
                    "Host: 127.0.0.1\r\nAuthorization: Bearer t1\r\nConnection: close\r\n\r\n"
                ).encode("ascii"))
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
                assert f"Content-Length: {len(blob)}".encode() in head
                data = await asyncio.wait_for(reader.readexactly(len(blob)), timeout=10.0)
                assert data == blob
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv_task)

    _run(scenario())
