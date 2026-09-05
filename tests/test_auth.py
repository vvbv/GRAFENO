"""Unit tests for grafeno.server.auth (optional token semantics)."""

from __future__ import annotations

import pytest

from grafeno.config import API_TOKEN_ENV, ApiConfig
from grafeno.server import auth


class TestCheckOpen:
    """Empty token set = no API key required (open access)."""

    def test_empty_tokens_allows_no_header(self, monkeypatch) -> None:
        monkeypatch.delenv(API_TOKEN_ENV, raising=False)
        auth.check(ApiConfig(tokens=""), {}, None)

    def test_empty_tokens_ignores_any_bearer(self, monkeypatch) -> None:
        monkeypatch.delenv(API_TOKEN_ENV, raising=False)
        auth.check(ApiConfig(tokens=""), {"Authorization": "Bearer whatever"}, None)


class TestCheckProtected:
    """Configured tokens re-enable Bearer/query authentication."""

    def test_missing_token_raises(self, monkeypatch) -> None:
        monkeypatch.delenv(API_TOKEN_ENV, raising=False)
        with pytest.raises(auth.AuthError):
            auth.check(ApiConfig(tokens="t1"), {}, None)

    def test_wrong_token_raises(self, monkeypatch) -> None:
        monkeypatch.delenv(API_TOKEN_ENV, raising=False)
        with pytest.raises(auth.AuthError):
            auth.check(ApiConfig(tokens="t1"), {"authorization": "Bearer t2"}, None)

    def test_bearer_token_accepted(self, monkeypatch) -> None:
        monkeypatch.delenv(API_TOKEN_ENV, raising=False)
        auth.check(ApiConfig(tokens="t1"), {"Authorization": "Bearer t1"}, None)

    def test_query_token_accepted(self, monkeypatch) -> None:
        monkeypatch.delenv(API_TOKEN_ENV, raising=False)
        auth.check(ApiConfig(tokens="t1"), {}, {"token": "t1"})

    def test_env_token_also_protects(self, monkeypatch) -> None:
        monkeypatch.setenv(API_TOKEN_ENV, "env-token")
        with pytest.raises(auth.AuthError):
            auth.check(ApiConfig(tokens=""), {}, None)
        auth.check(ApiConfig(tokens=""), {"Authorization": "Bearer env-token"}, None)
