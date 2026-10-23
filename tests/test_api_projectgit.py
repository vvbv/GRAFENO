"""Tests of the per-project git REST/WS operations."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from grafeno import models, remotesession
from grafeno.config import ApiConfig, Config
from grafeno.server import projectgit as api
from grafeno.server.actions import ApiError
from grafeno.server.service import ServerService
from test_api_rest_write import _request, _stop
from test_api_ws import _rpc, _ws_handshake

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")


class _Service:
    app = None


def _run(coro):
    return asyncio.run(coro)


def _error(coro) -> ApiError:
    with pytest.raises(ApiError) as info:
        _run(coro)
    return info.value


def _register(root: Path) -> None:
    models.save(models.Task.create("T", "d", str(root), Config()))


@pytest.fixture
def project(tmp_path):
    root = (tmp_path / "proj").resolve()
    _init_repo(root)
    _register(root)
    return root


def test_changes_lists_projects_with_nested_dirty_repo(project):
    nested = project / "modules" / "m1" / "deep" / "m2"
    _init_repo(nested)
    (nested / "a.txt").write_text("dirty\n", encoding="utf-8")
    body = _run(api.list_changes(_Service(), refresh=True))
    assert body["git_available"] is True and body["session"] is False
    item = next(p for p in body["projects"] if p["workdir"] == str(project))
    assert item["dirty"] is True and item["dirty_repos"] == ["modules/m1/deep/m2"]
    one = _run(api.list_changes(_Service(), workdir=str(project)))
    assert len(one["projects"]) == 1


def test_changes_invalidated_by_operation(project):
    assert _run(api.list_changes(_Service(), workdir=str(project), refresh=True))["projects"][0]["dirty"] is False
    (project / "a.txt").write_text("dirty\n", encoding="utf-8")
    assert _run(api.list_changes(_Service(), workdir=str(project)))["projects"][0]["dirty"] is False
    body = _run(api.checkout(_Service(), {"workdir": str(project), "branch": "main"}))
    assert body["pending"]["dirty"] is True
    assert _run(api.list_changes(_Service(), workdir=str(project)))["projects"][0]["dirty"] is True


def test_changes_remote_session(project, monkeypatch):
    monkeypatch.setattr(remotesession, "active", lambda: True)
    body = _run(api.list_changes(_Service()))
    assert body["projects"] == [] and body["session"] is True
    err = _error(api.push(_Service(), {"workdir": str(project)}))
    assert err.status == 409 and err.code == "remote"


def test_branches_and_checkout_api(project):
    _git(project, "branch", "feature")
    body = _run(api.list_branches(_Service(), {"workdir": str(project), "repo": "."}))
    assert body["current"] == "main" and {b["name"] for b in body["branches"]} == {"main", "feature"}
    body = _run(api.checkout(_Service(), {"workdir": str(project), "branch": "feature"}))
    assert body["result"]["outcome"] == "switched" and body["repo"]["branch"] == "feature"
    assert "pending" in body


def test_errors_map_to_status(project):
    base = {"workdir": str(project)}
    err = _error(api.checkout(_Service(), {**base, "branch": "nope"}))
    assert (err.status, err.code) == (404, "branch_not_found")
    err = _error(api.pull(_Service(), {**base, "repo": "nope"}))
    assert (err.status, err.code) == (409, "not_a_repo")
    err = _error(api.pull(_Service(), base))
    assert (err.status, err.code) == (409, "no_upstream")
    err = _error(api.pull(_Service(), {**base, "mode": "x"}))
    assert (err.status, err.code) == (400, "invalid_mode")


def test_pull_conflict_api(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = (tmp_path / "work").resolve()
    _init_repo(work)
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    _git(other, "config", "user.email", "a@b.c")
    _git(other, "config", "user.name", "t")
    (other / "a.txt").write_text("other\n", encoding="utf-8")
    _git(other, "commit", "-qam", "other")
    _git(other, "push", "-q")
    (work / "a.txt").write_text("work\n", encoding="utf-8")
    _git(work, "commit", "-qam", "work")
    _register(work)
    body = _run(api.pull(_Service(), {"workdir": str(work), "mode": "merge"}))
    assert body["result"]["outcome"] == "conflict" and body["repo"]["operation"] == "merge"
    body = _run(api.abort(_Service(), {"workdir": str(work)}))
    assert body["result"]["outcome"] == "aborted"


def test_end_to_end_rest_and_ws(project):
    async def scenario():
        service = ServerService(ApiConfig(enabled=True, host="127.0.0.1", port=0, tokens="t1"), app=None)
        srv = asyncio.create_task(service.run())
        for _ in range(200):
            if service.server is not None:
                break
            await asyncio.sleep(0.01)
        try:
            status, body = await _request(service, "GET", "/api/v1/projects/changes")
            assert status == 200 and body["projects"]
            status, body = await _request(
                service, "GET", f"/api/v1/projects/branches?workdir={project}")
            assert status == 200 and body["current"] == "main"
            status, body = await _request(
                service, "POST", "/api/v1/projects/checkout",
                json.dumps({"workdir": str(project), "branch": "nope"}).encode())
            assert status == 404 and body["code"] == "branch_not_found"
            reader, writer = await _ws_handshake(service.port)
            try:
                reply = await _rpc(reader, writer, {"id": 1, "method": "projects.changes", "params": {}})
                assert reply["result"]["projects"]
                reply = await _rpc(reader, writer, {
                    "id": 2, "method": "projects.checkout",
                    "params": {"workdir": str(project), "branch": "nope"}})
                assert reply["error"]["code"] == "branch_not_found"
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv)

    _run(scenario())


def test_changes_invalidated_by_review_commit(project):
    from grafeno.server import reviews as reviews_api

    (project / "a.txt").write_text("dirty\n", encoding="utf-8")
    first = _run(api.list_changes(_Service(), workdir=str(project), refresh=True))
    assert first["projects"][0]["dirty"] is True
    _, body = _run(reviews_api.start_review(_Service(), {"workdir": str(project)}))
    _run(reviews_api.commit(_Service(), body["review"]["id"], {
        "files": [{"repo": ".", "path": "a.txt"}], "message": "feat: change"}))
    again = _run(api.list_changes(_Service(), workdir=str(project)))  # no refresh: cache must be dropped
    assert again["projects"][0]["dirty"] is False
