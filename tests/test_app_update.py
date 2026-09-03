"""Tests of the ``grafeno update`` CLI interception in app.main."""

from __future__ import annotations

import pytest

from grafeno import app as app_module


def test_main_intercepts_update_command(monkeypatch):
    monkeypatch.setattr("sys.argv", ["grafeno", "update"])
    monkeypatch.setattr("grafeno.selfupdate.cli_update", lambda: 0)
    with pytest.raises(SystemExit) as excinfo:
        app_module.main()
    assert excinfo.value.code == 0


def test_main_intercepts_update_failure_code(monkeypatch):
    monkeypatch.setattr("sys.argv", ["grafeno", "update"])
    monkeypatch.setattr("grafeno.selfupdate.cli_update", lambda: 1)
    with pytest.raises(SystemExit) as excinfo:
        app_module.main()
    assert excinfo.value.code == 1


def test_main_version_flag_prints_version(monkeypatch, capsys):
    """``grafeno --version`` prints the version and exits 0 (before the TUI)."""
    from grafeno import __version__

    monkeypatch.setattr("sys.argv", ["grafeno", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        app_module.main()
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert "grafeno" in out


def test_main_version_short_flag(monkeypatch, capsys):
    """``grafeno -v`` is an alias of ``--version``."""
    from grafeno import __version__

    monkeypatch.setattr("sys.argv", ["grafeno", "-v"])
    with pytest.raises(SystemExit) as excinfo:
        app_module.main()
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out