"""Tests for the remote module: spec parsing, mount points, effective workdir."""

from __future__ import annotations

import asyncio

from grafeno import remote


def test_parse_spec_scp_like():
    spec = remote.parse_spec("user@example.com:/home/user/project")
    assert spec is not None
    assert spec.user == "user"
    assert spec.host == "example.com"
    assert spec.path == "/home/user/project"
    assert spec.port == 0
    assert spec.canonical == "user@example.com:/home/user/project"


def test_parse_spec_without_user():
    spec = remote.parse_spec("example.com:/srv/app")
    assert spec is not None and spec.user == "" and spec.target == "example.com"


def test_parse_spec_ssh_url_with_port():
    spec = remote.parse_spec("ssh://dev@10.0.0.2:2222/opt/code/")
    assert spec is not None
    assert (spec.user, spec.host, spec.port, spec.path) == ("dev", "10.0.0.2", 2222, "/opt/code")


def test_parse_spec_local_paths_return_none():
    assert remote.parse_spec("") is None
    assert remote.parse_spec("/Users/me/project") is None
    assert remote.parse_spec("./relative") is None


def test_spec_roundtrip():
    spec = remote.parse_spec("ssh://u@h:2222/x")
    assert spec is not None
    assert remote.RemoteSpec.from_dict(spec.to_dict()) == spec


def test_mount_dir_deterministic():
    spec = remote.parse_spec("u@h:/x")
    assert spec is not None
    first = remote.mount_dir(spec)
    assert first == remote.mount_dir(spec)
    assert first.parent.name == "mounts"
    assert "h-" in first.name


def test_is_self():
    spec = remote.parse_spec("me@localhost:/tmp/x")
    assert spec is not None and remote.is_self(spec)
    other = remote.parse_spec("me@other-host:/tmp/x")
    assert other is not None and not remote.is_self(other)


def test_effective_workdir_local_passthrough(tmp_path):
    assert str(remote.effective_workdir("", str(tmp_path))) == str(tmp_path)
    assert str(remote.effective_workdir("not-remote", str(tmp_path))) == str(tmp_path)


def test_effective_workdir_self_uses_remote_path():
    assert str(remote.effective_workdir("me@localhost:/data/proj", "/ignored")) == "/data/proj"


def test_effective_workdir_remote_uses_mount_dir():
    result = remote.effective_workdir("me@remote-box:/data/proj", "/ignored")
    assert result.parent.name == "mounts"


def test_detect_os_self_returns_empty():
    spec = remote.parse_spec("me@localhost:/tmp/x")
    assert spec is not None
    assert asyncio.run(remote.detect_os(spec)) == ""


def test_detect_os_returns_first_line_normalized(monkeypatch):
    async def _fake(command, timeout):
        return 0, "  Linux   6.1.0   x86_64 \nignored second line\n"

    monkeypatch.setattr(remote, "_run_capture", _fake)
    spec = remote.parse_spec("u@h:/x")
    assert spec is not None
    assert asyncio.run(remote.detect_os(spec)) == "Linux 6.1.0 x86_64"


def test_detect_os_failure_returns_empty(monkeypatch):
    async def _fake(command, timeout):
        return -1, ""

    monkeypatch.setattr(remote, "_run_capture", _fake)
    spec = remote.parse_spec("u@h:/x")
    assert spec is not None
    assert asyncio.run(remote.detect_os(spec)) == ""
