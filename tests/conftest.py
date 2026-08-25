"""Common fixtures: GRAFENO_HOME isolated per test + default language."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def grafeno_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAFENO_HOME", str(tmp_path / ".grafeno"))
    return tmp_path / ".grafeno"


@pytest.fixture(autouse=True)
def default_language():
    """Guarantee each test starts with English language (global state)."""
    from grafeno import i18n

    i18n.set_language("en")
    yield
    i18n.set_language("en")
