"""Tests of the [api] section in the global settings screen."""

from __future__ import annotations

import asyncio

from grafeno import config as config_module
from grafeno.app import GrafenoApp
from grafeno.tui.screens.config import ConfigScreen
from grafeno.tui.screens.tasks import TaskListScreen
from textual.widgets import Checkbox, Input


async def _fake_fetch(clis):
    return {"opencode": ["opencode-go/kimi-k3"], "kimi": []}


async def _fake_fetch_variants(clis):
    return {}


def test_api_section_roundtrip(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 180)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            # Defaults: disabled, default host/port, no tokens.
            assert app.screen.query_one("#api-enabled", Checkbox).value is False
            assert app.screen.query_one("#api-host", Input).value == "127.0.0.1"
            assert app.screen.query_one("#api-port", Input).value == "8735"
            assert app.screen.query_one("#api-tokens", Input).value == ""

            # Fill the values and save.
            app.screen.query_one("#api-enabled", Checkbox).value = True
            app.screen.query_one("#api-host", Input).value = "0.0.0.0"
            app.screen.query_one("#api-port", Input).value = "9999"
            app.screen.query_one("#api-tokens", Input).value = "secret,another"

            app.screen.query_one("#cfg-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#cfg-save")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)

        saved = config_module.load().api
        assert saved.enabled is True
        assert saved.host == "0.0.0.0"
        assert saved.port == 9999
        assert saved.tokens == "secret,another"

    asyncio.run(scenario())


def test_api_port_out_of_range_aborts_save(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 180)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            app.screen.query_one("#api-port", Input).value = "0"
            app.screen.query_one("#cfg-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#cfg-save")
            await pilot.pause()
            # Still on the screen: save was aborted by the validation.
            assert isinstance(app.screen, ConfigScreen)

    asyncio.run(scenario())


def test_api_port_non_integer_aborts_save(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 180)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            app.screen.query_one("#api-port", Input).value = "not-a-number"
            app.screen.query_one("#cfg-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#cfg-save")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

    asyncio.run(scenario())
