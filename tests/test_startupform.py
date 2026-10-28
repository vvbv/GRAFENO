"""Tests of the reusable startup-commands form widget."""

from __future__ import annotations

import asyncio

from textual.widgets import Button, Input, Select

from grafeno.config import StartupAction
from grafeno.tui.startupform import StartupForm


def _holder_app():
    from textual.app import App

    class Holder(App):
        def compose(self):
            yield StartupForm()

    return Holder()


def test_set_and_get_actions_roundtrip():
    async def scenario():
        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(StartupForm)
            actions = [StartupAction(name="a", command="x"), StartupAction(name="b", command="y", enabled=False)]
            form.set_actions(actions)
            out = form.actions()
            assert out == actions
            assert out is not actions

    asyncio.run(scenario())


def test_preset_fills_inputs_and_add_appends():
    async def scenario():
        app = _holder_app()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            form = app.query_one(StartupForm)
            app.query_one("#startup-preset", Select).value = "vscode"
            await pilot.pause()
            assert app.query_one("#startup-command", Input).value == (
                "code serve-web --host 0.0.0.0 --port 8080 --without-connection-token"
            )
            assert app.query_one("#startup-settings", Input).value == '{"git.repositoryScanMaxDepth": -1}'
            assert app.query_one("#startup-open-url", Input).value == "http://{host}:8080/?folder={folder}"
            app.query_one("#startup-add", Button).press()
            await pilot.pause()
            assert len(form.actions()) == 1
            assert form.actions()[0].name == "vscode-web"
            assert form.actions()[0].open_url == "http://{host}:8080/?folder={folder}"

    asyncio.run(scenario())


def test_add_rejects_invalid_input():
    async def scenario():
        app = _holder_app()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            form = app.query_one(StartupForm)
            app.query_one("#startup-add", Button).press()  # empty name
            await pilot.pause()
            assert form.actions() == []
            app.query_one("#startup-name", Input).value = "a"
            app.query_one("#startup-command", Input).value = "b"
            app.query_one("#startup-settings-file", Input).value = "/tmp/x.json"
            app.query_one("#startup-settings", Input).value = "[1]"
            app.query_one("#startup-add", Button).press()
            await pilot.pause()
            assert form.actions() == []

    asyncio.run(scenario())
