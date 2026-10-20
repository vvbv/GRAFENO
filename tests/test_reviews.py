"""Tests of the assisted review sessions (store, snapshot, navigation, agent and commits)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from grafeno import gitrepos, reviews
from grafeno.clisession_runtime import CliSessionManager
from grafeno.drivers.base import CLIDriver, RunResult, TokenUsage

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


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    _init_repo(root)
    _init_repo(root / "modules" / "modulo1")
    (root / "README.md").write_text("two\n", encoding="utf-8")
    (root / "modules" / "modulo1" / "algo.py").write_text("print(1)\n", encoding="utf-8")
    return root


class FakeDriver(CLIDriver):
    name = "claude"
    display_name = "Fake"
    executable = "fake"

    def __init__(self, text: str = "ok") -> None:
        self.text = text
        self.prompts: list[str] = []

    def is_available(self) -> bool:
        return True

    async def run(self, request, on_event=None, on_activity=None, on_session=None):
        self.prompts.append(request.prompt)
        if on_session:
            on_session("native-1")
        return RunResult(ok=True, text=self.text, session_id="native-1", tokens=TokenUsage(1, 1))


def _snap(review):
    return asyncio.run(reviews.snapshot(review))


def test_roundtrip_and_store(tmp_path):
    review = reviews.create(str(tmp_path), name="R", task_id="t1")
    assert reviews.load(review.id).to_dict() == review.to_dict()
    review.files.append(reviews.FileDecision("a", "b.py", "accepted", "n", "fp", 2))
    reviews.save(review)
    assert reviews.load(review.id).files[0].decision == "accepted"
    with pytest.raises(FileNotFoundError):
        reviews.load("../x")
    assert reviews.find_active(str(tmp_path)).id == review.id
    assert reviews.find_active(str(tmp_path), "t1").id == review.id
    assert reviews.find_active(str(tmp_path), "other") is None
    other = reviews.create(str(tmp_path))
    assert other.name == "Review 2"
    assert {r.id for r in reviews.list_all(str(tmp_path))} == {review.id, other.id}
    assert reviews.delete(review.id) and not reviews.delete(review.id)
    with pytest.raises(ValueError):
        reviews.create(str(tmp_path), cli="nope")


def test_from_dict_tolerates_garbage():
    review = reviews.ReviewSession.from_dict({"id": "x", "state": "weird", "files": ["bad", {"repo": "a"}]})
    assert review.state == "active" and len(review.files) == 1


def test_snapshot_entries(project):
    review = reviews.create(str(project))
    snap = _snap(review)
    by = {(e["repo"], e["path"]): e for e in snap.entries}
    assert set(by) == {(".", "README.md"), ("modules/modulo1", "algo.py")}
    assert by[("modules/modulo1", "algo.py")]["project_path"] == "modules/modulo1/algo.py"
    assert [e["position"] for e in snap.entries] == [1, 2]
    assert reviews.counts(snap)["pending"] == 2


def test_navigate(project):
    review = reviews.create(str(project))
    snap = _snap(review)
    first = reviews.navigate(review, snap, "first")
    assert first["path"] == "README.md"
    second = reviews.navigate(review, snap, "next")
    assert second["path"] == "algo.py"
    assert reviews.navigate(review, snap, "next") is None
    assert review.cursor_path == "algo.py"
    assert reviews.navigate(review, snap, "prev")["path"] == "README.md"
    assert reviews.navigate(review, snap, "goto", "modules/modulo1", "algo.py")["path"] == "algo.py"
    with pytest.raises(ValueError):
        reviews.navigate(review, snap, "goto", ".", "nope")
    with pytest.raises(ValueError):
        reviews.navigate(review, snap, "bogus")
    reviews.navigate(review, snap, "first")
    reviews.decide(review, snap, ".", "README.md", "accepted")
    assert reviews.navigate(review, snap, "first", skip_decided=True)["path"] == "algo.py"
    review.cursor_repo, review.cursor_path = ".", "gone.txt"
    assert reviews.navigate(review, snap, "current") is not None


def test_decide_outdated_and_prune(project):
    review = reviews.create(str(project))
    snap = _snap(review)
    reviews.decide(review, snap, ".", "README.md", "accepted", "ok")
    (project / "README.md").write_text("three, longer\n", encoding="utf-8")
    snap = _snap(review)
    entry = reviews.find_entry(snap, ".", "README.md")
    assert entry["outdated"] and entry["decision"] == "accepted"
    assert reviews.counts(snap)["outdated"] == 1
    with pytest.raises(ValueError):
        reviews.decide(review, snap, ".", "missing.txt", "accepted")
    with pytest.raises(ValueError):
        reviews.decide(review, snap, ".", "README.md", "bogus")
    _git(project, "add", "README.md")
    _git(project, "commit", "-qm", "readme")
    snap = _snap(review)
    assert reviews.prune(review, snap)
    assert review.files == []


def test_discuss_and_request_changes(project):
    driver = FakeDriver()
    manager = CliSessionManager(driver_factory=lambda cli: driver)

    async def scenario():
        review = reviews.create(str(project))
        snap = await reviews.snapshot(review)
        entry = reviews.find_entry(snap, "modules/modulo1", "algo.py")
        session = await reviews.discuss(manager, review, snap, entry, "why?")
        await manager.wait(session.id)
        again = await reviews.request_changes(manager, review, snap, entry, "rename it")
        await manager.wait(again.id)
        assert again.id == session.id
        with pytest.raises(ValueError):
            await reviews.request_changes(manager, review, snap, entry, "  ")
        return review

    review = asyncio.run(scenario())
    assert len(driver.prompts) == 2
    for prompt in driver.prompts:
        assert str(project / "modules" / "modulo1") in prompt
        assert "+print(1)" in prompt
    saved = reviews.load(review.id)
    decision = saved.decision_for("modules/modulo1", "algo.py")
    assert decision.decision == "changes_requested" and decision.discussions == 1
    assert saved.agent_session_id


def test_suggest_commit_message_does_not_commit(project):
    driver = FakeDriver("```\nfeat: add algo\n```")
    review = reviews.create(str(project))
    snap = _snap(review)
    head = _git(project / "modules" / "modulo1", "rev-parse", "HEAD")
    result = asyncio.run(reviews.suggest_commit_message(
        review, snap, "modules/modulo1", ["algo.py"], driver_factory=lambda cli: driver))
    assert result["message"] == "feat: add algo"
    assert _git(project / "modules" / "modulo1", "rev-parse", "HEAD") == head
    assert str(project / "modules" / "modulo1") in driver.prompts[0]


def test_clean_commit_message():
    assert reviews.clean_commit_message('"Commit message: fix: x"') == "fix: x"
    assert reviews.clean_commit_message("a\n\n\n\nb") == "a\n\nb"


def test_commit_files(project):
    review = reviews.create(str(project))
    snap = _snap(review)
    root_head = _git(project, "rev-parse", "HEAD")
    out = asyncio.run(reviews.commit_files(
        review, snap, [{"repo": "modules/modulo1", "path": "algo.py"}], "feat: add algo"))
    assert out["errors"] == [] and not out["partial"]
    assert _git(project / "modules" / "modulo1", "log", "--oneline", "-1").endswith("feat: add algo")
    assert _git(project, "rev-parse", "HEAD") == root_head
    assert [e["path"] for e in out["snapshot"].entries] == ["README.md"]
    assert reviews.load(review.id).commits == 1
    assert reviews.load_commits(review.id)[0]["repo"] == "modules/modulo1"


def test_commit_files_two_files_same_repo(project):
    (project / "b.txt").write_text("b\n", encoding="utf-8")
    review = reviews.create(str(project))
    snap = _snap(review)
    out = asyncio.run(reviews.commit_files(
        review, snap, [{"repo": ".", "path": "README.md"}, {"repo": ".", "path": "b.txt"}], "m"))
    assert sorted(out["commits"][0]["files"]) == ["README.md", "b.txt"]


def test_commit_files_multi_repo(project):
    review = reviews.create(str(project))
    snap = _snap(review)
    files = [{"repo": ".", "path": "README.md"}, {"repo": "modules/modulo1", "path": "algo.py"}]
    heads = (_git(project, "rev-parse", "HEAD"), _git(project / "modules" / "modulo1", "rev-parse", "HEAD"))
    with pytest.raises(gitrepos.GitReviewError) as exc:
        asyncio.run(reviews.commit_files(review, snap, files, "m"))
    assert exc.value.code == "multi_repo"
    assert heads == (_git(project, "rev-parse", "HEAD"), _git(project / "modules" / "modulo1", "rev-parse", "HEAD"))
    out = asyncio.run(reviews.commit_files(review, snap, files, "m", allow_multi_repo=True))
    assert len(out["commits"]) == 2
    assert heads[0] != _git(project, "rev-parse", "HEAD")
    assert heads[1] != _git(project / "modules" / "modulo1", "rev-parse", "HEAD")


def test_commit_files_stale_and_key(project):
    review = reviews.create(str(project))
    snap = _snap(review)
    entry = reviews.find_entry(snap, ".", "README.md")
    (project / "README.md").write_text("changed, much longer\n", encoding="utf-8")
    head = _git(project, "rev-parse", "HEAD")
    with pytest.raises(gitrepos.GitReviewError) as exc:
        asyncio.run(reviews.commit_files(
            review, snap, [{"key": entry["key"], "fingerprint": entry["fingerprint"]}], "m"))
    assert exc.value.code == "stale"
    assert _git(project, "rev-parse", "HEAD") == head
    with pytest.raises(ValueError):
        asyncio.run(reviews.commit_files(review, snap, [], "m"))


def test_commit_does_not_lose_concurrent_decisions(project):
    hook = project / "modules" / "modulo1" / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nsleep 1\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    review = reviews.create(str(project))
    snap = _snap(review)

    async def scenario():
        task = asyncio.create_task(reviews.commit_files(
            review, snap, [{"repo": "modules/modulo1", "path": "algo.py"}], "feat: algo"))
        await asyncio.sleep(0.3)
        async with reviews.review_lock(review.id):
            other = reviews.load(review.id)
            reviews.decide(other, snap, ".", "README.md", "accepted", "ok")
            other.agent_session_id = "agent-1"
            reviews.save(other)
        return await task

    out = asyncio.run(scenario())
    assert out["errors"] == []
    saved = reviews.load(review.id)
    assert saved.decision_for(".", "README.md").decision == "accepted"
    assert saved.agent_session_id == "agent-1"
    assert saved.commits == 1


def test_next_after_cursor_file_vanishes_does_not_skip(project):
    (project / "b.txt").write_text("b\n", encoding="utf-8")
    (project / "c.txt").write_text("c\n", encoding="utf-8")
    review = reviews.create(str(project))
    snap = _snap(review)
    # root files sorted case-insensitively, then the nested repository
    assert [e["path"] for e in snap.entries] == ["b.txt", "c.txt", "README.md", "algo.py"]
    reviews.navigate(review, snap, "goto", ".", "c.txt")
    reviews.save(review)  # the API persists the cursor after every navigation
    asyncio.run(reviews.commit_files(review, snap, [{"repo": ".", "path": "c.txt"}], "m"))
    new_snap = _snap(review)
    # c.txt is gone: README.md took its place and must not be skipped in favour of algo.py
    assert reviews.navigate(review, new_snap, "next")["path"] == "README.md"


def test_close_during_navigate_is_not_lost(project):
    from grafeno.server import reviews as api

    class Service:
        app = None

    review = reviews.create(str(project))

    async def scenario():
        original = api._snapshot

        async def slow_snapshot(rev):
            await asyncio.sleep(0.5)  # navigate loaded the review before this point
            return await original(rev)

        api._snapshot = slow_snapshot
        try:
            nav = asyncio.create_task(api.navigate(Service(), review.id, {"action": "first"}))
            await asyncio.sleep(0.1)
            closing = asyncio.create_task(api.close_review(Service(), review.id))
            await asyncio.gather(nav, closing, return_exceptions=True)
        finally:
            api._snapshot = original

    asyncio.run(scenario())
    assert reviews.load(review.id).state == "closed"
