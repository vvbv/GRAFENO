"""Fixtures comunes: GRAFENO_HOME aislado por test + idioma por defecto."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def grafeno_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAFENO_HOME", str(tmp_path / ".grafeno"))
    return tmp_path / ".grafeno"


@pytest.fixture(autouse=True)
def default_language():
    """Garantiza que cada test arranca con idioma inglés (estado global)."""
    from grafeno import i18n

    i18n.set_language("en")
    yield
    i18n.set_language("en")
