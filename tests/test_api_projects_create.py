"""Tests of the project creation API (REST + WS)."""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from grafeno import config as config_module
from grafeno import projectsetup, remote, remotesession
from grafeno.server import actions, projectcreate, ws
from grafeno.server.actions import ApiError

from test_api_rest import _request, _start_service, _stop


class _Service:
    app = None

    def _log(self, message: str) -> None:
        pass


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws_dir = (tmp_path / "ws").resolve()
    ws_dir.mkdir()
    cfg = config_module.load()
    cfg.workspaces = [str(ws_dir)]
    config_module.save(cfg)
    return ws_dir


def _create(payload: dict) -> tuple[int, dict]:
    return asyncio.run(projectcreate.create_project(_Service(), payload))


def _fails(payload: dict, status: int) -> None:
    with pytest.raises(ApiError) as info:
        _create(payload)
    assert info.value.status == status


def test_create_plain(workspace):
    status, body = _create({"name": "demo", "mode": "plain"})
    assert status == 201
    assert body["project"]["workdir"] == str(workspace / "demo")
    assert body["git"] is False
    listed = actions.list_projects(_Service())["projects"]
    [item] = [p for p in listed if p["workdir"] == str(workspace / "demo")]
    assert item["count"] == 0


@needs_git
def test_create_init(workspace):
    status, body = _create({"name": "demo"})
    assert status == 201 and body["git"] is True
    assert (workspace / "demo" / ".git").exists()


def test_validation_errors(workspace):
    _fails({"name": "../x"}, 400)
    _fails({"name": "x", "mode": "zip"}, 400)
    _fails({"name": "x", "url": "file:///tmp"}, 400)
    _fails({"name": "x", "workspace": str(workspace.parent)}, 400)
    _fails({"name": 123}, 400)


def test_duplicate_is_conflict(workspace):
    _create({"name": "demo", "mode": "plain"})
    _fails({"name": "demo", "mode": "plain"}, 409)


def test_no_workspaces_is_conflict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _fails({"name": "demo", "mode": "plain"}, 409)


def test_git_missing(workspace, monkeypatch):
    monkeypatch.setattr(projectsetup, "git_available", lambda: False)
    _fails({"name": "demo"}, 503)
    assert not (workspace / "demo").exists()


def test_remote_session_mode(workspace):
    remotesession._current = remotesession.RemoteSession(
        spec=remote.RemoteSpec(user="root", host="h", path="/root"), remote_home="/root",
    )
    try:
        _fails({"name": "demo", "mode": "plain"}, 409)
    finally:
        remotesession._current = None


def test_rest_post_and_get(workspace):
    async def scenario():
        service, task = await _start_service()
        try:
            body = json.dumps({"name": "demo", "mode": "plain"}).encode()
            status, data = await _request(service, "POST", "/api/v1/projects", body)
            assert status == 201 and data["project"]["name"] == "demo"
            status, _ = await _request(service, "GET", "/api/v1/projects")
            assert status == 200
            status, data = await _request(service, "POST", "/api/v1/projects", body)
            assert status == 409 and data["code"] == "exists"
        finally:
            _stop(service, task)

    asyncio.run(scenario())


def test_ws_method(workspace):
    result = asyncio.run(
        ws.METHODS["projects.create"](_Service(), {"name": "demo2", "mode": "plain"}, None)
    )
    assert result["project"]["name"] == "demo2"


def test_error_carries_code(workspace):
    with pytest.raises(ApiError) as info:
        _create({"name": "a/b"})
    assert (info.value.status, info.value.code) == (400, "invalid_name")
    _create({"name": "dup", "mode": "plain"})
    with pytest.raises(ApiError) as info:
        _create({"name": "dup", "mode": "plain"})
    assert (info.value.status, info.value.code) == (409, "exists")


def test_timeout_is_gateway_timeout(workspace, monkeypatch):
    async def fake(args, cwd, timeout):
        return projectsetup.TIMED_OUT, "timed out after 600 s"

    monkeypatch.setattr(projectsetup, "git_available", lambda: True)
    monkeypatch.setattr(projectsetup, "_run_git", fake)
    with pytest.raises(ApiError) as info:
        _create({"url": "https://host/o/r.git"})
    assert (info.value.status, info.value.code) == (504, "timeout")


def test_failures_are_logged_redacted(workspace, monkeypatch):
    async def fake(args, cwd, timeout):
        return 128, "fatal: could not read from https://u:tok@host/o/r.git"

    class Logging(_Service):
        def __init__(self):
            self.logged: list[str] = []

        def _log(self, message: str) -> None:
            self.logged.append(message)

    monkeypatch.setattr(projectsetup, "git_available", lambda: True)
    monkeypatch.setattr(projectsetup, "_run_git", fake)
    service = Logging()
    with pytest.raises(ApiError):
        asyncio.run(projectcreate.create_project(service, {"url": "https://u:tok@host/o/r.git"}))
    assert any("project create requested" in m for m in service.logged)
    assert any("project create failed: clone_failed" in m for m in service.logged)
    assert not any("tok" in m for m in service.logged)


def test_request_log_line_is_quoted_and_bounded(workspace):
    class Logging(_Service):
        def __init__(self):
            self.logged: list[str] = []

        def _log(self, message: str) -> None:
            self.logged.append(message)

    service = Logging()
    payload = {"name": "x", "mode": "plain", "workspace": "w\nforged line" + "a" * 5000}
    with pytest.raises(ApiError):
        asyncio.run(projectcreate.create_project(service, payload))
    first = service.logged[0]
    assert "\n" not in first and len(first) < 600
