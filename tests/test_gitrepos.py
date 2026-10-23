"""Tests of the multi-repository git core (discovery, status, diff and explicit commits)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from grafeno import gitrepos

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(repo: Path, commit: bool = True) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "t")
    if commit:
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")


def _repo_of(root: Path, repo_id: str = ".") -> gitrepos.Repo:
    return {r.id: r for r in gitrepos.discover(root)}[repo_id]


def _status(repo: gitrepos.Repo) -> gitrepos.RepoStatus:
    return asyncio.run(gitrepos.status(repo))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    _init_repo(root)
    _init_repo(root / "modules" / "modulo1")
    return root


def test_discover_nested_and_skipped(tmp_path):
    root = tmp_path / "proj"
    _init_repo(root)
    _init_repo(root / "modules" / "modulo1")
    (root / "vendor" / "lib").mkdir(parents=True)
    (root / "vendor" / "lib" / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    (root / "node_modules" / "x" / ".git").mkdir(parents=True)
    repos = gitrepos.discover(root)
    assert [r.id for r in repos] == [".", "modules/modulo1", "vendor/lib"]
    assert [r.kind for r in repos] == ["root", "nested", "submodule"]
    assert repos[0].excluded == ("modules/modulo1", "vendor/lib")
    assert repos[1].parent == "."


def test_discover_outer_repo(tmp_path):
    top = tmp_path / "top"
    _init_repo(top)
    sub = top / "sub"
    sub.mkdir()
    repos = gitrepos.discover(sub)
    assert len(repos) == 1
    assert repos[0].kind == "outer" and repos[0].scope == "sub" and repos[0].id == "."
    assert gitrepos.project_path(repos[0], "sub/x.py") == "x.py"


def test_status_isolates_nested_repo(project):
    nested = project / "modules" / "modulo1"
    (nested / "algo.py").write_text("print(1)\n", encoding="utf-8")
    (project / "a.txt").write_text("two\n", encoding="utf-8")
    root_status = _status(_repo_of(project))
    assert [f.path for f in root_status.files] == ["a.txt"]
    nested_repo = _repo_of(project, "modules/modulo1")
    nested_status = _status(nested_repo)
    assert [(f.repo, f.path) for f in nested_status.files] == [("modules/modulo1", "algo.py")]
    assert gitrepos.project_path(nested_repo, "algo.py") == "modules/modulo1/algo.py"


def test_parse_status_v2():
    repo = gitrepos.Repo(".", Path("/x"), "root")
    raw = (
        b"# branch.oid (initial)\0# branch.head (detached)\0"
        b"1 .M N... 100644 100644 100644 h1 h2 m.py\0"
        b"1 A. N... 000000 100644 100644 h1 h2 add.py\0"
        b"1 .D N... 100644 100644 000000 h1 h2 gone.py\0"
        b"2 R. N... 100644 100644 100644 h1 h2 R100 new.py\0old.py\0"
        b"u UU N... 100644 100644 100644 100644 h1 h2 h3 conf.py\0"
        b"? free.txt\0? sub/\0"
    )
    st = gitrepos.parse_status_v2(raw, repo)
    assert st.head == "" and st.branch == ""
    kinds = {f.path: f.kind for f in st.files}
    assert kinds == {
        "add.py": "added", "conf.py": "conflicted", "free.txt": "untracked",
        "gone.py": "deleted", "m.py": "modified", "new.py": "renamed",
    }
    renamed = next(f for f in st.files if f.path == "new.py")
    assert renamed.orig_path == "old.py"
    assert st.hidden == ["sub"]


def test_diff_variants(project):
    (project / "new.txt").write_text("hello\n", encoding="utf-8")
    (project / "a.txt").write_text("two\n", encoding="utf-8")
    (project / "bin.dat").write_bytes(b"\0\1\2")
    (project / "big.txt").write_text("x" * 5000 + "\n", encoding="utf-8")
    repo = _repo_of(project)
    st = _status(repo)
    by_path = {f.path: f for f in st.files}
    new = asyncio.run(gitrepos.diff(repo, by_path["new.txt"], True))
    assert "+hello" in new.text and not new.binary
    mod = asyncio.run(gitrepos.diff(repo, by_path["a.txt"], True))
    assert "-one" in mod.text and "+two" in mod.text
    binary = asyncio.run(gitrepos.diff(repo, by_path["bin.dat"], True))
    assert binary.binary
    big = asyncio.run(gitrepos.diff(repo, by_path["big.txt"], True, max_bytes=1024))
    assert big.truncated and big.size > 1024 and len(big.text) <= 1024


def test_diff_renamed(project):
    _git(project, "mv", "a.txt", "b.txt")
    repo = _repo_of(project)
    change = _status(repo).files[0]
    assert change.kind == "renamed" and change.orig_path == "a.txt"
    result = asyncio.run(gitrepos.diff(repo, change, True))
    assert "rename" in result.text


def test_commit_keeps_other_staged_and_unstaged(project):
    (project / "b.txt").write_text("b\n", encoding="utf-8")
    (project / "c.txt").write_text("c\n", encoding="utf-8")
    _git(project, "add", "b.txt", "c.txt")
    _git(project, "commit", "-qm", "more")
    (project / "a.txt").write_text("changed\n", encoding="utf-8")
    (project / "b.txt").write_text("b2\n", encoding="utf-8")
    (project / "c.txt").write_text("c2\n", encoding="utf-8")
    _git(project, "add", "c.txt")
    repo = _repo_of(project)
    result = asyncio.run(gitrepos.commit(repo, ["a.txt"], "edit a"))
    assert result.files == ["a.txt"]
    assert result.preserved_staged == ["c.txt"]
    assert _git(project, "diff", "--cached", "--name-only") == "c.txt"
    assert _git(project, "diff", "--name-only") == "b.txt"


def test_commit_goes_to_nested_repo(project):
    nested = project / "modules" / "modulo1"
    (nested / "algo.py").write_text("print(1)\n", encoding="utf-8")
    head = _git(project, "rev-parse", "HEAD")
    repo = _repo_of(project, "modules/modulo1")
    asyncio.run(gitrepos.commit(repo, ["algo.py"], "feat: add algo"))
    assert _git(nested, "log", "--oneline", "-1").endswith("feat: add algo")
    assert _git(project, "rev-parse", "HEAD") == head


def test_commit_untracked_deleted_renamed(project):
    (project / "b.txt").write_text("b\n", encoding="utf-8")
    _git(project, "add", "b.txt")
    _git(project, "commit", "-qm", "b")
    (project / "new.txt").write_text("n\n", encoding="utf-8")
    repo = _repo_of(project)
    asyncio.run(gitrepos.commit(repo, ["new.txt"], "add new"))
    assert "new.txt" in _git(project, "show", "--name-only", "--format=", "HEAD")
    (project / "b.txt").unlink()
    asyncio.run(gitrepos.commit(repo, ["b.txt"], "drop b"))
    assert _git(project, "show", "--name-status", "--format=", "HEAD").startswith("D")
    _git(project, "mv", "a.txt", "a2.txt")
    change = _status(repo).files[0]
    result = asyncio.run(gitrepos.commit(repo, [change.path], "rename a"))
    assert sorted(result.files) == ["a.txt", "a2.txt"]


def test_commit_in_unborn_repo_preserves_staged(tmp_path):
    root = tmp_path / "fresh"
    _init_repo(root, commit=False)
    (root / "x.txt").write_text("x\n", encoding="utf-8")
    (root / "y.txt").write_text("y\n", encoding="utf-8")
    (root / "z.txt").write_text("z\n", encoding="utf-8")
    _git(root, "add", "y.txt")
    repo = _repo_of(root)
    result = asyncio.run(gitrepos.commit(repo, ["x.txt"], "first"))
    assert result.files == ["x.txt"]
    assert result.preserved_staged == ["y.txt"]


def test_commit_errors(project):
    repo = _repo_of(project)
    for bad in ("nothere.txt", "../x"):
        with pytest.raises(gitrepos.GitReviewError) as exc:
            asyncio.run(gitrepos.commit(repo, [bad], "m"))
        assert exc.value.code in ("not_pending", "invalid_path")
    (project / "a.txt").write_text("two\n", encoding="utf-8")
    with pytest.raises(gitrepos.GitReviewError) as exc:
        asyncio.run(gitrepos.commit(repo, ["a.txt"], "  \n"))
    assert exc.value.code == "empty_message"
    _git(project, "add", "a.txt")
    (project / "a.txt").write_text("three\n", encoding="utf-8")
    with pytest.raises(gitrepos.GitReviewError) as exc:
        asyncio.run(gitrepos.commit(repo, ["a.txt"], "m"))
    assert exc.value.code == "partially_staged"
    change = _status(repo).files[0]
    old = gitrepos.fingerprint(repo, change)
    (project / "a.txt").write_text("four, longer\n", encoding="utf-8")
    with pytest.raises(gitrepos.GitReviewError) as exc:
        asyncio.run(gitrepos.commit(repo, ["a.txt"], "m", include_unstaged=True,
                                    expected={"a.txt": old}))
    assert exc.value.code == "stale"
    asyncio.run(gitrepos.commit(repo, ["a.txt"], "m", include_unstaged=True))
    assert _git(project, "status", "--porcelain", "--", "a.txt") == ""


def test_commit_blocked_during_merge(project):
    (project / "a.txt").write_text("two\n", encoding="utf-8")
    head = _git(project, "rev-parse", "HEAD")
    (project / ".git" / "MERGE_HEAD").write_text(head + "\n", encoding="utf-8")
    with pytest.raises(gitrepos.GitReviewError) as exc:
        asyncio.run(gitrepos.commit(_repo_of(project), ["a.txt"], "m"))
    assert exc.value.code == "operation_in_progress"


def test_failing_hook_rolls_back_add(project):
    hook = project / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    (project / "new.txt").write_text("n\n", encoding="utf-8")
    with pytest.raises(gitrepos.GitReviewError) as exc:
        asyncio.run(gitrepos.commit(_repo_of(project), ["new.txt"], "m"))
    assert exc.value.code == "commit_failed"
    assert _git(project, "status", "--porcelain", "--", "new.txt").strip() == "?? new.txt"


def test_validate_rel_path():
    assert gitrepos.validate_rel_path("a//b/./c.py") == "a/b/c.py"
    for bad in ("", "/abs", "../x", "a/../b", "-rf", "a\0b"):
        with pytest.raises(gitrepos.GitReviewError):
            gitrepos.validate_rel_path(bad)


def test_commit_copy_does_not_take_source(project):
    _git(project, "config", "status.renames", "copies")
    _git(project, "config", "diff.renames", "copies")
    body = "".join(f"line {i}\n" for i in range(60))
    (project / "a.txt").write_text(body, encoding="utf-8")
    _git(project, "add", "a.txt")
    _git(project, "commit", "-qm", "long a")
    (project / "a.txt").write_text(body + "extra\n", encoding="utf-8")
    _git(project, "add", "a.txt")
    (project / "b.txt").write_text(body, encoding="utf-8")
    _git(project, "add", "b.txt")
    repo = _repo_of(project)
    change = next(f for f in _status(repo).files if f.path == "b.txt")
    # guard: without a real copy record the test would not cover the regression
    assert change.kind == "copied" and change.orig_path == "a.txt"
    result = asyncio.run(gitrepos.commit(repo, ["b.txt"], "add b"))
    assert result.files == ["b.txt"]
    assert result.preserved_staged == ["a.txt"]
    assert _git(project, "diff", "--cached", "--name-only") == "a.txt"


def test_copy_diff_excludes_source(project):
    _git(project, "config", "status.renames", "copies")
    _git(project, "config", "diff.renames", "copies")
    body = "".join(f"line {i}\n" for i in range(60))
    (project / "a.txt").write_text(body, encoding="utf-8")
    _git(project, "add", "a.txt")
    _git(project, "commit", "-qm", "long a")
    (project / "a.txt").write_text(body + "SOURCE-ONLY\n", encoding="utf-8")
    _git(project, "add", "a.txt")
    (project / "b.txt").write_text(body, encoding="utf-8")
    _git(project, "add", "b.txt")
    repo = _repo_of(project)
    change = next(f for f in _status(repo).files if f.path == "b.txt")
    assert change.kind == "copied"
    assert "SOURCE-ONLY" not in asyncio.run(gitrepos.diff(repo, change, True)).text


def test_partial_diff_parts_are_clipped(project):
    big = "".join(f"line {i}\n" for i in range(5000))
    (project / "a.txt").write_text(big, encoding="utf-8")
    _git(project, "add", "a.txt")
    (project / "a.txt").write_text(big + "tail\n" + big, encoding="utf-8")
    repo = _repo_of(project)
    change = _status(repo).files[0]
    assert change.partially_staged
    result = asyncio.run(gitrepos.diff(repo, change, True, max_bytes=1024))
    assert result.truncated
    assert len(result.staged_text.encode()) <= 1024 and len(result.unstaged_text.encode()) <= 1024
    plain = asyncio.run(gitrepos.diff(repo, change, True, max_bytes=1024, split=False))
    assert plain.staged_text == "" and plain.unstaged_text == ""


def test_discover_any_depth(tmp_path):
    root = tmp_path / "proj"
    _init_repo(root)
    deep = root / "a/b/c/d/e/f/g/h/i/j/deep"
    _init_repo(deep)
    _init_repo(root / "x" / "y")
    repos, complete = gitrepos.discover_all(root)
    ids = [r.id for r in repos]
    assert "a/b/c/d/e/f/g/h/i/j/deep" in ids and "x/y" in ids
    assert complete is True
    assert [r.id for r in gitrepos.discover(root)] == ids


def test_discover_budget_marks_incomplete(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    _init_repo(root)
    for name in "abcde":
        (root / name).mkdir()
    monkeypatch.setattr(gitrepos, "MAX_SCAN_DIRS", 2)
    assert gitrepos.discover_all(root)[1] is False


def test_parse_status_upstream_ahead_behind():
    repo = gitrepos.Repo(".", Path("/x"), "root")
    head = [b"# branch.oid " + b"a" * 40, b"# branch.head main"]
    raw = b"\0".join([*head, b"# branch.upstream origin/main", b"# branch.ab +2 -1", b""])
    st = gitrepos.parse_status_v2(raw, repo)
    assert (st.upstream, st.ahead, st.behind, st.upstream_gone) == ("origin/main", 2, 1, False)
    raw = b"\0".join([*head, b"# branch.upstream origin/main", b""])
    assert gitrepos.parse_status_v2(raw, repo).upstream_gone is True
    st = gitrepos.parse_status_v2(b"\0".join([*head, b""]), repo)
    assert st.upstream == "" and st.upstream_gone is False
