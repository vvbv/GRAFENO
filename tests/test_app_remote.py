"""Tests for the remote-session CLI of the grafeno entry point."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from grafeno import app as app_module
from grafeno import remotesession


# ---------------------------------------------------------------------- #
# Password resolution: flag, env, prompt
# ---------------------------------------------------------------------- #
def test_resolve_remote_password_from_flag():
    assert app_module._resolve_remote_password("s3cret") == "s3cret"


def test_resolve_remote_password_from_env(monkeypatch):
    monkeypatch.setenv("GRAFENO_REMOTE_PASSWORD", "env-pwd")
    assert app_module._resolve_remote_password("") == "env-pwd"


def test_resolve_remote_password_dash_prompts(monkeypatch):
    monkeypatch.delenv("GRAFENO_REMOTE_PASSWORD", raising=False)
    monkeypatch.setattr(
        "getpass.getpass", lambda prompt="": "prompted-pwd"
    )
    assert app_module._resolve_remote_password("-") == "prompted-pwd"


def test_resolve_remote_password_empty_returns_empty(monkeypatch):
    monkeypatch.delenv("GRAFENO_REMOTE_PASSWORD", raising=False)
    assert app_module._resolve_remote_password("") == ""


# ---------------------------------------------------------------------- #
# Window title reflects an active session
# ---------------------------------------------------------------------- #
def test_window_title_session(monkeypatch, tmp_path):
    from grafeno import remote

    monkeypatch.setattr(remote, "sshfs_available", lambda: True)
    remote.set_session(
        remote.RemoteSpec(user="root", host="host", path="/root"),
        mounts_base=tmp_path,
    )
    remotesession._current = remotesession.RemoteSession(
        spec=remote.RemoteSpec(user="root", host="host", path="/root"),
        remote_home="/root",
    )
    try:
        assert app_module._window_title() == "Grafeno - root@host"
    finally:
        remotesession.deactivate()


def test_window_title_no_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # No active session -> falls back to the cwd basename.
    assert app_module._window_title() == f"Grafeno - {os.path.basename(str(tmp_path))}"


# ---------------------------------------------------------------------- #
# main() rejects an invalid remote spec with exit code 2
# ---------------------------------------------------------------------- #
def test_main_bad_spec(monkeypatch):
    from grafeno import app as app_module_local

    monkeypatch.setattr("sys.argv", ["grafeno", "root@host:/var/www"])
    # Avoid pushing screens: replace App.run with a no-op.
    app_mock = MagicMock()
    monkeypatch.setattr(app_module_local, "GrafenoApp", app_mock)

    with pytest.raises(SystemExit) as exc:
        app_module_local.main()
    # argparse error -> exit code 2.
    assert exc.value.code == 2
