"""Tests for projectsetup: validation, workspace resolution and creation."""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from grafeno import projectsetup, workspaces
from grafeno.projectsetup import ProjectError

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _code(func, *args, **kwargs) -> str:
    with pytest.raises(ProjectError) as info:
        func(*args, **kwargs)
    return info.value.code


@pytest.mark.parametrize("name", ["demo", "Mi proyecto", "gestion-ñandu"])
def test_validate_name_accepts(name):
    assert projectsetup.validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "   ", ".", "..", ".hidden", "-x", "a/b", "a\\b", "a:b", "a\x00b", "con", "CON.txt", "x.", "a" * 101],
)
def test_validate_name_rejects(name):
    assert _code(projectsetup.validate_name, name) == "invalid_name"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/org/repo.git",
        "http://host:8080/r",
        "ssh://git@host:22/org/r.git",
        "git@github.com:org/repo.git",
    ],
)
def test_validate_url_accepts(url):
    assert projectsetup.validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/r", "git://host/r", "ext::sh -c touch% /tmp/x", "/tmp/repo",
        "C:\\repo", "c:/repo", "-uhttps://x/y", "https://-oProxy/x", "https://host",
        "https://host/ r", "ssh://host:abc/r",
    ],
)
def test_validate_url_rejects(url):
    assert _code(projectsetup.validate_url, url) == "invalid_url"


def test_redact_url():
    assert projectsetup.redact_url("https://u:tok@host/r.git") == "https://u:***@host/r.git"
    assert projectsetup.redact_url("https://tok@host/r.git") == "https://***@host/r.git"
    assert projectsetup.redact_url("git@host:org/r.git") == "git@host:org/r.git"


def test_name_from_url():
    assert projectsetup.name_from_url("https://github.com/org/repo.git") == "repo"
    assert projectsetup.name_from_url("git@host:org/My.Repo.git") == "My.Repo"
    assert projectsetup.name_from_url("https://host/org/repo/") == "repo"


def test_resolve_mode():
    url = "https://h/r"
    assert projectsetup.resolve_mode("", "") == "init"
    assert projectsetup.resolve_mode("", url) == "clone"
    assert projectsetup.resolve_mode("PLAIN", "") == "plain"
    assert _code(projectsetup.resolve_mode, "clone", "") == "invalid_url"
    assert _code(projectsetup.resolve_mode, "init", url) == "invalid_mode"
    assert _code(projectsetup.resolve_mode, "zip", "") == "invalid_mode"


def test_resolve_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    one = tmp_path / "one"
    two = tmp_path / "two"
    other = tmp_path / "otra"
    for folder in (one, two, other):
        folder.mkdir()
    single = workspaces.roots([str(one)])
    assert projectsetup.resolve_workspace("", single) == one.resolve()
    both = workspaces.roots([str(one), str(two)])
    assert _code(projectsetup.resolve_workspace, "", both) == "invalid_workspace"
    tilde = workspaces.roots(["~/two"])
    assert projectsetup.resolve_workspace("~/two", tilde) == two.resolve()
    assert _code(projectsetup.resolve_workspace, str(other), both) == "invalid_workspace"
    assert _code(projectsetup.resolve_workspace, "", workspaces.roots([str(tmp_path / "x")])) == "no_workspace"


def _roots(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return workspaces.roots([str(ws)]), ws.resolve()


def test_create_plain(tmp_path):
    roots, ws = _roots(tmp_path)
    created = asyncio.run(projectsetup.create_project("demo", roots, mode="plain"))
    assert created.path == ws / "demo"
    assert created.git is False
    assert not (created.path / ".git").exists()


@needs_git
def test_create_init(tmp_path):
    roots, ws = _roots(tmp_path)
    created = asyncio.run(projectsetup.create_project("demo", roots))
    assert created.git is True
    assert (ws / "demo" / ".git").exists()


def test_create_duplicate_keeps_original(tmp_path):
    roots, ws = _roots(tmp_path)
    asyncio.run(projectsetup.create_project("demo", roots, mode="plain"))
    (ws / "demo" / "keep.txt").write_text("x")
    with pytest.raises(ProjectError) as info:
        asyncio.run(projectsetup.create_project("demo", roots, mode="plain"))
    assert info.value.code == "exists"
    assert (ws / "demo" / "keep.txt").exists()


def test_create_git_missing(tmp_path, monkeypatch):
    roots, ws = _roots(tmp_path)
    monkeypatch.setattr(projectsetup, "git_available", lambda: False)
    with pytest.raises(ProjectError) as info:
        asyncio.run(projectsetup.create_project("demo", roots))
    assert info.value.code == "git_missing"
    assert not (ws / "demo").exists()
    asyncio.run(projectsetup.create_project("demo", roots, mode="plain"))
    assert (ws / "demo").is_dir()


@needs_git
def test_create_clone_local(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "--quiet", str(origin)], check=True)
    subprocess.run(
        ["git", "-C", str(origin), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "--allow-empty", "-m", "init", "--quiet"],
        check=True,
    )
    monkeypatch.setattr(projectsetup, "ALLOWED_PROTOCOLS", "file")
    monkeypatch.setattr(projectsetup, "validate_url", lambda raw: raw.strip())
    roots, ws = _roots(tmp_path)
    created = asyncio.run(projectsetup.create_project("", roots, url=origin.as_uri()))
    assert created.name == "origin"
    assert (created.path / ".git").exists()


def test_clone_failure_redacts_and_cleans(tmp_path, monkeypatch):
    async def fake(args, cwd, timeout):
        return 128, "fatal: could not read from https://u:tok@host/r.git"

    monkeypatch.setattr(projectsetup, "_run_git", fake)
    monkeypatch.setattr(projectsetup, "git_available", lambda: True)
    roots, ws = _roots(tmp_path)
    with pytest.raises(ProjectError) as info:
        asyncio.run(projectsetup.create_project("r", roots, url="https://u:tok@host/r.git"))
    assert info.value.code == "clone_failed"
    assert "***" in info.value.message and "tok" not in info.value.message
    assert not (ws / "r").exists()


def test_git_env(monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    env = projectsetup._git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == "http:https:ssh"
    assert "BatchMode" in env["GIT_SSH_COMMAND"]
    monkeypatch.setenv("GIT_SSH_COMMAND", "custom")
    assert projectsetup._git_env()["GIT_SSH_COMMAND"] == "custom"
