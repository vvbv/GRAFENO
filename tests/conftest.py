"""Common fixtures: GRAFENO_HOME isolated per test + default language."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def grafeno_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAFENO_HOME", str(tmp_path / ".grafeno"))
    return tmp_path / ".grafeno"


@pytest.fixture(autouse=True)
def default_language():
    """Guarantee each test starts with English language (global state) and
    internal prompts following it."""
    from grafeno import i18n

    i18n.set_language("en")
    i18n.set_prompt_language("")
    yield
    i18n.set_language("en")
    i18n.set_prompt_language("")


@pytest.fixture(autouse=True)
def _clear_remote_session():
    """Ensure no remote session leaks between tests."""
    yield
    from grafeno import remotesession

    remotesession.deactivate()


@pytest.fixture(autouse=True)
def _offline_release_check(monkeypatch):
    """Keep the GRAFENO release check off the network in every test.

    Any mounted ``GrafenoApp`` runs the self-update check, which would hit the
    GitHub API (60 requests/hour unauthenticated) on every test run. Tests that
    exercise the check inject their own ``opener=``.
    """
    import urllib.error

    from grafeno import selfupdate

    def _offline(request, timeout):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(selfupdate, "default_opener", _offline)
    monkeypatch.setattr(selfupdate, "_latest_from_redirect", lambda timeout: None)
