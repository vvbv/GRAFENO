"""Tests for the ApiConfig section of the global config."""

from __future__ import annotations

from grafeno.config import API_TOKEN_ENV, ApiConfig, Config


class TestApiConfig:
    def test_defaults(self) -> None:
        cfg = ApiConfig()
        assert cfg.enabled is False
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8735
        assert cfg.tokens == ""

    def test_roundtrip(self) -> None:
        cfg = ApiConfig(enabled=True, host="0.0.0.0", port=9999, tokens="a,b")
        assert ApiConfig.from_dict(cfg.to_dict()).to_dict() == cfg.to_dict()

    def test_resolve_tokens_merges_env(self, monkeypatch) -> None:
        monkeypatch.setenv(API_TOKEN_ENV, "env-token")
        cfg = ApiConfig(tokens="file-token, env-token")
        assert cfg.resolve_tokens() == {"env-token", "file-token"}

    def test_config_nested_section(self) -> None:
        cfg = Config()
        cfg.api = ApiConfig(enabled=True, tokens="x")
        assert Config.from_dict(cfg.to_dict()).api.tokens == "x"
