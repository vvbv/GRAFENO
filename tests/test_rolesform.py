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


def test_rolesform_restricts_offered_clis():
    from textual.app import App

    class Host(App):
        def compose(self):
            yield RolesForm((("session", "cs.role"),), clis=("claude", "opencode"), id="rf")

    async def main():
        app = Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            select = app.query_one("#session-cli", Select)
            assert [opt[1] for opt in select._options] == ["claude", "opencode"]

    asyncio.run(main())


def _effort_host(models, variants, saved=None):
    """Host app with one planner RolesForm preloaded with a catalogue."""
    from textual.app import App

    class Host(App):
        def compose(self):
            yield RolesForm((("planner", "cfg.role.planner"),), id="rf")

    return Host(), models, variants, saved


def test_rolesform_effort_levels_follow_cli_and_model():
    from grafeno.i18n import t

    models = {
        "kimi": ["kimi-code/k3", "kimi-code/kimi-for-coding-highspeed"],
        "minimax": ["minimax/MiniMax-M3"],
    }
    variants = {
        "kimi": {"kimi-code/k3": ["low", "high", "max"]},
        "minimax": {"minimax/MiniMax-M3": ["none-thinking", "thinking"]},
    }

    async def main():
        app = _effort_host(models, variants)[0]
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one("#rf", RolesForm)
            form.set_models(models)
            form.set_variants(variants)
            cli = app.query_one("#planner-cli", Select)
            model = app.query_one("#planner-model", Select)
            effort = app.query_one("#planner-effort", Select)

            cli.value = "kimi"
            await pilot.pause()
            model.value = "kimi-code/k3"
            await pilot.pause()
            assert _visible_values(effort) == ["low", "high", "max"]
            assert effort.disabled is False

            model.value = "kimi-code/kimi-for-coding-highspeed"
            await pilot.pause()
            assert effort.disabled is True
            assert effort.prompt == t("cfg.effort.none")

            model.clear()
            await pilot.pause()
            assert effort.prompt == t("cfg.effort.pick_model")

            cli.value = "minimax"
            await pilot.pause()
            model.value = "minimax/MiniMax-M3"
            await pilot.pause()
            assert _visible_values(effort) == ["none-thinking", "thinking"]
            assert effort.disabled is False

    asyncio.run(main())


def test_rolesform_effort_tolerates_provider_prefix():
    models = {"kimi": ["k3"]}
    variants = {"kimi": {"kimi-code/k3": ["low"]}}

    async def main():
        app = _effort_host(models, variants)[0]
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one("#rf", RolesForm)
            form.set_models(models)
            form.set_role("planner", "kimi", "k3", "")
            form.set_variants(variants)
            await pilot.pause()
            assert _visible_values(app.query_one("#planner-effort", Select)) == ["low"]

    asyncio.run(main())


def test_rolesform_keeps_saved_effort_without_catalogue():
    async def main():
        app = _effort_host({}, {})[0]
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one("#rf", RolesForm)
            form.set_role("planner", "kimi", "kimi-code/k3", "high")
            await pilot.pause()
            assert form.role_values("planner")[2] == "high"
            assert app.query_one("#planner-effort", Select).disabled is False

    asyncio.run(main())
