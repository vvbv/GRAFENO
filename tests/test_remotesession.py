"""Tests for the remote session module (session bootstrap and helpers)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import pytest

from grafeno import remote, remotesession
from grafeno.remote import RemoteSpec


# ---------------------------------------------------------------------- #
# parse_host_spec
# ---------------------------------------------------------------------- #
def test_parse_host_spec_user_at_host():
    spec = remotesession.parse_host_spec("root@192.168.1.34")
    assert spec is not None
    assert spec.user == "root"
    assert spec.host == "192.168.1.34"
    assert spec.port == 0
    assert spec.path == ""


def test_parse_host_spec_bare_host():
    spec = remotesession.parse_host_spec("host.local")
    assert spec is not None
    assert spec.user == "" and spec.host == "host.local" and spec.port == 0


def test_parse_host_spec_with_port():
    spec = remotesession.parse_host_spec("root@host:2222")
    assert spec is not None
    assert spec.port == 2222


def test_parse_host_spec_ssh_url():
    spec = remotesession.parse_host_spec("ssh://root@host:2222")
    assert spec is not None
    assert spec.user == "root" and spec.host == "host" and spec.port == 2222


def test_parse_host_spec_ssh_url_with_slash():
    spec = remotesession.parse_host_spec("ssh://root@host/")
    assert spec is not None
    assert spec.path == ""


def test_parse_host_spec_scp_like_rejected():
    """A scp-like host:/path spec is NOT a session spec (it's a task remote)."""
    assert remotesession.parse_host_spec("root@host:/var/www") is None


def test_parse_host_spec_local_path_rejected():
    assert remotesession.parse_host_spec("/local/path") is None
    assert remotesession.parse_host_spec("./relative") is None


def test_parse_host_spec_empty_rejected():
    assert remotesession.parse_host_spec("") is None


# ---------------------------------------------------------------------- #
# spec_for_task / describe_target
# ---------------------------------------------------------------------- #
def test_spec_for_task_without_session_returns_none():
    task = type("T", (), {"remote": "", "workdir": "/x"})()
    assert remotesession.spec_for_task(task) is None  # type: ignore[arg-type]


def test_spec_for_task_with_explicit_remote_wins(tmp_path):
    task = type("T", (), {"remote": "u@h:/srv", "workdir": "/ignored"})()
    remote.set_session(
        RemoteSpec(user="x", host="h", path="/home/x"),
        mounts_base=tmp_path,
    )
    try:
        spec = remotesession.spec_for_task(task)  # type: ignore[arg-type]
        assert spec is not None
        assert spec.target == "u@h"
        assert spec.path == "/srv"
    finally:
        remote.clear_session()


def test_spec_for_task_session_relative_workdir(tmp_path):
    remote.set_session(
        RemoteSpec(user="root", host="h", path="/root"),
        mounts_base=tmp_path,
    )
    remotesession._current = remotesession.RemoteSession(
        spec=RemoteSpec(user="root", host="h", path="/root"),
        remote_home="/root",
    )
    try:
        task = type("T", (), {"remote": "", "workdir": "project/sub"})()
        spec = remotesession.spec_for_task(task)  # type: ignore[arg-type]
        assert spec is not None
        assert spec.path == "/root/project/sub"
        # Absolute workdir is preserved.
        task_abs = type("T", (), {"remote": "", "workdir": "/srv/app"})()
        spec_abs = remotesession.spec_for_task(task_abs)  # type: ignore[arg-type]
        assert spec_abs is not None and spec_abs.path == "/srv/app"
    finally:
        remote.clear_session()
        remotesession._current = None


def test_describe_target_falls_back_to_session_target():
    remotesession._current = remotesession.RemoteSession(
        spec=RemoteSpec(user="root", host="h", path="/root"),
        remote_home="/root",
    )
    try:
        task = type("T", (), {"remote": "", "workdir": "/x"})()
        assert remotesession.describe_target(task) == "root@h"  # type: ignore[arg-type]
        task_remote = type("T", (), {"remote": "u@h:/y", "workdir": "/x"})()
        assert remotesession.describe_target(task_remote) == "u@h:/y"  # type: ignore[arg-type]
    finally:
        remotesession._current = None


# ---------------------------------------------------------------------- #
# bootstrap / activate / deactivate
# ---------------------------------------------------------------------- #
def test_bootstrap_and_activate(monkeypatch, tmp_path):
    home_mount = tmp_path / "home-mount"
    home_mount.mkdir()

    async def fake_run_command(spec, command, timeout):
        return 0, "/root\n"

    async def fake_mount(spec, on_info: Callable[[str], None] = lambda m: None):
        spec_mount = remote.mount_dir(spec)
        spec_mount.mkdir(parents=True, exist_ok=True)
        return True

    async def fake_detect_os(spec):
        return "Linux x86_64"

    monkeypatch.setattr(remote, "sshfs_available", lambda: True)
    monkeypatch.setattr(remote, "run_remote_command", fake_run_command)
    monkeypatch.setattr(remote, "ensure_mounted", fake_mount)
    monkeypatch.setattr(remote, "detect_os", fake_detect_os)
    # Redirect mount_dir to a tmp_path-based location so the test does not
    # try to create real directories under the simulated remote $HOME.
    monkeypatch.setattr(remote, "mount_dir", lambda spec: tmp_path / "grafeno-home")
    monkeypatch.setattr(remotesession, "sessions_base", lambda: tmp_path / "sessions")

    spec = RemoteSpec(user="root", host="remote", path="")
    session = asyncio.run(
        remotesession.bootstrap(spec, identity="", password="", on_info=lambda m: None)
    )

    assert session.remote_home == "/root"
    assert session.spec.path == "/root"
    assert session.remote_os == "Linux x86_64"
    assert session.home_mount is not None
    assert session.home_mount.exists()

    remotesession.activate(session)
    try:
        import os

        assert os.environ.get("GRAFENO_HOME") == str(session.home_mount)
        assert remotesession.active()
        assert remotesession.label() == "root@remote"
    finally:
        remotesession.deactivate()
        assert not remotesession.active()


def test_bootstrap_connect_fail(monkeypatch):
    async def fake_run_command(spec, command, timeout):
        return 1, ""

    monkeypatch.setattr(remote, "sshfs_available", lambda: True)
    monkeypatch.setattr(remote, "run_remote_command", fake_run_command)
    monkeypatch.setattr(remotesession, "sessions_base", lambda: Path("/tmp"))

    spec = RemoteSpec(user="root", host="remote", path="")
    with pytest.raises(remotesession.SessionError):
        asyncio.run(remotesession.bootstrap(spec))
    # State is cleared so the next test starts clean.
    remotesession.deactivate()
