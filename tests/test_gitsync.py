"""Tests of branch operations and pending summaries (local bare remotes, no network)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from grafeno import gitrepos, gitsync

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
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")


def _clone_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Bare remote plus two clones (``work`` is the project, ``other`` simulates a teammate)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    seed = tmp_path / "seed"
    _init_repo(seed)
    _git(seed, "branch", "-M", "main")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "-u", "origin", "main")
    work, other = tmp_path / "work", tmp_path / "other"
    for target in (work, other):
        subprocess.run(["git", "clone", "-q", str(remote), str(target)], check=True)
        _git(target, "config", "user.email", "a@b.c")
        _git(target, "config", "user.name", "t")
    return remote, work, other


def _repo(root: Path) -> gitrepos.Repo:
    return {r.id: r for r in gitrepos.discover(root)}["."]


def _commit(repo: Path, name: str, text: str, message: str = "change") -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)


def _run(coro):
    return asyncio.run(coro)


def _code(coro) -> str:
    with pytest.raises(gitrepos.GitReviewError) as info:
        _run(coro)
    return info.value.code


def test_pending_summary_nested_any_depth(tmp_path):
    root = tmp_path / "proj"
    _init_repo(root)
    _init_repo(root / "mods" / "a")
    _init_repo(root / "mods" / "a" / "sub" / "deep" / "b")
    (root / "mods" / "a" / "new.txt").write_text("n\n", encoding="utf-8")
    (root / "mods" / "a" / "sub" / "deep" / "b" / "a.txt").write_text("changed\n", encoding="utf-8")
    s = _run(gitsync.pending_summary(root, refresh=True))
    assert s.git and s.dirty and s.repos == 3 and s.files == 2
    assert sorted(s.dirty_repos) == ["mods/a", "mods/a/sub/deep/b"]
    (root / "mods" / "a" / "new.txt").unlink()
    _git(root / "mods" / "a" / "sub" / "deep" / "b", "checkout", "--", "a.txt")
    assert _run(gitsync.pending_summary(root, refresh=True)).dirty is False


def test_pending_summary_cache_and_invalidate(tmp_path):
    root = tmp_path / "proj"
    _init_repo(root)
    assert _run(gitsync.pending_summary(root, refresh=True)).dirty is False
    (root / "a.txt").write_text("dirty\n", encoding="utf-8")
    assert _run(gitsync.pending_summary(root)).dirty is False
    gitsync.invalidate(root)
    assert _run(gitsync.pending_summary(root)).dirty is True


def test_pending_summary_not_git(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    s = _run(gitsync.pending_summary(root, refresh=True))
    assert s.git is False and s.dirty is False


def test_list_branches_and_checkout(tmp_path):
    _, work, _ = _clone_pair(tmp_path)
    _git(work, "branch", "feature")
    repo = _repo(work)
    branches = _run(gitsync.list_branches(repo))
    assert branches[0].name == "main" and branches[0].current
    assert branches[0].upstream == "origin/main"
    result = _run(gitsync.checkout(repo, "feature"))
    assert result.outcome == "switched" and result.previous_branch == "main"
    assert _git(work, "rev-parse", "--abbrev-ref", "HEAD") == "feature"
    assert _run(gitsync.checkout(repo, "feature")).outcome == "unchanged"


def test_checkout_rejects_unknown_and_invalid(tmp_path):
    _, work, other = _clone_pair(tmp_path)
    _git(other, "switch", "-q", "-c", "remote-only")
    _git(other, "push", "-q", "-u", "origin", "remote-only")
    _git(work, "fetch", "-q")
    repo = _repo(work)
    assert _code(gitsync.checkout(repo, "nope")) == "branch_not_found"
    assert _code(gitsync.checkout(repo, "remote-only")) == "branch_not_found"
    assert _code(gitsync.checkout(repo, "")) == "invalid_branch"
    assert _code(gitsync.checkout(repo, "-x")) == "invalid_branch"


def test_checkout_local_changes(tmp_path):
    _, work, _ = _clone_pair(tmp_path)
    _git(work, "switch", "-q", "-c", "feature")
    _commit(work, "a.txt", "feature\n")
    _git(work, "switch", "-q", "main")
    (work / "a.txt").write_text("local\n", encoding="utf-8")
    assert _code(gitsync.checkout(_repo(work), "feature")) == "local_changes"
    assert _git(work, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_push_current_branch(tmp_path):
    remote, work, _ = _clone_pair(tmp_path)
    _commit(work, "b.txt", "b\n")
    repo = _repo(work)
    assert _run(gitsync.push(repo)).outcome == "pushed"
    assert _git(remote, "rev-parse", "main") == _git(work, "rev-parse", "HEAD")
    assert _run(gitsync.push(repo)).outcome == "up_to_date"


def test_push_new_branch_requires_set_upstream(tmp_path):
    _, work, _ = _clone_pair(tmp_path)
    _git(work, "switch", "-q", "-c", "topic")
    _commit(work, "t.txt", "t\n")
    repo = _repo(work)
    assert _code(gitsync.push(repo)) == "no_upstream"
    assert _run(gitsync.push(repo, set_upstream=True)).outcome == "created"
    assert _run(gitrepos.status(repo)).upstream == "origin/topic"


def test_push_upstream_mismatch(tmp_path):
    remote, work, _ = _clone_pair(tmp_path)
    _git(work, "switch", "-q", "-c", "feature", "origin/main")  # upstream is origin/main
    _commit(work, "f.txt", "f\n", "feature work")
    before = _git(remote, "rev-parse", "main")
    repo = _repo(work)
    assert _code(gitsync.push(repo)) == "upstream_mismatch"
    assert _git(remote, "rev-parse", "main") == before
    assert _run(gitsync.push(repo, set_upstream=True)).outcome == "created"
    assert _git(remote, "rev-parse", "feature") == _git(work, "rev-parse", "HEAD")
    assert _git(remote, "rev-parse", "main") == before


def test_push_rejected(tmp_path):
    _, work, other = _clone_pair(tmp_path)
    _commit(other, "o.txt", "o\n")
    _git(other, "push", "-q")
    _commit(work, "w.txt", "w\n")
    assert _code(gitsync.push(_repo(work))) == "push_rejected"


def test_pull_fast_forward_and_diverged(tmp_path):
    _, work, other = _clone_pair(tmp_path)
    _commit(other, "o.txt", "o\n")
    _git(other, "push", "-q")
    repo = _repo(work)
    result = _run(gitsync.pull(repo))
    assert result.outcome == "updated" and result.commits == 1
    assert _run(gitsync.pull(repo)).outcome == "up_to_date"
    _commit(work, "w.txt", "w\n")
    _commit(other, "o2.txt", "o2\n")
    _git(other, "push", "-q")
    assert _code(gitsync.pull(repo)) == "diverged"
    assert _run(gitsync.pull(repo, mode="merge")).outcome == "updated"


def test_pull_conflict_and_abort(tmp_path):
    _, work, other = _clone_pair(tmp_path)
    _commit(other, "a.txt", "other\n")
    _git(other, "push", "-q")
    _commit(work, "a.txt", "work\n")
    repo = _repo(work)
    result = _run(gitsync.pull(repo, mode="merge"))
    assert result.outcome == "conflict"
    assert result.conflicts == ["a.txt"] and result.in_progress == "merge"
    assert _code(gitsync.pull(repo)) == "operation_in_progress"
    assert _run(gitsync.abort(repo)).outcome == "aborted"
    st = _run(gitrepos.status(repo))
    assert st.operation == "" and not any(c.conflicted for c in st.files)
    assert _code(gitsync.abort(repo)) == "no_operation"


def test_pull_without_upstream_and_detached(tmp_path):
    _, work, _ = _clone_pair(tmp_path)
    repo = _repo(work)
    assert _code(gitsync.pull(repo, mode="bogus")) == "invalid_mode"
    _git(work, "switch", "-q", "-c", "local")
    assert _code(gitsync.pull(repo)) == "no_upstream"
    _git(work, "switch", "-q", "--detach")
    assert _code(gitsync.pull(repo)) == "detached"


def test_classify_failure_and_redaction():
    err = gitsync.classify_failure("fatal: Authentication failed for 'https://u:tok@h/r.git/'", "push")
    assert err.code == "auth_failed" and "tok" not in err.message
    cases = {
        "Could not resolve host: h": "remote_unreachable",
        "! [rejected] main -> main (fetch first)": "push_rejected",
        "fatal: Not possible to fast-forward, aborting.": "diverged",
        "something else": "git_failed",
    }
    for text, code in cases.items():
        assert gitsync.classify_failure(text, "pull").code == code


def test_network_timeout(tmp_path, monkeypatch):
    _, work, _ = _clone_pair(tmp_path)

    async def fake(*args, **kwargs):
        return gitsync.TIMED_OUT, ""

    monkeypatch.setattr(gitsync, "_run_process", fake)
    assert _code(gitsync.push(_repo(work))) == "timeout"
