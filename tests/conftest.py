"""Fixtures comunes: GRAFENO_HOME aislado por test."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def grafeno_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAFENO_HOME", str(tmp_path / ".grafeno"))
    return tmp_path / ".grafeno"
