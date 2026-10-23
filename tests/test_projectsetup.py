"""Tests for projectsetup: validation, workspace resolution and creation."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time

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
    assert "ConnectTimeout=" in env["GIT_SSH_COMMAND"]
    assert env["GIT_HTTP_LOW_SPEED_LIMIT"] == "1000"
    monkeypatch.setenv("GIT_HTTP_LOW_SPEED_TIME", "5")
    assert projectsetup._git_env()["GIT_HTTP_LOW_SPEED_TIME"] == "5"
    monkeypatch.setenv("GIT_ASKPASS", "/x")
    assert projectsetup._git_env()["GIT_ASKPASS"] == ""
    monkeypatch.setenv("GIT_SSH_COMMAND", "custom")
    assert projectsetup._git_env()["GIT_SSH_COMMAND"] == "custom"


def test_validate_name_rejects_long_utf8():
    assert _code(projectsetup.validate_name, "\U0001F600" * 70) == "invalid_name"


posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX process semantics")


def _tree_code(tmp_path) -> str:
    pid_file = tmp_path / "g.pid"
    grandchild = (
        "import os, time, pathlib\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    return (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "time.sleep(60)\n"
    )


def _assert_grandchild_dead(tmp_path) -> None:
    pid_file = tmp_path / "g.pid"
    if not pid_file.exists():
        pytest.skip("grandchild did not start in time")
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        status = f"/proc/{pid}/status"
        if os.path.exists(status) and "State:\tZ" in open(status).read():
            return
        time.sleep(0.05)
    raise AssertionError("grandchild still alive")


def test_spawn_options_per_platform(monkeypatch):
    monkeypatch.setattr(projectsetup.os, "name", "nt")
    assert "creationflags" in projectsetup._spawn_options()
    monkeypatch.setattr(projectsetup.os, "name", "posix")
    assert projectsetup._spawn_options() == {"start_new_session": True}


@posix_only
def test_run_process_has_no_controlling_session(tmp_path):
    code = "import os; print(os.getsid(0) == os.getpid())"
    rc, out = asyncio.run(
        projectsetup._run_process([sys.executable, "-c", code], tmp_path, 10, dict(os.environ))
    )
    assert (rc, out) == (0, "True")


@posix_only
def test_timeout_kills_process_tree_and_returns_fast(tmp_path):
    start = time.monotonic()
    rc, _out = asyncio.run(
        projectsetup._run_process([sys.executable, "-c", _tree_code(tmp_path)], tmp_path, 1.0, dict(os.environ))
    )
    assert rc == projectsetup.TIMED_OUT
    assert time.monotonic() - start < 1.0 + projectsetup.KILL_GRACE + 2
    _assert_grandchild_dead(tmp_path)


@posix_only
def test_cancel_kills_process_tree(tmp_path):
    start = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(
            asyncio.wait_for(
                projectsetup._run_process(
                    [sys.executable, "-c", _tree_code(tmp_path)], tmp_path, 30, dict(os.environ)
                ),
                1.0,
            )
        )
    assert time.monotonic() - start < 1.0 + projectsetup.KILL_GRACE + 2
    _assert_grandchild_dead(tmp_path)


def test_create_timeout_maps_to_timeout_code(tmp_path, monkeypatch):
    async def fake(args, cwd, timeout):
        return projectsetup.TIMED_OUT, "timed out after 600 s"

    monkeypatch.setattr(projectsetup, "_run_git", fake)
    monkeypatch.setattr(projectsetup, "git_available", lambda: True)
    roots, ws = _roots(tmp_path)
    with pytest.raises(ProjectError) as info:
        asyncio.run(projectsetup.create_project("r", roots, url="https://host/r.git"))
    assert info.value.code == "timeout"
    assert not (ws / "r").exists()


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs a non-root POSIX user"
)
def test_remove_tree_clears_read_only(tmp_path):
    d = tmp_path / "d"
    sub = d / "sub"
    sub.mkdir(parents=True)
    f = sub / "f.txt"
    f.write_text("x")
    os.chmod(f, 0o400)
    os.chmod(sub, 0o500)
    projectsetup._remove_tree(d)
    assert not d.exists()


@needs_git
def test_create_init_name_with_spaces_and_unicode(tmp_path):
    roots, _ws = _roots(tmp_path)
    created = asyncio.run(projectsetup.create_project("Mi proyecto \u00f1andú", roots))
    assert (created.path / ".git").exists()


def test_resolve_workspace_accepts_trailing_separator(tmp_path):
    roots, ws = _roots(tmp_path)
    assert projectsetup.resolve_workspace(str(ws) + os.sep, roots) == ws
    if os.altsep:
        assert projectsetup.resolve_workspace(str(ws) + os.altsep, roots) == ws


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs a non-root POSIX user"
)
def test_remove_tree_handles_unreadable_directory(tmp_path):
    d = tmp_path / "d"
    sub = d / "locked"
    sub.mkdir(parents=True)
    (sub / "f.txt").write_text("x")
    os.chmod(sub, 0o000)
    projectsetup._remove_tree(d)  # must not raise (no TypeError from os.open)
    assert not d.exists()


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX permissions")
def test_remove_tree_keeps_workspace_root_mode(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    d = ws / "proj"
    d.mkdir(parents=True)
    (d / "stuck.txt").write_text("x")
    os.chmod(ws, 0o755)
    real_unlink = os.unlink

    def fake_unlink(path, *args, **kwargs):
        if os.path.basename(str(path)) == "stuck.txt":
            raise PermissionError("locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(projectsetup.os, "unlink", fake_unlink)
    projectsetup._remove_tree(d)
    assert (os.stat(ws).st_mode & 0o777) == 0o755
