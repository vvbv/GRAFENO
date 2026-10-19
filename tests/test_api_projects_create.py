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
        finally:
            _stop(service, task)

    asyncio.run(scenario())


def test_ws_method(workspace):
    result = asyncio.run(
        ws.METHODS["projects.create"](_Service(), {"name": "demo2", "mode": "plain"}, None)
    )
    assert result["project"]["name"] == "demo2"
