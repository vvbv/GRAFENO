"""Tests of the gitops module (branch helpers and changes report)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from grafeno.pipeline import gitops


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_repo(repo: Path) -> str:
    """Create a single-commit repo and return the commit hash."""
    _git(repo, "init")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("primera linea\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return _git(repo, "rev-parse", "HEAD")


def test_current_head_not_a_repo(tmp_path):
    """``current_head`` returns "" on a plain directory."""
    assert gitops.current_head(tmp_path) == ""
    assert gitops.changes_markdown(tmp_path, "") == ""


def test_changes_markdown_committed_and_uncommitted(tmp_path):
    """The report includes a new commit, an unstaged edit and an untracked file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _init_repo(repo)

    (repo / "a.txt").write_text("primera linea\nsegunda linea\n", encoding="utf-8")
    (repo / "nuevo.txt").write_text("contenido nuevo\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "anade b")

    doc = gitops.changes_markdown(repo, base)

    assert doc.startswith("# Cambios de la tarea")
    assert "anade b" in doc
    assert "nuevo.txt" in doc
    assert "contenido nuevo" in doc
    assert "## Diff" in doc
    assert "+segunda linea" in doc
    assert "## Archivos nuevos sin seguimiento" in doc


def test_changes_markdown_fallback_base_invalida(tmp_path):
    """An invalid base falls back to HEAD; the report still gets generated."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.txt").write_text("primera linea\nsegunda linea\n", encoding="utf-8")
    (repo / "nuevo.txt").write_text("contenido nuevo\n", encoding="utf-8")

    doc = gitops.changes_markdown(repo, "deadbeef")

    assert doc.startswith("# Cambios de la tarea")
    assert "## Diff" in doc
    assert "Aviso" in doc  # the fallback notice
    assert "nuevo.txt" in doc


def test_changes_markdown_empty_repo(tmp_path):
    """A repo with no commits yields an empty document."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "t")

    assert gitops.changes_markdown(repo, "") == ""


def test_changes_markdown_clean_repo(tmp_path):
    """A repo with no changes since ``base`` still produces a valid report."""
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _init_repo(repo)

    doc = gitops.changes_markdown(repo, base)

    assert doc.startswith("# Cambios de la tarea")
    assert "(limpio)" in doc
    assert "(sin cambios)" in doc
    assert "(ninguno)" in doc


def test_changes_markdown_never_raises(tmp_path):
    """The helper swallows git errors instead of propagating them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Plain directory masquerading as a repo: is_git_repo is False -> "".
    assert gitops.changes_markdown(repo, "anything") == ""