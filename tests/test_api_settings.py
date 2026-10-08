"""Tests of the catalog, report and settings operations (server/settings.py)."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from grafeno import config as config_module
from grafeno import i18n, profiles, references, triggers
from grafeno.config import RoleConfig
from grafeno.server import settings
from grafeno.server.actions import ApiError


class FakeService:
    app = None

    def _log(self, message: str) -> None:
        pass


def test_form_options_defaults_and_profiles(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = config_module.load()
    cfg.automode.test_command = "pytest -q"
    cfg.first_prompt = "first"
    config_module.save(cfg)
    profiles.save_global([profiles.Profile(name="fast", planner=RoleConfig("claude", "sonnet", ""))])
    data = settings.form_options(FakeService())
    assert data["defaults"]["workdir"] == str(tmp_path)
    assert data["defaults"]["test_command"] == "pytest -q"
    assert data["defaults"]["first_prompt"] == "first"
    assert data["profiles"][0]["name"] == "fast"
    assert data["profiles"][0]["roles"]["planner"]["cli"] == "claude"
    assert "plan=claude/sonnet" not in data["defaults"]["roles_summary"]  # the summary is of the global config, not of the profile
    assert data["defaults"]["roles_summary"].count("·") == 4
    assert "plan=" in data["defaults"]["roles_summary"]
    assert "tests" in data["hook_stages"]
    assert data["session"]["active"] is False


def test_list_dirs_children_and_prefix(tmp_path) -> None:
    for name in ("alpha", "beta", "alps", ".hidden"):
        (tmp_path / name).mkdir()
    (tmp_path / "file.txt").write_text("x")
    data = settings.list_dirs(FakeService(), str(tmp_path))
    assert sorted(d.rsplit("/", 1)[1] for d in data["dirs"]) == ["alpha", "alps", "beta"]
    data = settings.list_dirs(FakeService(), str(tmp_path / "al"))
    assert sorted(d.rsplit("/", 1)[1] for d in data["dirs"]) == ["alpha", "alps"]
    assert settings.list_dirs(FakeService(), "/definitely/missing/x")["dirs"] == []


def test_list_issues_without_repo(tmp_path) -> None:
    data = asyncio.run(settings.list_issues(FakeService(), str(tmp_path)))
    assert data == {"available": False, "issues": []}


def test_usage_report_periods() -> None:
    today = date.today().isoformat()
    data = settings.usage_report(FakeService())
    assert data["from"] == data["to"] == today
    data = settings.usage_report(FakeService(), period="month")
    assert data["from"].endswith("-01")
    data = settings.usage_report(FakeService(), start="2026-02-10", end="2026-02-01")
    assert (data["from"], data["to"]) == ("2026-02-01", "2026-02-10")
    with pytest.raises(ApiError):
        settings.usage_report(FakeService(), start="yesterday")
    with pytest.raises(ApiError):
        settings.usage_report(FakeService(), period="year")


def test_settings_secrets_are_write_only() -> None:
    cfg = config_module.load()
    cfg.telegram.bot_token = "123:secret"
    cfg.api.tokens = "apikey"
    config_module.save(cfg)
    data = settings.get_settings(FakeService())
    assert data["telegram"]["bot_token_set"] is True
    assert "bot_token" not in data["telegram"]
    assert data["api"]["tokens_set"] is True
    assert "123:secret" not in str(data) and "apikey" not in str(data)
    # Empty value keeps the secret; <key>_clear empties it.
    settings.update_settings(FakeService(), {"telegram": {"bot_token": ""}, "api": {"tokens_clear": True}})
    cfg = config_module.load()
    assert cfg.telegram.bot_token == "123:secret"
    assert cfg.api.tokens == ""


def test_update_settings_sections() -> None:
    cfg = config_module.load()
    cfg.theme = "nord"
    config_module.save(cfg)
    result = settings.update_settings(FakeService(), {
        "roles": {"planner": {"cli": "claude", "model": "opus", "effort": "high"}},
        "automode": {"enabled": True, "max_iterations": 7, "test_command": "make test"},
        "hook": {"command": "echo", "stages": ["plan", "nope"]},
        "language": "es",
        "prompt_language": "en",
        "workspaces": "~/a, ~/b",
        "references": [{"name": "r", "path": "/tmp"}, {"name": "", "path": "/x"}],
        "triggers": [{"name": "t", "description": "d", "phases": "plan,bogus", "timing": "before"}],
        "profiles": [{"name": "p", "roles": {"final": {"cli": "codex"}}}, {"name": "p"}],
    })
    cfg = config_module.load()
    assert cfg.theme == "nord"  # untouched fields survive
    assert (cfg.planner.cli, cfg.planner.model, cfg.planner.effort) == ("claude", "opus", "high")
    assert cfg.automode.enabled is True and cfg.automode.max_iterations == 7
    assert cfg.hook.stages == "plan"
    assert cfg.workspaces == ["~/a", "~/b"]
    assert i18n.current_language() == "es"  # applied at once, like the TUI
    assert i18n.prompt_language() == "en"
    assert [ref.name for ref in references.load_global()] == ["r"]
    trigger = triggers.load_global()[0]
    assert (trigger.phases, trigger.timing) == ("plan", "before")
    assert [p.name for p in profiles.load_global()] == ["p"]
    assert profiles.load_global()[0].final.cli == "codex"
    assert result["language"] == "es"


@pytest.mark.parametrize(
    "payload",
    [
        {"automode": {"max_iterations": 0}},
        {"automode": {"max_iterations": "x"}},
        {"api": {"port": 70000}},
        {"roles": {"planner": {"cli": "bogus"}}},
        {"profiles": [{"name": "p", "roles": {"planner": {"cli": "bogus"}}}]},
    ],
)
def test_update_settings_validation(payload) -> None:
    with pytest.raises(ApiError) as exc:
        settings.update_settings(FakeService(), payload)
    assert exc.value.status == 400


def test_models_catalog_is_cached(monkeypatch) -> None:
    import grafeno.drivers as drivers

    calls = []

    async def fake_models(clis):
        calls.append("models")
        return {cli: [cli + "/m"] for cli in clis}

    async def fake_variants(clis):
        return {"claude": {"claude/m": ["high"]}}

    monkeypatch.setattr(drivers, "fetch_all_models", fake_models)
    monkeypatch.setattr(drivers, "fetch_all_variants", fake_variants)
    service = FakeService()
    first = asyncio.run(settings.models_catalog(service))
    second = asyncio.run(settings.models_catalog(service))
    assert first is second and calls == ["models"]
    asyncio.run(settings.models_catalog(service, refresh=True))
    assert calls == ["models", "models"]
    assert first["variants"]["claude"]["claude/m"] == ["high"]
