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