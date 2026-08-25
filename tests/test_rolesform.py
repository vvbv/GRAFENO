"""Tests of the roles form: model selector filtering."""

from __future__ import annotations

import asyncio

from grafeno.app import GrafenoApp
from grafeno.tui.rolesform import RolesForm, filter_models
from grafeno.tui.screens.config import ConfigScreen
from textual.widgets import Input, Select


async def _fake_fetch(clis):
    return {
        "opencode": ["opencode-go/kimi-k3", "opencode/big-pickle", "opencode/k3-turbo"],
        "kimi": ["kimi-code/k3"],
    }


async def _fake_fetch_variants(clis):
    return {}


def _visible_values(select: Select) -> list[str]:
    """Return the visible values of the Select, excluding the NULL sentinel."""
    return [opt[1] for opt in select._options if opt[1] is not Select.NULL]


def test_filter_models_substring_casefold():
    models = ["opencode-go/kimi-k3", "opencode/big-pickle", "opencode/k3-turbo"]
    assert filter_models(models, "") == models
    assert filter_models(models, "  ") == models
    assert filter_models(models, "K3") == ["opencode-go/kimi-k3", "opencode/k3-turbo"]
    assert filter_models(models, "pickle") == ["opencode/big-pickle"]
    assert filter_models(models, "inexistente") == []


def test_rolesform_filter_narrows_select_options(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            for _ in range(50):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break

            select = app.screen.query_one("#planner-model", Select)
            filter_input = app.screen.query_one("#planner-model-filter", Input)
            filter_input.value = "pickle"
            await pilot.pause()
            assert _visible_values(select) == ["opencode/big-pickle"]

            select.value = "opencode/big-pickle"
            await pilot.pause()
            filter_input.value = "turbo"
            await pilot.pause()
            values = _visible_values(select)
            assert "opencode/k3-turbo" in values
            assert "opencode/big-pickle" in values

    asyncio.run(scenario())


def test_rolesform_cli_change_clears_filter(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            for _ in range(50):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break

            filter_input = app.screen.query_one("#planner-model-filter", Input)
            cli = app.screen.query_one("#planner-cli", Select)
            filter_input.value = "k3"
            await pilot.pause()
            cli.value = "kimi"
            for _ in range(20):
                await pilot.pause(0.05)
                if filter_input.value == "":
                    break
            assert filter_input.value == ""

    asyncio.run(scenario())
