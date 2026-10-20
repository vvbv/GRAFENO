"""Tests of the assisted review REST/WS operations."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from grafeno import clisessions, models, remotesession
from grafeno import reviews as reviews_module
from grafeno.clisession_runtime import CliSessionManager
from grafeno.config import ApiConfig, Config
from grafeno.server import reviews as api
from grafeno.server.actions import ApiError
from grafeno.server.service import ServerService
from test_api_rest_write import _request, _stop
from test_api_sessions import FakeSessionDriver
from test_api_ws import _read_server_frame, _rpc, _ws_handshake

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")


class _Service:
    app = None


class _App:
    def __init__(self) -> None:
        self.cli_sessions = CliSessionManager(driver_factory=lambda cli: FakeSessionDriver())

    def notify(self, message: str, **_: Any) -> None:
        pass


class _AppService:
    def __init__(self) -> None:
        self.app = _App()


@pytest.fixture
def project(tmp_path):
    root = (tmp_path / "proj").resolve()
    _init_repo(root)
    _init_repo(root / "modules" / "modulo1")
    (root / "README.md").write_text("two\n", encoding="utf-8")
    (root / "modules" / "modulo1" / "algo.py").write_text("print(1)\n", encoding="utf-8")
    models.save(models.Task.create("T", "d", str(root), Config()))
    return root


def _run(coro):
    return asyncio.run(coro)


def _start(service, root, **extra) -> dict:
    status, body = _run(api.start_review(service, {"workdir": str(root), **extra}))
    return status, body


def _error(coro, status: int, code: str = "") -> ApiError:
    with pytest.raises(ApiError) as info:
        _run(coro)
    assert info.value.status == status
    if code:
        assert info.value.code == code
    return info.value


def test_start_resume_and_detail(project):
    service = _Service()
    status, body = _start(service, project)
    assert status == 201 and body["resumed"] is False
    files = {(f["repo"], f["path"]): f for f in body["files"]}
    assert set(files) == {(".", "README.md"), ("modules/modulo1", "algo.py")}
    assert files[("modules/modulo1", "algo.py")]["project_path"] == "modules/modulo1/algo.py"
    assert [r["id"] for r in body["repos"]] == [".", "modules/modulo1"]
    status, again = _start(service, project)
    assert status == 200 and again["resumed"] is True
    assert again["review"]["id"] == body["review"]["id"]
    status, other = _start(service, project, resume=False)
    assert status == 201 and other["review"]["id"] != body["review"]["id"]
    listed = api.list_reviews(service)["reviews"]
    assert len(listed) == 2
    with pytest.raises(ApiError):
        api.list_reviews(service, state="weird")
    repos = _run(api.list_repos(service, workdir=str(project)))
    assert [r["id"] for r in repos["repos"]] == [".", "modules/modulo1"]


def test_unknown_project_and_remote_mode(project, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    _error(api.start_review(_Service(), {"workdir": str(other)}), 404)
    monkeypatch.setattr(remotesession, "active", lambda: True)
    _error(api.start_review(_Service(), {"workdir": str(project)}), 409, "remote")


def test_navigate_and_decision(project):
    service = _Service()
    _, body = _start(service, project)
    rid = body["review"]["id"]
    first = _run(api.navigate(service, rid, {"action": "first"}))
    assert first["current"]["path"] == "README.md" and first["end"] is False
    assert "+two" in first["diff"]["text"]
    second = _run(api.navigate(service, rid, {"action": "next"}))
    assert second["current"]["path"] == "algo.py"
    last = _run(api.navigate(service, rid, {"action": "next"}))
    assert last["current"] is None and last["end"] is True
    _error(api.navigate(service, rid, {"action": "goto", "repo": ".", "path": "nope"}), 409, "not_pending")
    _error(api.navigate(service, rid, {"action": "bogus"}), 400)
    _run(api.navigate(service, rid, {"action": "first"}))
    result = _run(api.decide(service, rid, {
        "repo": ".", "path": "README.md", "decision": "accepted", "advance": True}))
    assert result["file"]["decision"] == "accepted"
    assert result["current"]["path"] == "algo.py" and result["end"] is False
    assert result["counts"]["accepted"] == 1
    listing = _run(api.list_files(service, rid, decision="accepted"))
    assert [f["path"] for f in listing["files"]] == ["README.md"]
    diff = _run(api.get_diff(service, rid, {"repo": "modules/modulo1", "path": "algo.py"}))
    assert "+print(1)" in diff["diff"]["text"]
    _error(api.get_diff(service, rid, {"path": "algo.py"}), 400)


def test_commit_to_nested_repo(project):
    service = _Service()
    _, body = _start(service, project)
    rid = body["review"]["id"]
    root_head = _git(project, "rev-parse", "HEAD")
    nested = project / "modules" / "modulo1"
    result = _run(api.commit(service, rid, {
        "files": [{"repo": "modules/modulo1", "path": "algo.py"}], "message": "feat: add algo"}))
    assert result["partial"] is False and len(result["commits"]) == 1
    assert _git(nested, "log", "--oneline", "-1").endswith("feat: add algo")
    assert _git(project, "rev-parse", "HEAD") == root_head
    assert [f["path"] for f in result["review"]["files"]] == ["README.md"]
    _error(api.commit(service, rid, {
        "files": [{"repo": "modules/modulo1", "path": "algo.py"}], "message": "again"}),
        409, "not_pending")
    assert api.list_commits(service, rid)["commits"][0]["repo"] == "modules/modulo1"


def test_commit_validation(project):
    service = _Service()
    _, body = _start(service, project)
    rid = body["review"]["id"]
    both = [{"repo": ".", "path": "README.md"}, {"repo": "modules/modulo1", "path": "algo.py"}]
    root_head = _git(project, "rev-parse", "HEAD")
    _error(api.commit(service, rid, {"files": both, "message": "m"}), 400, "multi_repo")
    assert _git(project, "rev-parse", "HEAD") == root_head
    _error(api.commit(service, rid, {"files": both[:1], "message": "  "}), 400)
    _error(api.commit(service, rid, {"files": [], "message": "m"}), 400)
    result = _run(api.commit(service, rid, {"files": both, "message": "m", "allow_multi_repo": True}))
    assert len(result["commits"]) == 2
    _run(api.close_review(service, rid))
    _error(api.navigate(service, rid, {"action": "first"}), 409, "closed")
    _error(api.commit(service, rid, {"files": both[:1], "message": "m"}), 409, "closed")
    _run(api.reopen_review(service, rid))
    api.delete_review(service, rid)
    _error(api.get_review(service, rid), 404)


def test_commit_message_does_not_commit(project, monkeypatch):
    service = _Service()
    _, body = _start(service, project)
    rid = body["review"]["id"]

    class Driver(FakeSessionDriver):
        async def run(self, request, on_event=None, on_activity=None, on_session=None):
            from grafeno.drivers.base import RunResult, TokenUsage
            return RunResult(ok=True, text="```\nfeat: add algo\n```", tokens=TokenUsage(1, 1))

    monkeypatch.setattr(reviews_module, "get_driver", lambda name: Driver())
    original = reviews_module.suggest_commit_message
    # default argument binds get_driver at definition time: pass the factory explicitly
    async def patched(*args, **kwargs):
        kwargs.setdefault("driver_factory", lambda name: Driver())
        return await original(*args, **kwargs)
    monkeypatch.setattr(reviews_module, "suggest_commit_message", patched)
    head = _git(project / "modules" / "modulo1", "rev-parse", "HEAD")
    result = _run(api.commit_message(service, rid, {
        "files": [{"repo": "modules/modulo1", "path": "algo.py"}]}))
    assert result["message"] == "feat: add algo"
    assert _git(project / "modules" / "modulo1", "rev-parse", "HEAD") == head
    both = [{"repo": ".", "path": "README.md"}, {"repo": "modules/modulo1", "path": "algo.py"}]
    _error(api.commit_message(service, rid, {"files": both}), 400, "multi_repo")


def test_discuss_requires_app_and_runs_turn(project):
    _, body = _start(_Service(), project)
    rid = body["review"]["id"]
    _error(api.discuss(_Service(), rid, {"message": "hi"}), 503)
    service = _AppService()

    async def scenario():
        result = await api.discuss(service, rid, {
            "message": "why?", "repo": "modules/modulo1", "path": "algo.py"})
        session_id = result["session"]["id"]
        await service.app.cli_sessions.wait(session_id)
        transcript = api.review_transcript(service, rid)
        changes = await api.request_changes(service, rid, {
            "repo": "modules/modulo1", "path": "algo.py", "instructions": "rename"})
        await service.app.cli_sessions.wait(session_id)
        return result, transcript, changes, session_id

    result, transcript, changes, session_id = _run(scenario())
    assert result["review"]["agent_session_id"] == session_id
    assert transcript["total"] >= 2
    assert changes["session"]["id"] == session_id
    assert changes["file"]["decision"] == "changes_requested"
    assert session_id in {s.id for s in clisessions.list_all()}
    _error(api.request_changes(service, rid, {
        "repo": "modules/modulo1", "path": "algo.py", "instructions": " "}), 400)
    empty = api.review_transcript(_Service(), _start(_Service(), project, resume=False)[1]["review"]["id"])
    assert empty["entries"] == [] and empty["session"] is None


def test_end_to_end_rest_and_ws(project):
    async def scenario():
        service = ServerService(ApiConfig(enabled=True, host="127.0.0.1", port=0, tokens="t1"),
                                app=_App())
        srv = asyncio.create_task(service.run())
        for _ in range(200):
            if service.server is not None:
                break
            await asyncio.sleep(0.01)
        try:
            status, body = await _request(
                service, "POST", "/api/v1/reviews", json.dumps({"workdir": str(project)}).encode())
            assert status == 201
            rid = body["review"]["id"]
            query = urllib.parse.urlencode({"repo": "modules/modulo1", "path": "algo.py"})
            status, body = await _request(service, "GET", f"/api/v1/reviews/{rid}/diff?{query}")
            assert status == 200 and "+print(1)" in body["diff"]["text"]
            status, body = await _request(
                service, "POST", f"/api/v1/reviews/{rid}/commit",
                json.dumps({"files": [{"repo": "modules/modulo1", "path": "algo.py"}],
                            "message": "feat: add algo"}).encode())
            assert status == 200, body
            status, body = await _request(
                service, "POST", f"/api/v1/reviews/{rid}/commit",
                json.dumps({"files": [{"repo": "modules/modulo1", "path": "algo.py"}],
                            "message": "again"}).encode())
            assert status == 409 and body["code"] == "not_pending"
            reader, writer = await _ws_handshake(service.port)
            try:
                reply = await _rpc(reader, writer, {
                    "id": 1, "method": "reviews.get", "params": {"review_id": rid}})
                assert reply["result"]["review"]["id"] == rid
                reply = await _rpc(reader, writer, {
                    "id": 2, "method": "subscribe", "params": {"topics": ["reviews"]}})
                assert reply["result"]["topics"] == ["reviews"]
                reply = await _rpc(reader, writer, {
                    "id": 3, "method": "reviews.navigate",
                    "params": {"review_id": rid, "action": "first"}})
                assert reply["result"]["current"]["path"] == "README.md"
                seen = None
                for _ in range(10):
                    opcode, raw = await asyncio.wait_for(_read_server_frame(reader), timeout=3.0)
                    payload = json.loads(raw.decode("utf-8"))
                    if payload.get("event") == "review.changed":
                        seen = payload
                        break
                assert seen is not None and seen["review"]["id"] == rid
                reply = await _rpc(reader, writer, {
                    "id": 4, "method": "reviews.diff",
                    "params": {"review_id": rid, "repo": ".", "path": "nope"}})
                assert reply["error"]["code"] == "not_pending"
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, srv)

    _run(scenario())
    assert _git(project / "modules" / "modulo1", "log", "--oneline", "-1").endswith("feat: add algo")
    assert _git(project, "status", "--porcelain", "--", "README.md").strip() == "M README.md"
